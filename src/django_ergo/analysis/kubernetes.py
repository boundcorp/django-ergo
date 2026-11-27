# ruff: noqa: TRY003, EM101, PLR2004
"""Controller-only Kubernetes adapter. Application processes never import this.

Requires an approved namespace with deny-all ingress/egress, hard PVC quotas,
a sandbox RuntimeClass and kubelet podPidsLimit <= 64. Refuse unpinned images.
"""

import hashlib
import hmac
import json
import re

from django_ergo.analysis.protocol import MAX_RESPONSE
from django_ergo.analysis.protocol import encode

TERMINATION_FINALIZER = "analysis.ergonaut.dev/termination-observed"
# The executor overwrites this file with {"v":1,"bytes":N,"sha256":...} for its
# one log record. The kubelet keeps at most 4096 bytes of it, which is why the
# result itself stays in the log. Admission (infra-octo ergo-analysis-job-pod)
# must pin both fields; FallbackToLogsOnError would copy user-influenced bytes.
TERMINATION_MESSAGE_PATH = "/dev/termination-log"
TERMINATION_MESSAGE_POLICY = "File"


class KubernetesRuntime:
    def __init__(self, *, core, namespace, image, storage_class, runtime_class):
        if (
            not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image)
            or not runtime_class
            or not storage_class
        ):
            raise ValueError(
                "Pinned image, quota-backed storage and sandbox runtime required"
            )
        self.core = core
        self.namespace = namespace
        self.image = image
        self.storage_class = storage_class
        self.runtime_class = runtime_class

    def pvc(self, workspace):
        return "analysis-" + workspace

    def ensure_workspace(self, workspace):
        from kubernetes.client.exceptions import ApiException

        name = self.pvc(workspace)
        try:
            self.core.read_namespaced_persistent_volume_claim(
                name, self.namespace, _request_timeout=(5, 15)
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.core.create_namespaced_persistent_volume_claim(
                self.namespace,
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": name, "labels": {"app": "analysis-job"}},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": self.storage_class,
                        "resources": {"requests": {"storage": "1Gi"}},
                    },
                },
                _request_timeout=(5, 15),
            )

    def pod_spec(self, job, workspace, request):
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "analysis-" + job,
                "finalizers": [TERMINATION_FINALIZER],
                "labels": {"app": "analysis-job", "analysis-workspace": workspace},
                "annotations": {
                    "sidecar.istio.io/inject": "false",
                    "linkerd.io/inject": "disabled",
                },
            },
            "spec": {
                "automountServiceAccountToken": False,
                "serviceAccountName": "analysis-job",
                "hostNetwork": False,
                "hostPID": False,
                "hostIPC": False,
                "enableServiceLinks": False,
                "restartPolicy": "Never",
                "runtimeClassName": self.runtime_class,
                "activeDeadlineSeconds": 60,
                "terminationGracePeriodSeconds": 1,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 10001,
                    "runAsGroup": 10001,
                    "fsGroup": 10001,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "executor",
                        "image": self.image,
                        "imagePullPolicy": "IfNotPresent",
                        "terminationMessagePath": TERMINATION_MESSAGE_PATH,
                        "terminationMessagePolicy": TERMINATION_MESSAGE_POLICY,
                        "command": [
                            "python",
                            "-I",
                            "-m",
                            "django_ergo.analysis.executor",
                        ],
                        "env": [
                            {
                                "name": "ANALYSIS_REQUEST",
                                "value": encode(request, limit=64 * 1024).decode(),
                            },
                            {"name": "MPLBACKEND", "value": "Agg"},
                            {
                                "name": "HOME",
                                "value": "/tmp",  # noqa: S108 — pod-private bounded tmpfs
                            },
                            {"name": "OPENBLAS_NUM_THREADS", "value": "1"},
                        ],
                        "securityContext": {
                            "readOnlyRootFilesystem": True,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "resources": {
                            "requests": {
                                "cpu": "100m",
                                "memory": "128Mi",
                                "ephemeral-storage": "128Mi",
                            },
                            "limits": {
                                "cpu": "1",
                                "memory": "512Mi",
                                "ephemeral-storage": "256Mi",
                            },
                        },
                        "volumeMounts": [
                            {"name": "workspace", "mountPath": "/workspace"},
                            {
                                "name": "tmp",
                                "mountPath": "/tmp",  # noqa: S108 — pod-private bounded tmpfs
                            },
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "workspace",
                        "persistentVolumeClaim": {"claimName": self.pvc(workspace)},
                    },
                    {
                        "name": "tmp",
                        "emptyDir": {"medium": "Memory", "sizeLimit": "128Mi"},
                    },
                ],
            },
        }

    def launch(self, job, workspace, request):
        self.ensure_workspace(workspace)
        self.core.create_namespaced_pod(
            self.namespace,
            self.pod_spec(job, workspace, request),
            _request_timeout=(5, 15),
        )

    def status(self, job):
        from kubernetes.client.exceptions import ApiException

        try:
            pod = self.core.read_namespaced_pod(
                "analysis-" + job, self.namespace, _request_timeout=(5, 15)
            )
        except ApiException as exc:
            if exc.status == 404:
                return "absent"
            raise
        phase = pod.status.phase.lower()
        if phase in {"succeeded", "failed"}:
            # PodGC/node-loss can synthesize a terminal phase. Require kubelet
            # container termination evidence, not merely a high-level Pod phase.
            statuses = pod.status.container_statuses or []
            if (
                pod.status.reason in {"NodeLost", "NodeShutdown"}
                or len(statuses) != 1
                or statuses[0].name != "executor"
                or not statuses[0].state.terminated
                or not statuses[0].state.terminated.finished_at
                or statuses[0].state.terminated.finished_at.year <= 1970
                or statuses[0].state.terminated.reason
                in {"ContainerStatusUnknown", "NodeLost"}
            ):
                return "unknown"
        return phase

    def release_job(self, job):
        """Remove ONLY our finalizer after the ledger commits termination evidence."""
        from kubernetes.client.exceptions import ApiException

        try:
            pod = self.core.read_namespaced_pod(
                "analysis-" + job, self.namespace, _request_timeout=(5, 15)
            )
            finalizers = pod.metadata.finalizers or []
            if TERMINATION_FINALIZER in finalizers:
                # Optimistic concurrency prevents removing another owner's finalizer
                # or mutating a replacement object after a stale read.
                self.core.patch_namespaced_pod(
                    "analysis-" + job,
                    self.namespace,
                    [
                        {
                            "op": "test",
                            "path": "/metadata/uid",
                            "value": pod.metadata.uid,
                        },
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": pod.metadata.resource_version,
                        },
                        {
                            "op": "replace",
                            "path": "/metadata/finalizers",
                            "value": [
                                v for v in finalizers if v != TERMINATION_FINALIZER
                            ],
                        },
                    ],
                    _request_timeout=(5, 15),
                )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def termination_record(self, job):
        pod = self.core.read_namespaced_pod(
            "analysis-" + job, self.namespace, _request_timeout=(5, 15)
        )
        statuses = pod.status.container_statuses or []
        terminated = statuses[0].state.terminated if len(statuses) == 1 else None
        try:
            record = json.loads((terminated and terminated.message) or "")
        except ValueError:
            return None
        if (
            not isinstance(record, dict)
            or set(record) != {"v", "bytes", "sha256"}
            or record["v"] != 1
            or type(record["bytes"]) is not int
            or not isinstance(record["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
        ):
            return None
        return record

    def result(self, job):
        # Never log pod output: this is untrusted user data, not controller telemetry.
        raw = self.core.read_namespaced_pod_log(
            "analysis-" + job,
            self.namespace,
            limit_bytes=MAX_RESPONSE + 1,
            _request_timeout=(5, 15),
        )
        if len(raw.encode()) > 64 * 1024:
            return {"ok": False, "error": "Output cap exceeded"}
        # The executor writes exactly one record and then a digest of it to the
        # termination message after every other process in the pod is dead. A
        # log that does not match was not written (only) by the executor.
        record = self.termination_record(job)
        data = raw.encode()
        if (
            record is None
            or record["bytes"] != len(data)
            or not hmac.compare_digest(
                record["sha256"], hashlib.sha256(data).hexdigest()
            )
        ):
            return {"ok": False, "error": "Executor record failed integrity check"}
        try:
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) not in ({"ok", "result"}, {"ok", "error"})
                or type(value["ok"]) is not bool
                or (value["ok"] is True and not isinstance(value.get("result"), dict))
                or (value["ok"] is False and not isinstance(value.get("error"), str))
            ):
                raise ValueError  # noqa: TRY301
            encode(value, limit=64 * 1024)
            return value
        except (ValueError, TypeError, RecursionError):
            return {"ok": False, "error": "Malformed executor response"}

    def delete_job(self, job):
        from kubernetes.client.exceptions import ApiException

        try:
            self.core.delete_namespaced_pod(
                "analysis-" + job,
                self.namespace,
                grace_period_seconds=1,
                propagation_policy="Foreground",
                _request_timeout=(5, 15),
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def delete_workspace(self, workspace):
        from kubernetes.client.exceptions import ApiException

        try:
            self.core.delete_namespaced_persistent_volume_claim(
                self.pvc(workspace), self.namespace, _request_timeout=(5, 15)
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            return True
        # Accepted deletion is asynchronous. A later 404 confirms completion.
        return False
