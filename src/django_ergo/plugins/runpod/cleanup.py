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
            """)

    def configure(self, ownership):
        """Bind cleanup-only ownership; this cannot establish storage absence."""
        self._configure(ownership, None)

    def configure_create(  # noqa: PLR0913
        self,
        *,
        account,
        image,
        container_disk_gb,
        gpu_type,
        data_center_ids,
        quoted_hourly_usd=None,
        ssh_public_key=None,
    ):
        """Bind a digest, explicit location allowlist and existing absolute clock.

        Binding a profile alone does not authorize live create.
        Omitting networkVolumeId (and any template) deliberately attaches none.
        """
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
            if (
                not quote.is_finite()
                or quote < 0
                or quote != admitted
                or evidence["verified"] is not True
            ):
                msg = "compute quote does not match admission"
                raise ValueError(msg)
        body = {
            "name": "ergo-" + attempt,
            "computeType": "GPU",
            "cloudType": "SECURE",
            "gpuTypeIds": [gpu_type],
            "gpuCount": 1,
            "minRAMPerGPU": 167,
            "minVCPUPerGPU": 10,
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
            access = create_access(ssh_public_key)
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

    def create_once(self, *, now, client=None, live_execute=False):
        """Commit uncertainty before the sole POST; never replay on failure."""
        self.experiment._time(now)
        if client is not None and not isinstance(client, FixtureRunpodREST):
            if not isinstance(client, RunpodREST) or live_execute is not True:
                msg = "create requires an offline fixture client; live create disabled"
                raise ValueError(msg)
            from .supervisor import CleanerSupervision

            CleanerSupervision(self.experiment.path).require_fresh(now=now)
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
        except ProviderError:
            with self.experiment._tx() as db:
                db.execute(
                    "UPDATE experiment SET cleanup_error='provider_request_failed' WHERE id=1"
                )
        return self.experiment.status()

    def _remember(self, db, pod, profile, request_bound, *, verified_read=False):
        resource_id = pod.get("id")
        if not _pod_id(resource_id) or not profile.matches(
            pod, request_bound=request_bound
        ):
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

    def _clean_resource(self, resource_id, client, profile, request_bound):
        try:
            pod = client.get_pod(resource_id)
            if pod is not None:
                with self.experiment._tx() as db:
                    if not self._remember(
                        db, pod, profile, request_bound, verified_read=True
                    ):
                        db.execute(
                            "DELETE FROM rest_storage_proofs WHERE resource_id=?",
                            (resource_id,),
                        )
                        return "ownership_unverified"
                client.terminate_owned(
                    resource_id, profile, request_bound=request_bound
                )
                pod = client.get_pod(resource_id)
            if pod is not None:
                with self.experiment._tx() as db:
                    if not self._remember(
                        db, pod, profile, request_bound, verified_read=True
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

    def reconcile_once(self, *, now, client=None):  # noqa: C901
        """One inventory scan, at most ten known-ID cleanup actions, no create replay."""
        self.experiment._time(now)
        with self.experiment._tx() as db:
            profile = self._profile(db)
            row = self.experiment._expire(db, self.experiment._row(db), now)
            if not row["stop_reason"]:
                return dict(row)
            request_bound = (
                db.execute("SELECT 1 FROM rest_create_profile WHERE id=1").fetchone()
                is not None
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
            pods = client.list_pods()
        except ProviderError:
            pods = []
            error = "provider_request_failed"
        with self.experiment._tx() as db:
            for pod in pods:
                self._remember(db, pod, profile, request_bound)
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
                resource["resource_id"], client, profile, request_bound
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
