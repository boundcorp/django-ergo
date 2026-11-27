"""Independent adversarial regression tests; no live runtime or untrusted execution."""

import copy
import json
import queue
import threading
from datetime import UTC
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from types import SimpleNamespace

import httpx
import pytest

from django_ergo.analysis.controller import Controller
from django_ergo.analysis.controller import make_server
from django_ergo.analysis.kubernetes import TERMINATION_FINALIZER
from django_ergo.analysis.kubernetes import KubernetesRuntime
from django_ergo.analysis.protocol import ProtocolError
from django_ergo.analysis.protocol import open_envelope
from django_ergo.analysis.protocol import seal
from django_ergo.analysis.results import dataset_result
from django_ergo.analysis.results import select_result
from tests.test_analysis_runtime import KEY
from tests.test_analysis_runtime import Runtime
from tests.test_analysis_runtime import broker as runtime_broker
from tests.test_analysis_runtime import uid


@pytest.fixture
def broker(tmp_path):
    return runtime_broker.__wrapped__(tmp_path)


@pytest.mark.parametrize("state", ["running", "launching", "reaping"])
def test_disappearance_never_releases_fence(broker, tmp_path, state):
    controller, runtime, body = broker
    controller.dispatch(body)
    runtime.pods.clear()  # API object lost; process on partitioned node may survive.
    with controller.db:
        controller.db.execute(
            "UPDATE jobs SET state=?,started=0,final='timeout'", (state,)
        )
    restarted = Controller(str(tmp_path / "ledger.sqlite"), runtime)
    assert restarted.dispatch({**body, "action": "status"})["state"] == "reaping"
    with pytest.raises(ProtocolError, match="busy"):
        restarted.dispatch({**body, "job": uid()})
    with restarted.db:
        restarted.db.execute("UPDATE workspaces SET touched=0")
    restarted.reconcile()
    assert runtime.deleted == []


def test_crash_after_finalizer_removal_preserves_termination_evidence(
    broker, tmp_path, monkeypatch
):
    controller, runtime, body = broker
    controller.dispatch(body)
    runtime.pods[body["job"]] = "succeeded"

    def crash_after_release(job):
        runtime.pods.pop(job)
        message = "fixture crash after finalizer removal"
        raise OSError(message)

    monkeypatch.setattr(runtime, "release_job", crash_after_release)
    with pytest.raises(OSError, match="finalizer"):
        controller.reconcile()
    row = controller.db.execute("SELECT state,stopped FROM jobs").fetchone()
    assert tuple(row) == ("reaping", 1)
    monkeypatch.setattr(runtime, "release_job", lambda job: None)
    restarted = Controller(str(tmp_path / "ledger.sqlite"), runtime)
    response = restarted.dispatch({**body, "action": "status"})
    assert response["state"] == "succeeded"
    assert response["response"]["result"]["output"] == "ok"


def test_cancel_survives_partition_and_later_kubelet_confirmation(broker):
    controller, runtime, body = broker
    controller.dispatch(body)
    runtime.hold_deletion = True
    assert controller.dispatch({**body, "action": "cancel"})["state"] == "reaping"
    runtime.pods[body["job"]] = "unknown"
    controller.reconcile()
    assert controller.db.execute("SELECT stopped FROM jobs").fetchone()[0] == 0
    runtime.hold_deletion = False
    controller.reconcile()  # request graceful deletion, retained API object
    assert controller.dispatch({**body, "action": "status"})["state"] == "cancelled"


def runtime_for(core):
    return KubernetesRuntime(
        core=core,
        namespace="analysis",
        image="registry/runner@sha256:" + "a" * 64,
        storage_class="hard-quota",
        runtime_class="gvisor",
    )


@pytest.mark.parametrize("reason", ["NodeLost", "ContainerStatusUnknown"])
def test_synthetic_terminal_status_is_not_termination_evidence(reason):
    terminated = SimpleNamespace(finished_at=datetime.now(UTC), reason=reason)
    pod = SimpleNamespace(
        status=SimpleNamespace(
            phase="Failed",
            reason=None,
            container_statuses=[
                SimpleNamespace(
                    name="executor", state=SimpleNamespace(terminated=terminated)
                )
            ],
        )
    )
    runtime = runtime_for(SimpleNamespace(read_namespaced_pod=lambda *a, **kw: pod))
    assert runtime.status(uid()) == "unknown"


def test_controller_authentication_over_actual_http(tmp_path):
    ready = queue.Queue()

    def run_server():
        controller = Controller(str(tmp_path / "http.sqlite"), Runtime())
        server = make_server(controller, KEY, port=0)
        ready.put(server)
        try:
            server.serve_forever(poll_interval=0.02)
        finally:
            controller.db.close()
            server.server_close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    server = ready.get(timeout=5)
    url = f"http://127.0.0.1:{server.server_port}/v1"
    body = {
        "action": "submit",
        "workspace": uid(),
        "session": uid(),
        "user": "1",
        "job": uid(),
        "request": {"op": "list"},
    }
    raw = seal(KEY, body)
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            good = client.post(url, content=raw)
            assert good.status_code == 200
            reply = open_envelope(KEY, good.content)["body"]
            assert reply["request_nonce"] == open_envelope(KEY, raw)["nonce"]
            assert reply["response"]["state"] == "running"
            assert client.post(url, content=raw).status_code == 400
            assert (
                client.post(
                    url, content=seal(b"wrong-fixture-key" * 3, body)
                ).status_code
                == 400
            )
            assert client.post(url, content=seal(KEY, body, now=1)).status_code == 400
            assert client.post(url, content=b"[]").status_code == 400
            assert (
                client.post(url + "/wrong", content=seal(KEY, body)).status_code == 400
            )
            assert client.post(url, content=b"x" * (96 * 1024 + 1)).status_code == 400
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_adapter_with_generated_kubernetes_client_over_http():  # noqa: C901
    """Exercise real client serialization, URLs, query names, exceptions and models."""
    from kubernetes import client

    calls = []
    objects = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_api(self):
            path = self.path.split("?", 1)[0]
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw) if raw else None
            calls.append(
                (self.command, self.path, body, self.headers.get("Content-Type"))
            )
            status, value = 200, {}
            if self.command == "POST":
                name = body["metadata"]["name"]
                objects[path + "/" + name] = body
                value = body
                if body["kind"] == "Pod":
                    body["metadata"].update(uid="fixture-uid", resourceVersion="1")
                    body["status"] = {
                        "phase": "Succeeded",
                        "containerStatuses": [
                            {
                                "name": "executor",
                                "image": "fixture",
                                "imageID": "fixture",
                                "ready": False,
                                "restartCount": 0,
                                "state": {
                                    "terminated": {
                                        "exitCode": 0,
                                        "reason": "Completed",
                                        "finishedAt": "2026-11-27T01:00:00Z",
                                    }
                                },
                            }
                        ],
                    }
            elif path.endswith("/log"):
                value = {"ok": True, "result": {"output": "fixture"}}
            elif path not in objects:
                status, value = (
                    404,
                    {"kind": "Status", "message": "Not found", "code": 404},
                )
            elif self.command == "GET":
                value = objects[path]
            elif self.command == "DELETE":
                if "pods/" in path:
                    objects[path]["metadata"]["deletionTimestamp"] = (
                        "2026-11-27T01:00:01Z"
                    )
                else:
                    objects.pop(path)
            elif self.command == "PATCH":
                assert self.headers["Content-Type"] == "application/json-patch+json"
                assert body[0] == {
                    "op": "test",
                    "path": "/metadata/uid",
                    "value": "fixture-uid",
                }
                assert body[1]["value"] == "1"
                assert body[2]["value"] == []
                objects.pop(path)
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/plain" if path.endswith("/log") else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = handle_api  # noqa: N815 — standard HTTP handler names
        do_POST = handle_api  # noqa: N815 — standard HTTP handler names
        do_DELETE = handle_api  # noqa: N815 — standard HTTP handler names
        do_PATCH = handle_api  # noqa: N815 — standard HTTP handler names

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = client.Configuration()
    config.host = f"http://127.0.0.1:{server.server_port}"
    config.proxy = None
    job, workspace = uid(), uid()
    try:
        with client.ApiClient(config) as api:
            runtime = runtime_for(client.CoreV1Api(api))
            runtime.launch(job, workspace, {"op": "list"})
            assert runtime.status(job) == "succeeded"
            assert runtime.result(job)["result"]["output"] == "fixture"
            runtime.delete_job(job)
            assert runtime.status(job) == "succeeded"  # finalizer retains object
            runtime.release_job(job)
            assert runtime.status(job) == "absent"
            assert runtime.delete_workspace(workspace) is False
            assert runtime.delete_workspace(workspace) is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    pod = next(c[2] for c in calls if c[0] == "POST" and c[2]["kind"] == "Pod")
    assert pod["metadata"]["finalizers"] == [TERMINATION_FINALIZER]
    assert pod["spec"]["containers"][0]["command"][1] == "-I"
    assert any("limitBytes=" in c[1] for c in calls)
    deletion = next(c for c in calls if c[0] == "DELETE" and "/pods/" in c[1])
    assert "gracePeriodSeconds=1" in deletion[1]
    assert "propagationPolicy=Foreground" in deletion[1]


@pytest.mark.parametrize(
    "patch",
    [
        {"rows": 2**63},
        {"rows": True},
        {"schema": [{"name": "x", "type": []}]},
        {"version": []},
        {"schema": [{"name": "x", "type": "int"}, {"name": "X", "type": "int"}]},
    ],
)
def test_forged_dataset_result_rejected(patch):
    value = {"version": "a" * 64, "rows": 2, "schema": [{"name": "x", "type": "int"}]}
    with pytest.raises(ProtocolError):
        dataset_result({**value, **patch})


@pytest.mark.parametrize(
    "patch",
    [
        {"next_offset": 999},
        {"rows": [[{"payload": "nested"}]]},
        {"total_rows": True},
        {"total_rows": 0},
        {"source_rows": 999},
        {"columns": ["other"]},
        {"truncated": False},
    ],
)
def test_forged_select_cannot_mint_unbounded_cursor(patch):
    value = {
        "rows": [[1]],
        "columns": ["x"],
        "total_rows": 2,
        "source_rows": 2,
        "count_kind": "exact",
        "truncated": True,
        "next_offset": 1,
    }
    with pytest.raises(ProtocolError):
        select_result(
            {**copy.deepcopy(value), **patch},
            columns=["x"],
            source_rows=2,
            offset=0,
            limit=1,
        )


def test_cancel_before_ambiguous_submit_records_permanent_tombstone(broker):
    controller, runtime, body = broker
    response = controller.dispatch({**body, "action": "cancel"})
    assert response["state"] == "cancelled"
    assert runtime.launches == 0
    assert controller.dispatch(body)["state"] == "cancelled"
    assert runtime.launches == 0
    with pytest.raises(ProtocolError, match="conflict"):
        controller.dispatch({**body, "request": {"op": "list"}})


def test_executor_entrypoint_ignores_workspace_import_shadowing(tmp_path):
    import os
    import subprocess
    import sys

    (tmp_path / "json.py").write_text(
        "raise RuntimeError('workspace import executed')\n"
    )
    package = tmp_path / "django_ergo"
    package.mkdir()
    (package / "__init__.py").write_text(
        "raise RuntimeError('workspace package executed')\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-m", "django_ergo.analysis.executor"],
        cwd=tmp_path,
        env={**os.environ, "ANALYSIS_REQUEST": '{"op":"unknown-fixture"}'},
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout)["ok"] is False
    assert b"workspace import executed" not in result.stderr
    assert b"workspace package executed" not in result.stderr


def test_old_controller_ledger_upgrades_without_inventing_termination(broker, tmp_path):
    controller, runtime, body = broker
    controller.dispatch(body)
    with controller.db:
        controller.db.execute("ALTER TABLE jobs DROP COLUMN stopped")
    controller.db.close()
    restarted = Controller(str(tmp_path / "ledger.sqlite"), runtime)
    assert restarted.db.execute("SELECT stopped FROM jobs").fetchone()[0] == 0
    runtime.pods.clear()
    with restarted.db:
        restarted.db.execute("UPDATE jobs SET started=0")
    restarted.reconcile()
    assert restarted.db.execute("SELECT state FROM jobs").fetchone()[0] == "reaping"
