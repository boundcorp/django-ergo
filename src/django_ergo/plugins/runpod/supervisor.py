"""Local independent cleaner supervision; no installation or create capability.

The worker and supervisor share Experiment's durable ledger, not credentials or
process lifetime. This is local operational preparation, not launch approval or
a guarantee about provider billing, availability, or simultaneous host failure.
"""

# This ledger adapter intentionally shares Experiment's transaction primitives.
# ruff: noqa: SLF001

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .cleanup import RESTCleanup
from .experiment import Experiment

MAX_RENTAL_SECONDS = 3600


class CleanerSupervision:
    """Persist optional worker freshness gating in the existing REST ledger."""

    def __init__(self, path):
        if not Path(path).is_file():
            msg = "existing ledger required"
            raise ValueError(msg)
        self.path = str(Path(path).resolve())
        self.experiment = Experiment(self.path)

    def configure(
        self, *, interval_seconds=1, operation_timeout=5, freshness_seconds=10
    ):
        values = (interval_seconds, operation_timeout, freshness_seconds)
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
                for value in values
            )
            or freshness_seconds <= interval_seconds + operation_timeout
        ):
            msg = "positive timings required; freshness must exceed interval plus operation timeout"
            raise ValueError(msg)
        cleaner = RESTCleanup(self.path)
        with self.experiment._tx() as db:
            cleaner._profile(db)
            row = self.experiment._row(db)
            if not 0 < row["deadline"] - row["created"] <= MAX_RENTAL_SECONDS:
                msg = (
                    "supervision requires an immutable rental deadline within one hour"
                )
                raise ValueError(msg)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cleaner_supervision (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    interval_seconds REAL NOT NULL,
                    operation_timeout REAL NOT NULL,
                    freshness_seconds REAL NOT NULL,
                    heartbeat REAL, process_id INTEGER, tick_error TEXT,
                    executing INTEGER NOT NULL DEFAULT 0
                )
            """)
            previous = db.execute(
                "SELECT interval_seconds, operation_timeout, freshness_seconds FROM cleaner_supervision WHERE id=1"
            ).fetchone()
            if previous is not None and tuple(previous) != values:
                msg = "supervision timings already bound; restart with the same configuration"
                raise ValueError(msg)
            db.execute(
                "INSERT OR IGNORE INTO cleaner_supervision(id, interval_seconds, operation_timeout, freshness_seconds) VALUES (1,?,?,?)",
                values,
            )

    @staticmethod
    def _configuration(db):
        try:
            row = db.execute("SELECT * FROM cleaner_supervision WHERE id=1").fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            msg = "independent cleaner not configured"
            raise ValueError(msg)
        return row

    def require_fresh(self, *, now, require_execute=True):
        """Gate worker create/shot admission; never renew a work or rental lease.

        Invoke immediately before each worker operation, not just once at startup.
        The cleaner itself does not use this gate: it must clean stale workers.
        """
        self.experiment._time(now)
        reason = None
        with self.experiment._tx() as db:
            state = self.experiment._expire(db, self.experiment._row(db), now)
            config = self._configuration(db)
            if state["stop_reason"]:
                reason = "experiment stopped"
            elif config["process_id"] == os.getpid():
                reason = "cleaner must be an independent process"
            elif require_execute and not config["executing"]:
                reason = "independent cleaner heartbeat is dry-run only"
            elif config["tick_error"]:
                reason = "independent cleaner tick has failed"
            elif (
                config["heartbeat"] is None
                or not 0 <= now - config["heartbeat"] < config["freshness_seconds"]
            ):
                reason = "independent cleaner heartbeat missing or stale"
        # Raise outside the transaction so deadline/worker-loss intent persists.
        if reason:
            raise ValueError(reason)
        return True

    def tick(self, *, now, execute=False, credential_env=None, run=subprocess.run):
        """Commit a heartbeat/expiry, then bound one existing cleanup command.

        Only an explicitly named environment callback in cleanup obtains tokens.
        Neither command arguments nor this table contain the credential value.
        """
        self.experiment._time(now)
        if execute and (
            not isinstance(credential_env, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential_env)
        ):
            msg = "execute requires an explicit credential environment variable name"
            raise ValueError(msg)
        if credential_env is not None and not execute:
            msg = "credential environment requires execute"
            raise ValueError(msg)
        with self.experiment._tx() as db:
            config = self._configuration(db)
            state = self.experiment._expire(db, self.experiment._row(db), now)
            db.execute(
                "UPDATE cleaner_supervision SET heartbeat=?, process_id=?, tick_error=NULL, executing=? WHERE id=1",
                (now, os.getpid(), int(execute)),
            )
        if not state["stop_reason"]:
            return self.report()
        command = [
            sys.executable,
            "-m",
            "django_ergo.plugins.runpod.cleanup",
            self.path,
        ]
        if execute:
            command.extend(["--execute", "--credential-env", credential_env])
        error = None
        try:
            result = run(
                command,
                timeout=config["operation_timeout"],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                error = "cleanup_process_failed"
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps its direct child before raising.
            # Requests may already have succeeded: keep all receipts and reconcile.
            error = "cleanup_operation_timeout"
        except OSError:
            error = "cleanup_process_failed"
        if error:
            with self.experiment._tx() as db:
                db.execute(
                    "UPDATE cleaner_supervision SET tick_error=? WHERE id=1", (error,)
                )
                db.execute(
                    "UPDATE experiment SET cleanup_error=? WHERE id=1 AND phase!='terminated'",
                    (error,),
                )
        return self.report()

    def report(self):
        with self.experiment._tx() as db:
            state = self.experiment._row(db)
            config = self._configuration(db)
            return {
                **{
                    key: state[key]
                    for key in (
                        "phase",
                        "stop_reason",
                        "outcome",
                        "cleanup_error",
                        "cleanup_attempts",
                    )
                },
                "heartbeat": config["heartbeat"],
                "tick_error": config["tick_error"],
            }

    def delay(self, *, now):
        """Wake at the earliest persisted deadline, not after a whole idle tick."""
        self.experiment._time(now)
        with self.experiment._tx() as db:
            config = self._configuration(db)
            state = self.experiment._row(db)
            delay = config["interval_seconds"]
            if not state["stop_reason"]:
                for key in ("deadline", "work_deadline", "idle_deadline"):
                    if state[key] is not None:
                        delay = min(delay, max(0, state[key] - now))
            return delay

    def run_forever(self, *, execute=False, credential_env=None):
        # Advisory singleton only on this local host/filesystem; not a distributed
        # lease. Keep monitoring even after termination for durable late receipts.
        with Path(self.path + ".cleaner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                msg = "another cleaner supervisor holds this ledger"
                raise ValueError(msg) from None
            while True:
                self.tick(
                    now=time.time(), execute=execute, credential_env=credential_env
                )
                time.sleep(self.delay(now=time.time()))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run an independent local REST cleaner; dry-run by default"
    )
    parser.add_argument("database")
    parser.add_argument("--interval-seconds", type=float, default=1)
    parser.add_argument("--operation-timeout", type=float, default=5)
    parser.add_argument("--freshness-seconds", type=float, default=10)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--credential-env")
    parser.add_argument(
        "--check-fresh", action="store_true", help="check worker admission and exit"
    )
    args = parser.parse_args(argv)
    if args.execute and (
        not args.credential_env
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.credential_env)
    ):
        parser.error("--execute requires an explicit --credential-env variable name")
    if args.credential_env and not args.execute:
        parser.error("--credential-env requires --execute")
    if args.check_fresh and args.execute:
        parser.error("--check-fresh does not execute cleanup")
    try:
        supervisor = CleanerSupervision(args.database)
        if args.check_fresh:
            supervisor.require_fresh(now=time.time())
            print(json.dumps({"cleaner_fresh": True}))  # noqa: T201
            return
        supervisor.configure(
            interval_seconds=args.interval_seconds,
            operation_timeout=args.operation_timeout,
            freshness_seconds=args.freshness_seconds,
        )
        supervisor.run_forever(execute=args.execute, credential_env=args.credential_env)
    except (ValueError, sqlite3.Error, OSError):
        parser.exit(2, "invalid ledger, cleaner configuration, or stale heartbeat\n")
    except KeyboardInterrupt:
        parser.exit(130, "cleaner stopped; worker freshness will expire\n")


if __name__ == "__main__":
    main()
