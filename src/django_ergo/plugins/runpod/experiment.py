"""Singleton rental ledger and independently invocable fake cleanup.

The fake provider and ledger share one local SQLite file so fake operations are
atomic; those semantics must not be assumed of a real provider. The separate
REST controller uses this ledger but is not an authorization boundary. An
external scheduler must invoke cleanup independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict

from .budget import admit_rental


class Experiment:
    """One lifetime rental intent per SQLite file; never replace an attempt."""

    def __init__(self, path):
        self.path = str(path)
        self._send_capability = None
        with self._tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experiment (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    token TEXT NOT NULL UNIQUE, marker TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL, deadline REAL NOT NULL,
                    idle_grace REAL NOT NULL, idle_deadline REAL,
                    work_deadline REAL, phase TEXT NOT NULL, stop_reason TEXT,
                    outcome TEXT, reservation TEXT NOT NULL, evidence TEXT NOT NULL,
                    cleanup_error TEXT, cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                    create_started INTEGER NOT NULL DEFAULT 0,
                    cleanup_cursor TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS experiment_receipts (
                    resource_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS experiment_exports (
                    name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL, metadata TEXT NOT NULL, content BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fake_resources (
                    resource_id TEXT PRIMARY KEY, marker TEXT NOT NULL,
                    compute INTEGER NOT NULL, storage INTEGER NOT NULL,
                    compute_failures INTEGER NOT NULL DEFAULT 0,
                    storage_failures INTEGER NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _time(now):
        if (
            isinstance(now, bool)
            or not isinstance(now, float | int)
            or not math.isfinite(now)
        ):
            msg = "finite timestamp required"
            raise ValueError(msg)

    @staticmethod
    def _row(db):
        row = db.execute("SELECT * FROM experiment WHERE id=1").fetchone()
        if row is None:
            msg = "no experiment intent"
            raise ValueError(msg)
        return row

    @staticmethod
    def _stop(db, reason):
        db.execute(
            "UPDATE experiment SET stop_reason=COALESCE(stop_reason, ?) WHERE id=1",
            (reason,),
        )

    @classmethod
    def _expire(cls, db, row, now):
        if now >= row["deadline"]:
            cls._stop(db, "deadline")
        elif row["work_deadline"] is not None and now >= row["work_deadline"]:
            cls._stop(db, "worker_lost")
        elif row["idle_deadline"] is not None and now >= row["idle_deadline"]:
            cls._stop(db, "idle")
        queue_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_control'"
        ).fetchone()
        if queue_exists:
            control = db.execute(
                "SELECT stopped FROM queue_control WHERE id=1"
            ).fetchone()
            if control is None or control["stopped"]:
                cls._stop(db, "cancelled")
        return cls._row(db)

    @classmethod
    def _running(cls, db, now):
        row = cls._expire(db, cls._row(db), now)
        if row["stop_reason"]:
            # Caller must not roll back the newly persisted stop intent.
            return None
        return row

    def reserve(self, *, now, idle_grace, eligibility, **budget):
        """Persist authorization and start the immutable rental clock before create.

        Eligibility records operator-supplied facts, not independent
        authorization. A license review can be recorded as delegated rather
        than falsely marked complete. Budget is the admit_rental argument set.
        """
        self._time(now)
        if (
            isinstance(idle_grace, bool)
            or not isinstance(idle_grace, int | float)
            or not math.isfinite(idle_grace)
            or idle_grace <= 0
        ):
            msg = "positive finite idle grace required"
            raise ValueError(msg)
        required = {"model", "hardware", "license", "inputs", "budget"}
        if (
            not isinstance(eligibility, dict)
            or set(eligibility) != required
            or any(eligibility[key] is not True for key in required - {"license"})
            or (
                eligibility["license"] is not True
                and eligibility["license"] != "delegated"
            )
        ):
            msg = "missing or unverified eligibility"
            raise ValueError(msg)
        admission = admit_rental(**budget)
        if not admission.allowed:
            raise ValueError(admission.reason)
        evidence = json.dumps(
            {
                "eligibility": eligibility,
                "budget": {
                    k: asdict(v) if hasattr(v, "__dataclass_fields__") else str(v)
                    for k, v in budget.items()
                },
            },
            default=str,
            sort_keys=True,
        )
        token, marker = uuid.uuid4().hex, uuid.uuid4().hex
        with self._tx() as db:
            if db.execute("SELECT 1 FROM experiment WHERE id=1").fetchone():
                msg = "singleton rental already reserved"
                raise ValueError(msg)
            db.execute(
                """INSERT INTO experiment
                (id,token,marker,created,deadline,idle_grace,phase,reservation,evidence)
                VALUES (1,?,?,?,?,?,'reserved',?,?)""",
                (
                    token,
                    marker,
                    now,
                    now + budget["timeline"].rental_seconds,
                    idle_grace,
                    str(admission.reservation_usd),
                    evidence,
                ),
            )
        return self.status()

    def begin_create(self, *, now):
        """Commit uncertainty before a one-time create; never replay this call."""
        self._time(now)
        with self._tx() as db:
            row = self._running(db, now)
            if row is None:
                return None
            if row["phase"] != "reserved":
                msg = "create already attempted; inspect instead of replay"
                raise ValueError(msg)
            db.execute(
                "UPDATE experiment SET phase='create_unknown', create_started=1 WHERE id=1"
            )
        self._send_capability = row["token"]
        return row["token"]

    def fake_create(self, token, *, now):
        """One atomic fake send, never available for a real provider."""
        self._time(now)
        if self._send_capability != token or token is None:
            msg = "create capability lost or already consumed; inspect instead"
            raise ValueError(msg)
        self._send_capability = None
        with self._tx() as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rest_profile'"
            ).fetchone():
                msg = "fake create is unavailable for a REST ledger"
                raise ValueError(msg)
        with self._tx() as db:
            row = self._running(db, now)
            if row is None:
                return None
            if row["phase"] != "create_unknown" or token != row["token"]:
                msg = "invalid create intent"
                raise ValueError(msg)
            if db.execute(
                "SELECT 1 FROM fake_resources WHERE marker=?", (row["marker"],)
            ).fetchone():
                msg = "create already sent"
                raise ValueError(msg)
            resource_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO fake_resources(resource_id,marker,compute,storage) VALUES (?,?,1,1)",
                (resource_id, row["marker"]),
            )
            return resource_id

    def receipt(self, resource_id):
        """Keep late receipts, including after stop or a prior absence observation."""
        if not isinstance(resource_id, str) or not resource_id:
            msg = "resource id required"
            raise ValueError(msg)
        with self._tx() as db:
            self._row(db)
            db.execute(
                "INSERT OR IGNORE INTO experiment_receipts VALUES (?)", (resource_id,)
            )
            db.execute(
                "UPDATE experiment SET phase='cleanup_pending', outcome=NULL WHERE stop_reason IS NOT NULL"
            )

    def ready(self, *, now):
        self._time(now)
        with self._tx() as db:
            row = self._running(db, now)
            if row is None:
                return False
            owned = db.execute(
                "SELECT 1 FROM fake_resources WHERE marker=? AND compute=1",
                (row["marker"],),
            ).fetchone()
            if row["phase"] != "create_unknown" or not owned:
                msg = "no owned fake compute"
                raise ValueError(msg)
            db.execute(
                "UPDATE experiment SET phase='ready', idle_deadline=? WHERE id=1",
                (now + row["idle_grace"],),
            )
            return True

    def work_started(self, *, now, lease_seconds):
        """Record actual work, with a fixed nonrenewable worker lease."""
        self._time(now)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int | float)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            msg = "positive finite work lease required"
            raise ValueError(msg)
        with self._tx() as db:
            row = self._running(db, now)
            if row is None:
                return False
            if row["phase"] != "ready" or row["work_deadline"] is not None:
                msg = "work already active or not ready"
                raise ValueError(msg)
            db.execute(
                "UPDATE experiment SET idle_deadline=NULL, work_deadline=? WHERE id=1",
                (min(now + lease_seconds, row["deadline"]),),
            )
            return True

    def work_finished(self, *, now):
        self._time(now)
        with self._tx() as db:
            row = self._running(db, now)
            if row is None:
                return False
            if row["work_deadline"] is None:
                msg = "no work in progress"
                raise ValueError(msg)
            db.execute(
                "UPDATE experiment SET work_deadline=NULL, idle_deadline=? WHERE id=1",
                (now + row["idle_grace"],),
            )
            return True

    def export(self, *, now, name, content, metadata, retained=False):
        """Persist immutable off-Pod bytes and metadata.

        ``retained`` archives bytes already durable on the controller host after
        stop; it authorizes no provider operation or continuation of work.
        """
        self._time(now)
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(content, bytes)
            or not content
            or not isinstance(metadata, dict)
            or not metadata
        ):
            msg = "named nonempty bytes and metadata required"
            raise ValueError(msg)
        encoded = json.dumps(metadata, sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(content).hexdigest()
        with self._tx() as db:
            row = self._row(db) if retained else self._running(db, now)
            if row is None:
                return False
            if not retained and row["phase"] != "ready":
                msg = "export requires ready compute"
                raise ValueError(msg)
            previous = db.execute(
                "SELECT * FROM experiment_exports WHERE name=?", (name,)
            ).fetchone()
            values = (name, digest, len(content), encoded, content)
            if previous and tuple(previous) != values:
                msg = "export identity conflict"
                raise ValueError(msg)
            db.execute(
                "INSERT OR IGNORE INTO experiment_exports VALUES (?,?,?,?,?)", values
            )
            return True

    def finish(self, *, now):
        self._time(now)
        with self._tx() as db:
            row = self._running(db, now)
            if row is None:
                return False
            if row["phase"] != "ready":
                msg = "success requires ready compute"
                raise ValueError(msg)
            if not db.execute("SELECT 1 FROM experiment_exports LIMIT 1").fetchone():
                msg = "durable export required"
                raise ValueError(msg)
            self._stop(db, "success")
            return True

    def stop(self, reason="cancelled"):
        if reason not in {"cancelled", "failed", "worker_lost"}:
            msg = "invalid stop reason"
            raise ValueError(msg)
        with self._tx() as db:
            self._row(db)
            self._stop(db, reason)

    def status(self):
        with self._tx() as db:
            return dict(self._row(db))

    @staticmethod
    def _cleanup_resource(db, resource):
        resource_id = resource["resource_id"]
        if resource["compute"]:
            if resource["compute_failures"]:
                db.execute(
                    "UPDATE fake_resources SET compute_failures=compute_failures-1 WHERE resource_id=?",
                    (resource_id,),
                )
                return "compute_cleanup_failed"
            db.execute(
                "UPDATE fake_resources SET compute=0 WHERE resource_id=?",
                (resource_id,),
            )
        if resource["storage"]:
            if resource["storage_failures"]:
                db.execute(
                    "UPDATE fake_resources SET storage_failures=storage_failures-1 WHERE resource_id=?",
                    (resource_id,),
                )
                return "storage_cleanup_failed"
            db.execute(
                "UPDATE fake_resources SET storage=0 WHERE resource_id=?",
                (resource_id,),
            )
        return None

    def reconcile_once(self, *, now):
        """One bounded fake cleanup pass; unresolved rows remain due on every tick.

        A missing inventory match while create is unknown never proves absence.
        A receipt does not establish ownership. The fake DB is the only provider.
        """
        self._time(now)
        with self._tx() as db:
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rest_profile'"
            ).fetchone():
                msg = "use REST cleanup for a REST ledger"
                raise ValueError(msg)
            row = self._expire(db, self._row(db), now)
            if not row["stop_reason"]:
                return dict(row)
            db.execute(
                "UPDATE experiment SET cleanup_attempts=cleanup_attempts+1, phase='cleanup_pending', cleanup_error=NULL, outcome=NULL WHERE id=1"
            )
            sql = "SELECT * FROM fake_resources WHERE marker=? AND (compute=1 OR storage=1) AND resource_id>? ORDER BY resource_id LIMIT 10"
            resources = db.execute(
                sql, (row["marker"], row["cleanup_cursor"])
            ).fetchall()
            if not resources:
                resources = db.execute(sql, (row["marker"], "")).fetchall()
            if resources:
                db.execute(
                    "UPDATE experiment SET cleanup_cursor=? WHERE id=1",
                    (resources[-1]["resource_id"],),
                )
            conflicts = db.execute(
                """SELECT r.resource_id FROM experiment_receipts r
                LEFT JOIN fake_resources f ON f.resource_id=r.resource_id
                WHERE f.resource_id IS NULL OR f.marker != ? LIMIT 1""",
                (row["marker"],),
            ).fetchone()
            error = "ownership_unverified" if conflicts else None
            for resource in resources:
                result = self._cleanup_resource(db, resource)
                error = error or result
            # A missing match after create was authorized is not proof of absence.
            known = db.execute(
                "SELECT 1 FROM fake_resources WHERE marker=? LIMIT 1", (row["marker"],)
            ).fetchone()
            remaining = db.execute(
                "SELECT 1 FROM fake_resources WHERE marker=? AND (compute=1 OR storage=1) LIMIT 1",
                (row["marker"],),
            ).fetchone()
            if not known and row["create_started"]:
                error = error or "create_unknown"
            if remaining:
                error = error or "cleanup_pending"
            if error:
                db.execute("UPDATE experiment SET cleanup_error=? WHERE id=1", (error,))
            else:
                outcome = (
                    "succeeded"
                    if row["stop_reason"] == "success"
                    else row["stop_reason"]
                )
                db.execute(
                    "UPDATE experiment SET phase='terminated', outcome=? WHERE id=1",
                    (outcome,),
                )
            return dict(self._row(db))


def main():
    parser = argparse.ArgumentParser(
        description="Run one offline fake-provider cleanup tick"
    )
    parser.add_argument("database", help="local SQLite queue/experiment file")
    args = parser.parse_args()
    print(  # noqa: T201
        json.dumps(
            Experiment(args.database).reconcile_once(now=time.time()), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
