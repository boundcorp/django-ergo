"""Local durable experiment queue, independent of any provider or worker.

This is not a rental controller or an authorization boundary. SQLite's locking
is suitable only for processes sharing the same reliable local database file;
it is not a substitute for the planned shared PostgreSQL resource ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from contextlib import contextmanager


class Conflict(ValueError):  # noqa: N818
    """An identity was reused with different immutable contents."""


class StaleLease(ValueError):  # noqa: N818
    """The lease no longer authorizes a queue transition."""


class Queue:
    def __init__(self, path):
        self.path = str(path)
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    spec TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    shot TEXT NOT NULL,
                    version TEXT NOT NULL,
                    phase INTEGER NOT NULL,
                    original_id INTEGER REFERENCES jobs(id),
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    epoch INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL,
                    submission_state TEXT NOT NULL DEFAULT 'not_sent',
                    submission_id TEXT NOT NULL UNIQUE,
                    result TEXT,
                    UNIQUE(project, request_key)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    job_id INTEGER NOT NULL REFERENCES jobs(id),
                    receipt TEXT NOT NULL,
                    PRIMARY KEY(job_id, receipt)
                );
                CREATE TABLE IF NOT EXISTS queue_control (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    stopped INTEGER NOT NULL DEFAULT 0,
                    queue_id TEXT NOT NULL
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO queue_control(id, queue_id) VALUES (1, ?)",
                (uuid.uuid4().hex,),
            )

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
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

    def append(  # noqa: PLR0913
        self,
        *,
        project,
        request_key,
        shot,
        version,
        phase,
        spec,
        original_id=None,
        reason=None,
    ):
        """Append immutable work; repeated identical keys return the same row.

        Phases 0/1/2 are the test, first chapter, and second chapter. A
        correction references an existing original, including its version,
        and takes priority over ordinary jobs within the same phase only.
        """
        if not all(
            isinstance(v, str) and v for v in (project, request_key, shot, version)
        ):
            msg = "project, request key, shot, and version are required"
            raise ValueError(msg)
        if type(phase) is not int or phase not in (0, 1, 2):
            msg = "phase must be 0, 1, or 2"
            raise ValueError(msg)
        if (original_id is None) != (reason is None) or (
            reason is not None and (not isinstance(reason, str) or not reason.strip())
        ):
            msg = "corrections require an original and reason"
            raise ValueError(msg)
        normalized = json.dumps(
            {
                "shot": shot,
                "version": version,
                "phase": phase,
                "spec": spec,
                "original_id": original_id,
                "reason": reason,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM jobs WHERE project=? AND request_key=?",
                (project, request_key),
            ).fetchone()
            if existing:
                if existing["spec_hash"] != digest:
                    msg = "request key already names a different specification"
                    raise Conflict(msg)
                return dict(existing)
            if db.execute("SELECT stopped FROM queue_control WHERE id=1").fetchone()[0]:
                msg = "queue is stopped"
                raise ValueError(msg)
            queue_id = db.execute(
                "SELECT queue_id FROM queue_control WHERE id=1"
            ).fetchone()[0]
            identity = hashlib.sha256(
                json.dumps(
                    [queue_id, project, request_key], separators=(",", ":")
                ).encode()
            ).hexdigest()
            if original_id is not None:
                original = db.execute(
                    "SELECT * FROM jobs WHERE id=?", (original_id,)
                ).fetchone()
                if (
                    not original
                    or original["project"] != project
                    or original["original_id"] is not None
                ):
                    msg = "correction must reference an original in the same project"
                    raise ValueError(msg)
                if (original["shot"], original["version"], original["phase"]) != (
                    shot,
                    version,
                    phase,
                ):
                    msg = "correction must identify the original shot and version in the original phase"
                    raise ValueError(msg)
            cur = db.execute(
                """INSERT INTO jobs
                (project, request_key, spec, spec_hash, shot, version, phase,
                 original_id, reason, submission_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project,
                    request_key,
                    normalized,
                    digest,
                    shot,
                    version,
                    phase,
                    original_id,
                    reason,
                    identity,
                ),
            )
            return dict(
                db.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone()
            )

    def claim(self, *, now, lease_seconds):
        """Claim one job or recover its expired lease; epochs fence stale owners."""
        if (
            not math.isfinite(now)
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            msg = "time must be finite and lease must be positive"
            raise ValueError(msg)
        with self._transaction() as db:
            if db.execute("SELECT stopped FROM queue_control WHERE id=1").fetchone()[0]:
                return None
            running = db.execute(
                "SELECT * FROM jobs WHERE status='running' ORDER BY id LIMIT 1"
            ).fetchone()
            if running and running["lease_until"] > now:
                return None
            job = (
                running
                or db.execute("""SELECT * FROM jobs WHERE status='queued'
                ORDER BY phase, CASE WHEN original_id IS NULL THEN 1 ELSE 0 END, id LIMIT 1""").fetchone()
            )
            if job is None:
                return None
            db.execute(
                "UPDATE jobs SET status='running', epoch=epoch+1, lease_until=? WHERE id=?",
                (now + lease_seconds, job["id"]),
            )
            return dict(
                db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            )

    @staticmethod
    def _leased(db, job_id, epoch, now):
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (
            not math.isfinite(now)
            or not job
            or job["status"] != "running"
            or job["epoch"] != epoch
            or job["lease_until"] <= now
        ):
            msg = "job is cancelled, terminal, or lease expired"
            raise StaleLease(msg)
        return job

    def prepare_submission(self, job_id, epoch, now):
        """Persist uncertainty before a send; unknown must be inspected first."""
        with self._transaction() as db:
            job = self._leased(db, job_id, epoch, now)
            if job["submission_state"] != "not_sent":
                msg = "inspect the stable submission identity before any retry"
                raise Conflict(msg)
            db.execute(
                "UPDATE jobs SET submission_state='unknown' WHERE id=?", (job_id,)
            )
            return job["submission_id"]

    def record_receipt(self, job_id, receipt):
        """Retain even a stale owner's late receipt, without advancing job state."""
        if not isinstance(receipt, str) or not receipt:
            msg = "receipt required"
            raise ValueError(msg)
        with self._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO receipts(job_id, receipt) VALUES (?, ?)",
                (job_id, receipt),
            )

    def observe_submission(self, job_id, epoch, now, *, state):
        """Apply an authoritative inspection: accepted, or positively absent.

        Only a runner that guarantees stable identities/tombstones can prove
        absence. A timeout or eventually consistent missing result is not proof.
        """
        if state not in ("accepted", "absent"):
            msg = "inspection must establish accepted or positive absence"
            raise ValueError(msg)
        with self._transaction() as db:
            job = self._leased(db, job_id, epoch, now)
            if job["submission_state"] != "unknown":
                msg = "no uncertain submission to inspect"
                raise Conflict(msg)
            db.execute(
                "UPDATE jobs SET submission_state=? WHERE id=?",
                ("accepted" if state == "accepted" else "not_sent", job_id),
            )

    def finish(self, job_id, epoch, now, *, status, result=None):
        if status not in ("succeeded", "failed"):
            msg = "finish requires succeeded or failed"
            raise ValueError(msg)
        encoded = json.dumps(result, sort_keys=True, allow_nan=False)
        with self._transaction() as db:
            job = self._leased(db, job_id, epoch, now)
            if status == "succeeded" and job["submission_state"] != "accepted":
                msg = "success requires a confirmed runner submission"
                raise Conflict(msg)
            db.execute(
                "UPDATE jobs SET status=?, result=?, lease_until=NULL WHERE id=?",
                (status, encoded, job_id),
            )

    def cancel(self):
        """Persist monotonic stop, cancelling queued and running work atomically."""
        with self._transaction() as db:
            db.execute("UPDATE queue_control SET stopped=1 WHERE id=1")
            db.execute(
                "UPDATE jobs SET status='cancelled', lease_until=NULL WHERE status IN ('queued','running')"
            )

    def jobs(self):
        with self._transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")]

    def receipts(self, job_id):
        with self._transaction() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT receipt FROM receipts WHERE job_id=? ORDER BY receipt",
                    (job_id,),
                )
            ]
