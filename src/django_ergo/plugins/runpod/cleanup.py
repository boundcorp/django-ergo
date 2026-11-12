"""Independent, conservative REST cleanup for a single local experiment ledger.

This module does not authorize or perform a create. Provider ownership fields and
storage deletion semantics have not been verified against an authoritative API
contract; unmatched resources and storage liability remain unresolved.
"""

# This ledger adapter intentionally shares Experiment's transaction primitives.
# ruff: noqa: SLF001

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .experiment import Experiment
from .transport import Ownership
from .transport import OwnershipError
from .transport import ProviderError
from .transport import RunpodREST


class RESTCleanup:
    """Bridge a reserved experiment to REST; a sole supervisor invokes ticks."""

    def __init__(self, path):
        if not Path(path).is_file():
            msg = "existing ledger required"
            raise ValueError(msg)
        self.experiment = Experiment(path)

    def configure(self, ownership):
        """Permanently bind ownership before any create intent is recorded."""
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

    def reconcile_once(self, *, now, client=None):  # noqa: C901, PLR0912
        """Persist stop first, then use REST only if a client was explicitly supplied.

        An empty inventory is never proof that an ambiguous create failed. A Pod
        404 confirms compute absence only; storage semantics are not established.
        """
        self.experiment._time(now)
        with self.experiment._tx() as db:
            profile = self._profile(db)
            row = self.experiment._expire(db, self.experiment._row(db), now)
            if not row["stop_reason"]:
                return dict(row)
            db.execute(
                "UPDATE experiment SET phase='cleanup_pending', outcome=NULL, cleanup_error=?, cleanup_attempts=cleanup_attempts+1 WHERE id=1",
                ("dry_run" if client is None else "cleanup_pending",),
            )
            db.execute(
                "INSERT OR IGNORE INTO rest_resources(resource_id) SELECT resource_id FROM experiment_receipts"
            )
        if client is None:
            return self.experiment.status()

        try:
            pods = client.list_pods()
            with self.experiment._tx() as db:
                for pod in pods:
                    resource_id = pod.get("id")
                    if (
                        profile.matches(pod)
                        and isinstance(resource_id, str)
                        and resource_id.isascii()
                        and resource_id.isalnum()
                    ):
                        db.execute(
                            "INSERT INTO rest_resources(resource_id) VALUES (?) ON CONFLICT(resource_id) DO UPDATE SET compute_absent=0",
                            (resource_id,),
                        )
                cursor = db.execute(
                    "SELECT cleanup_cursor FROM experiment WHERE id=1"
                ).fetchone()[0]
                sql = "SELECT resource_id FROM rest_resources WHERE compute_absent=0 AND resource_id>? ORDER BY resource_id LIMIT 10"
                resources = db.execute(sql, (cursor,)).fetchall()
                if not resources:
                    resources = db.execute(sql, ("",)).fetchall()
                if resources:
                    db.execute(
                        "UPDATE experiment SET cleanup_cursor=? WHERE id=1",
                        (resources[-1]["resource_id"],),
                    )
            error = "storage_unverified" if resources else "create_unknown"
            for resource in resources:
                resource_id = resource["resource_id"]
                try:
                    pod = client.get_pod(resource_id)
                    if pod is not None:
                        if not profile.matches(pod):
                            error = "ownership_unverified"
                            continue
                        client.terminate_owned(resource_id, profile)
                        pod = client.get_pod(resource_id)
                    if pod is None:
                        with self.experiment._tx() as db:
                            db.execute(
                                "UPDATE rest_resources SET compute_absent=1 WHERE resource_id=?",
                                (resource_id,),
                            )
                    else:
                        error = "compute_cleanup_pending"
                except OwnershipError:
                    error = "ownership_unverified"
                except (ProviderError, ValueError):
                    error = "provider_request_failed"
            with self.experiment._tx() as db:
                if (
                    db.execute(
                        "SELECT 1 FROM rest_resources WHERE compute_absent=1 LIMIT 1"
                    ).fetchone()
                    and error == "create_unknown"
                ):
                    error = "storage_unverified"
                db.execute("UPDATE experiment SET cleanup_error=? WHERE id=1", (error,))
        except ProviderError:
            with self.experiment._tx() as db:
                db.execute(
                    "UPDATE experiment SET cleanup_error='provider_request_failed' WHERE id=1"
                )
        return self.experiment.status()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile one local REST ledger; dry-run by default"
    )
    parser.add_argument("database")
    parser.add_argument(
        "--execute", action="store_true", help="allow REST cleanup requests"
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
        print(  # noqa: T201
            json.dumps(
                {
                    key: state[key]
                    for key in (
                        "phase",
                        "stop_reason",
                        "outcome",
                        "cleanup_error",
                        "cleanup_attempts",
                    )
                },
                sort_keys=True,
            )
        )
    except (ValueError, sqlite3.Error):
        parser.exit(2, "invalid or unconfigured ledger\n")


if __name__ == "__main__":
    main()
