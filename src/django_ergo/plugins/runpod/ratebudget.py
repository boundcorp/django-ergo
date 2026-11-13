"""Durable RunPod REST request budget shared by every role using one API key.

RunPod limits each API key across all REST endpoints (observed November 2026:
11/minute, 225/hour, 2700/day, published in the ``ratelimit`` and
``ratelimit-policy`` response headers). The controller Worker (bot-tasks) and
independent maintenance are separate processes, so the budget lives in one
SQLite file on the shared rental mount, keyed by credential name.

Ordinary reads stop ``RESERVE`` short of each limit; cleanup (DELETE and its
ownership reads) may spend the reserve. The provider's own remaining counts are
honored until their reset, so calls made by other processes on the same key are
accounted for. A 429 blocks every caller until Retry-After plus jitter. Nothing
sleeps: a refused acquire raises ``RateLimitError`` with the wait, and callers
keep their existing retry state.
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .transport import RateLimitError

LIMITS = {"minute": (60, 11), "hour": (3600, 225), "day": (86400, 2700)}
RESERVE = {"minute": 3, "hour": 30, "day": 100}
JITTER_SECONDS = 5
_HEADER = re.compile(r'"(minute|hour|day)"\s*;\s*r=(\d+)\s*;\s*t=(\d+)')


def budget_path(root, credential):
    return Path(root) / f".rest-budget-{credential}.sqlite3"


class RestBudget:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self._clock = clock

    @contextmanager
    def _tx(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS calls (at REAL NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id=1),"
                " blocked_until REAL NOT NULL, provider TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO state VALUES (1, 0, '{}')")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def acquire(self, *, priority=False):
        """Record one request, or raise RateLimitError without making it."""
        now = self._clock()
        with self._tx() as db:
            db.execute("DELETE FROM calls WHERE at <= ?", (now - 86400,))
            blocked, provider = db.execute(
                "SELECT blocked_until, provider FROM state WHERE id=1"
            ).fetchone()
            waits = [blocked - now] if blocked > now else []
            provider = json.loads(provider)
            for window, (seconds, limit) in LIMITS.items():
                floor = 0 if priority else RESERVE[window]
                used, oldest = db.execute(
                    "SELECT COUNT(*), MIN(at) FROM calls WHERE at > ?", (now - seconds,)
                ).fetchone()
                if used >= limit - floor:
                    waits.append(oldest + seconds - now)
                seen = provider.get(window)
                if seen and seen["reset"] > now:
                    since = db.execute(
                        "SELECT COUNT(*) FROM calls WHERE at >= ?", (seen["observed"],)
                    ).fetchone()[0]
                    if seen["remaining"] - since <= floor:
                        waits.append(seen["reset"] - now)
            if waits:
                message = "provider request budget exhausted"
                raise RateLimitError(message, max(1, math.ceil(max(waits))))
            db.execute("INSERT INTO calls VALUES (?)", (now,))

    def observe(self, headers, status):
        """Persist the provider's remaining counts and any 429 Retry-After."""
        now = self._clock()
        text = headers.get("ratelimit", "") if headers is not None else ""
        seen = {
            window: {"remaining": int(r), "reset": now + int(t), "observed": now}
            for window, r, t in _HEADER.findall(text or "")
        }
        retry = None
        if status == 429:  # noqa: PLR2004 -- HTTP Too Many Requests
            try:
                retry = min(max(int(headers.get("Retry-After", "")), 1), 3600)
            except (AttributeError, TypeError, ValueError):
                retry = 60
        if not seen and retry is None:
            return
        with self._tx() as db:
            if seen:
                current = json.loads(
                    db.execute("SELECT provider FROM state WHERE id=1").fetchone()[0]
                )
                db.execute(
                    "UPDATE state SET provider=? WHERE id=1",
                    (json.dumps({**current, **seen}),),
                )
            if retry is not None:
                until = now + retry + random.uniform(0, JITTER_SECONDS)
                db.execute(
                    "UPDATE state SET blocked_until=MAX(blocked_until, ?) WHERE id=1",
                    (until,),
                )
