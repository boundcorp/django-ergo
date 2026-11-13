"""Bounded single-intent recovery and independently invocable REST cleanup.

Live create requires explicit execution and a fresh independent cleaner.
Inventory absence never resolves an unknown create. Known-ID GET 404 is separate
from storage evidence and from ambiguous mutating request failures.
"""

# This ledger adapter intentionally shares Experiment's transaction primitives.
# ruff: noqa: SLF001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

from .budget import MAX_RENTAL_SECONDS
from .experiment import Experiment
from .ssh import create_access
from .transport import CapacityError
from .transport import FixtureRunpodREST
from .transport import Ownership
from .transport import OwnershipError
from .transport import ProviderError
from .transport import RunpodREST


def _pod_id(value):
    return isinstance(value, str) and value.isascii() and value.isalnum()


class RESTCleanup:
    """Bridge one local ledger; an external supervisor must schedule each tick."""

    def __init__(self, path):
        if not Path(path).is_file():
            msg = "existing ledger required"
            raise ValueError(msg)
        self.experiment = Experiment(path)
        with self.experiment._tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rest_create_profile (
                    id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rest_storage_proofs (
                    resource_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS rest_retired_profiles (
                    fingerprint TEXT PRIMARY KEY, profile TEXT NOT NULL,
                    body TEXT NOT NULL, reason TEXT NOT NULL
                );
            """)

    def configure(self, ownership):
        """Bind cleanup-only ownership; this cannot establish storage absence."""
        self._configure(ownership, None)

    def configure_create(  # noqa: PLR0913
        self,
        *,
        account,
        image,
        gpu_type,
        data_center_ids,
        hardware,
        quoted_hourly_usd=None,
        ssh_public_key=None,
        gpu_identity=None,
    ):
        """Bind a digest, explicit location allowlist and existing absolute clock.

        ``hardware`` is validated ``hardware.requirements()`` data: cloud type,
        GPU count, host RAM/vCPU minimums and container disk come from policy.
        Binding a profile alone does not authorize live create.
        Omitting networkVolumeId (and any template) deliberately attaches none.
        """
        container_disk_gb = hardware["container_disk_gb"]
        if not isinstance(image, str) or not re.fullmatch(
            r"[^@\s]+@sha256:[0-9a-f]{64}", image
        ):
            msg = "digest-pinned image required"
            raise ValueError(msg)
        if (
            not isinstance(data_center_ids, list | tuple)
            or not data_center_ids
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)+", value)
                for value in data_center_ids
            )
            or len(set(data_center_ids)) != len(data_center_ids)
        ):
            msg = "explicit nonempty data-center allowlist required"
            raise ValueError(msg)
        if not isinstance(gpu_type, str) or not gpu_type:
            msg = "GPU type required"
            raise ValueError(msg)
        state = self.experiment.status()
        created, deadline = state["created"], state["deadline"]
        self.experiment._time(created)
        self.experiment._time(deadline)
        if not 0 < deadline - created <= MAX_RENTAL_SECONDS:
            msg = "absolute deadline must be within one hour of reservation"
            raise ValueError(msg)
        attempt = state["marker"]
        if quoted_hourly_usd is not None:
            try:
                quote = Decimal(quoted_hourly_usd)
                evidence = json.loads(state["evidence"])["budget"]["compute_hourly_usd"]
                admitted = Decimal(evidence["amount"])
            except (InvalidOperation, KeyError, TypeError, ValueError):
                msg = "invalid admitted compute quote"
                raise ValueError(msg) from None
            # The admitted rate is the profile's hourly ceiling; a live offer
            # may be cheaper but never above what the reservation priced.
            if (
                not quote.is_finite()
                or quote < 0
                or quote > admitted
                or evidence["verified"] is not True
            ):
                msg = "compute quote exceeds the admitted hourly ceiling"
                raise ValueError(msg)
        body = {
            "name": "ergo-" + attempt,
            "computeType": "GPU",
            "cloudType": hardware["cloud_type"],
            "gpuTypeIds": [gpu_type],
            "gpuCount": hardware["gpu_count"],
            "minRAMPerGPU": hardware["min_ram_per_gpu_gb"],
            "minVCPUPerGPU": hardware["min_vcpu_per_gpu"],
            "imageName": image,
            "containerDiskInGb": container_disk_gb,
            "volumeInGb": 0,
            "dataCenterIds": list(data_center_ids),
            "dataCenterPriority": "custom",
            "env": {
                "ERGO_ATTEMPT_ID": attempt,
                "ERGO_DEADLINE": str(deadline),
            },
        }
        if ssh_public_key is not None:
            access = create_access(ssh_public_key, gpu_type, gpu_identity)
            body["env"].update(access.pop("env"))
            body.update(access)
        if quoted_hourly_usd is not None:
            body["env"]["ERGO_COMPUTE_HOURLY_USD"] = str(quote)
        fingerprint = hashlib.sha256(
            json.dumps(
                body, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        ownership = Ownership(account, attempt, fingerprint, image, container_disk_gb)
        body["env"]["ERGO_ATTEMPT_FINGERPRINT"] = fingerprint
        self._configure(ownership, body)
        return ownership

    def _configure(self, ownership, body):
        if not isinstance(ownership, Ownership):
            msg = "ownership profile required"
            raise TypeError(msg)
        with self.experiment._tx() as db:
            row = self.experiment._row(db)
            if row["phase"] != "reserved" or row["create_started"]:
                msg = "profile must precede create intent"
                raise ValueError(msg)
            if ownership.attempt != row["marker"]:
                msg = "attempt must match ledger marker"
                raise ValueError(msg)
            db.execute(
                "CREATE TABLE IF NOT EXISTS rest_profile (id INTEGER PRIMARY KEY CHECK(id=1), profile TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS rest_resources (resource_id TEXT PRIMARY KEY, compute_absent INTEGER NOT NULL DEFAULT 0)"
            )
            if db.execute("SELECT 1 FROM rest_profile").fetchone():
                msg = "profile already bound"
                raise ValueError(msg)
            db.execute(
                "INSERT INTO rest_profile VALUES (1, ?)",
                (json.dumps(asdict(ownership), sort_keys=True),),
            )
            if body is not None:
                db.execute(
                    "INSERT INTO rest_create_profile VALUES (1, ?)",
                    (json.dumps(body, sort_keys=True),),
                )

    def _profile(self, db):
        try:
            row = db.execute("SELECT profile FROM rest_profile WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            msg = "REST profile not configured"
            raise ValueError(msg) from None
        if row is None:
            msg = "REST profile not configured"
            raise ValueError(msg)
        return Ownership(**json.loads(row["profile"]))

    def _profiles(self, db):
        """Current ownership first, then every retired never-created request."""
        try:
            current = db.execute(
                "SELECT profile FROM rest_profile WHERE id=1"
            ).fetchall()
        except sqlite3.OperationalError:
            current = []
        retired = db.execute(
            "SELECT profile FROM rest_retired_profiles ORDER BY rowid"
        ).fetchall()
        return [Ownership(**json.loads(row["profile"])) for row in current + retired]

    def _retire(self, db, reason):
        """Move the bound request aside so another offer can be bound.

        Retired ownerships stay in every cleanup scan, so even a provider that
        later materialized a refused request is still found and terminated.
        """
        profile = db.execute("SELECT profile FROM rest_profile WHERE id=1").fetchone()
        body = db.execute("SELECT body FROM rest_create_profile WHERE id=1").fetchone()
        if profile is None or body is None:
            msg = "no bound create request to retire"
            raise ValueError(msg)
        db.execute(
            "INSERT INTO rest_retired_profiles VALUES (?, ?, ?, ?)",
            (
                json.loads(profile["profile"])["fingerprint"],
                profile["profile"],
                body["body"],
                reason,
            ),
        )
        db.execute("DELETE FROM rest_profile")
        db.execute("DELETE FROM rest_create_profile")

    def retire_unsent(self):
        """Release a bound request that was never POSTed (crash before send)."""
        with self.experiment._tx() as db:
            row = self.experiment._row(db)
            try:
                bound = db.execute("SELECT 1 FROM rest_profile").fetchone()
            except sqlite3.OperationalError:
                bound = None
            if not bound:
                return False
            if row["create_started"]:
                msg = "create already attempted; inspect instead of replay"
                raise ValueError(msg)
            self._retire(db, "unsent")
            return True

    def _release_refused(self, client):
        """After an explicit no-capacity refusal, prove nothing exists, then release.

        Any inventory failure, receipt or Pod carrying this attempt's marker
        leaves the create uncertain: it is reconciled, never retried.
        """
        try:
            # Proving nothing was created is ownership reconciliation: reserve.
            pods = client.list_pods(priority=True)
        except ProviderError:
            pods = None
        with self.experiment._tx() as db:
            row = self.experiment._row(db)
            marker = row["marker"]
            present = pods is None or any(
                isinstance(pod.get("env"), dict)
                and pod["env"].get("ERGO_ATTEMPT_ID") == marker
                for pod in pods
            )
            if (
                present
                or not row["create_started"]
                or db.execute("SELECT 1 FROM experiment_receipts").fetchone()
            ):
                db.execute(
                    "UPDATE experiment SET cleanup_error='provider_request_failed' WHERE id=1"
                )
                return False
            self._retire(db, "no_capacity")
            db.execute(
                "UPDATE experiment SET create_started=0, cleanup_error=NULL, phase=? WHERE id=1",
                ("reserved" if row["stop_reason"] is None else "cleanup_pending",),
            )
            return True

    def create_once(self, *, now, client=None, live_execute=False):  # noqa: C901
        """Commit uncertainty before the sole POST; never replay on failure.

        An explicit no-capacity refusal, confirmed by inventory, releases the
        attempt (``create_started`` returns to 0) so the caller may bind the
        next offer. Timeouts and other failures stay uncertain.
        """
        self.experiment._time(now)
        if client is not None and not isinstance(client, FixtureRunpodREST):
            if not isinstance(client, RunpodREST) or live_execute is not True:
                msg = "create requires an offline fixture client; live create disabled"
                raise ValueError(msg)
            from .supervisor import CleanerSupervision

            CleanerSupervision(self.experiment.path).require_fresh(now=now)
        if client is not None:
            # Charge the POST to the shared request budget before committing
            # uncertainty. A refusal here provably sent nothing: the bind stays
            # unsent (retired by the next launch step) and RateLimitError
            # propagates to the caller as a wait, never as an uncertain create.
            client.reserve()
        with self.experiment._tx() as db:
            self._profile(db)
            request = db.execute(
                "SELECT body FROM rest_create_profile WHERE id=1"
            ).fetchone()
            if request is None:
                msg = "create profile not configured"
                raise ValueError(msg)
            body = json.loads(request["body"])
            if live_execute and "ERGO_COMPUTE_HOURLY_USD" not in body.get("env", {}):
                msg = "live create requires bound admitted compute quote"
                raise ValueError(msg)
            row = self.experiment._expire(db, self.experiment._row(db), now)
            if row["create_started"]:
                msg = "create already attempted; inspect instead of replay"
                raise ValueError(msg)
            if client is None or row["stop_reason"]:
                return dict(row)
            if row["phase"] != "reserved":
                msg = "create requires reserved intent"
                raise ValueError(msg)
            db.execute(
                "UPDATE experiment SET phase='create_unknown', create_started=1 WHERE id=1"
            )
            # The exact request is immutable and was committed with the profile.
        try:
            pod = client.create_pod(body)
            if not _pod_id(pod.get("id")):
                msg = "invalid provider create response"
                raise ProviderError(msg)  # noqa: TRY301
            self.experiment.receipt(pod["id"])
        except CapacityError:
            self._release_refused(client)
        except ProviderError:
            with self.experiment._tx() as db:
                db.execute(
                    "UPDATE experiment SET cleanup_error='provider_request_failed' WHERE id=1"
                )
        return self.experiment.status()

    def sweep_retired(self, *, client):
        """Terminate any Pod a refused (retired) request materialized late.

        Runs on every maintenance tick, not only after stop, so a late Pod never
        runs alongside the current offer. Returns a cleanup error or None.
        """
        with self.experiment._tx() as db:
            retired = [
                Ownership(**json.loads(row["profile"]))
                for row in db.execute("SELECT profile FROM rest_retired_profiles")
            ]
        if not retired:
            return None
        try:
            pods = client.list_pods()
        except ProviderError:
            return "provider_request_failed"
        error = None
        for pod in pods:
            if not _pod_id(pod.get("id")) or not self._owner(
                pod, retired, request_bound=True
            ):
                continue
            with self.experiment._tx() as db:
                self._remember(db, pod, retired, request_bound=True)
            result = self._clean_resource(
                pod["id"], client, retired, request_bound=True
            )
            error = error or result
        return error

    @staticmethod
    def _owner(pod, profiles, request_bound):
        return next(
            (p for p in profiles if p.matches(pod, request_bound=request_bound)), None
        )

    def _remember(self, db, pod, profiles, request_bound, *, verified_read=False):
        resource_id = pod.get("id")
        if not _pod_id(resource_id) or not self._owner(pod, profiles, request_bound):
            return False
        db.execute(
            "INSERT INTO rest_resources(resource_id) VALUES (?) ON CONFLICT(resource_id) DO UPDATE SET compute_absent=0",
            (resource_id,),
        )
        if request_bound and verified_read:
            # Request has no template/network/global volume option. Network volumes
            # attach at create and persist independently; Pod disks die with the Pod.
            db.execute(
                "INSERT OR IGNORE INTO rest_storage_proofs VALUES (?)", (resource_id,)
            )
        return True

    def _clean_resource(self, resource_id, client, profiles, request_bound):
        try:
            # Teardown reads may spend the provider budget's cleanup reserve.
            pod = client.get_pod(resource_id, priority=True)
            if pod is not None:
                owner = self._owner(pod, profiles, request_bound)
                with self.experiment._tx() as db:
                    if owner is None or not self._remember(
                        db, pod, profiles, request_bound, verified_read=True
                    ):
                        db.execute(
                            "DELETE FROM rest_storage_proofs WHERE resource_id=?",
                            (resource_id,),
                        )
                        return "ownership_unverified"
                client.terminate_owned(resource_id, owner, request_bound=request_bound)
                pod = client.get_pod(resource_id, priority=True)
            if pod is not None:
                with self.experiment._tx() as db:
                    if not self._remember(
                        db, pod, profiles, request_bound, verified_read=True
                    ):
                        db.execute(
                            "DELETE FROM rest_storage_proofs WHERE resource_id=?",
                            (resource_id,),
                        )
                        return "ownership_unverified"
                return "compute_cleanup_pending"
            with self.experiment._tx() as db:
                db.execute(
                    "UPDATE rest_resources SET compute_absent=1 WHERE resource_id=?",
                    (resource_id,),
                )
        except OwnershipError:
            with self.experiment._tx() as db:
                db.execute(
                    "DELETE FROM rest_storage_proofs WHERE resource_id=?",
                    (resource_id,),
                )
            return "ownership_unverified"
        except (ProviderError, ValueError):
            return "provider_request_failed"
        return None

    def reconcile_once(self, *, now, client=None):  # noqa: C901, PLR0912
        """One inventory scan, at most ten known-ID cleanup actions, no create replay."""
        self.experiment._time(now)
        with self.experiment._tx() as db:
            profiles = self._profiles(db)
            row = self.experiment._expire(db, self.experiment._row(db), now)
            if not row["stop_reason"]:
                return dict(row)
            if not profiles:
                # No request was ever bound, so no create could have been sent.
                if (
                    row["create_started"]
                    or db.execute("SELECT 1 FROM experiment_receipts").fetchone()
                ):
                    msg = "REST profile not configured"
                    raise ValueError(msg)
                db.execute(
                    "UPDATE experiment SET phase='terminated', outcome=?, cleanup_error=NULL, cleanup_attempts=cleanup_attempts+1 WHERE id=1",
                    (row["stop_reason"],),
                )
                return dict(self.experiment._row(db))
            current = db.execute("SELECT 1 FROM rest_profile").fetchone()
            # Retired requests were all request-bound creates.
            request_bound = (
                db.execute("SELECT 1 FROM rest_create_profile WHERE id=1").fetchone()
                is not None
                if current
                else True
            )
            db.execute(
                "UPDATE experiment SET phase='cleanup_pending', outcome=NULL, cleanup_error=?, cleanup_attempts=cleanup_attempts+1 WHERE id=1",
                ("dry_run" if client is None else "cleanup_pending",),
            )
            db.execute(
                "INSERT OR IGNORE INTO rest_resources(resource_id) SELECT resource_id FROM experiment_receipts"
            )
        if client is None:
            return self.experiment.status()

        error = None
        try:
            # Unfinished cleanup may use the reserve; terminated ledgers rescan
            # for late receipts from the ordinary budget.
            pods = client.list_pods(priority=row["phase"] != "terminated")
        except ProviderError:
            pods = []
            error = "provider_request_failed"
        with self.experiment._tx() as db:
            for pod in pods:
                self._remember(db, pod, profiles, request_bound)
            cursor = self.experiment._row(db)["cleanup_cursor"]
            sql = "SELECT resource_id FROM rest_resources WHERE compute_absent=0 AND resource_id>? ORDER BY resource_id LIMIT 10"
            resources = db.execute(sql, (cursor,)).fetchall()
            if not resources:
                resources = db.execute(sql, ("",)).fetchall()
            if resources:
                db.execute(
                    "UPDATE experiment SET cleanup_cursor=? WHERE id=1",
                    (resources[-1]["resource_id"],),
                )
        for resource in resources:
            result = self._clean_resource(
                resource["resource_id"], client, profiles, request_bound
            )
            error = error or result
        with self.experiment._tx() as db:
            known = db.execute("SELECT 1 FROM rest_resources LIMIT 1").fetchone()
            pending = db.execute(
                "SELECT 1 FROM rest_resources WHERE compute_absent=0 LIMIT 1"
            ).fetchone()
            storage_unknown = db.execute(
                "SELECT 1 FROM rest_resources r LEFT JOIN rest_storage_proofs p USING(resource_id) WHERE p.resource_id IS NULL LIMIT 1"
            ).fetchone()
            if not known and row["create_started"]:
                error = error or "create_unknown"
            if pending:
                error = error or "compute_cleanup_pending"
            if storage_unknown:
                error = error or "storage_unverified"
            if error:
                db.execute("UPDATE experiment SET cleanup_error=? WHERE id=1", (error,))
            else:
                db.execute(
                    "UPDATE experiment SET phase='terminated', outcome=?, cleanup_error=NULL WHERE id=1",
                    (row["stop_reason"],),
                )
        return self.experiment.status()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile one local REST ledger; dry-run by default"
    )
    parser.add_argument("database")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="allow REST cleanup requests, never create",
    )
    parser.add_argument(
        "--credential-env",
        help="explicit environment variable containing the bearer token",
    )
    args = parser.parse_args(argv)
    if args.execute and (
        not args.credential_env
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.credential_env)
    ):
        parser.error("--execute requires an explicit --credential-env variable name")
    if args.credential_env and not args.execute:
        parser.error("--credential-env requires --execute")
    try:
        cleaner = RESTCleanup(args.database)
        client = (
            RunpodREST(lambda: os.environ.get(args.credential_env))
            if args.execute
            else None
        )
        state = cleaner.reconcile_once(now=time.time(), client=client)
        report = {
            key: state[key]
            for key in (
                "phase",
                "stop_reason",
                "outcome",
                "cleanup_error",
                "cleanup_attempts",
            )
        }
        report["live_eligibility"] = dict.fromkeys(
            ("model", "hardware", "license", "inputs", "budget", "account", "launch"),
            False,
        )
        print(json.dumps(report, sort_keys=True))  # noqa: T201
    except (ValueError, sqlite3.Error):
        parser.exit(2, "invalid or unconfigured ledger\n")


if __name__ == "__main__":
    main()
