"""Default-off admission fencing and bounded database-clock drain leases.

Execution registrations outlive their admission transactions. The host still
refuses rollout handoff: its external upgrader cannot retain the lease safely.
"""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from uuid import uuid4

from django.db import connection
from django.db import transaction

from django_ergo.conversation.models import AdmissionDrain


def _locked():
    # The singleton is created by migration, avoiding an empty-table lock race.
    return AdmissionDrain.objects.select_for_update().get(pk=1)


def _now():
    # Read the database clock *after* waiting for the lock. PostgreSQL's
    # transaction timestamp may predate that wait by an arbitrary duration.
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]


def acquire(ttl: timedelta, maximum: timedelta):
    """Acquire a lease, returning its unguessable owner token or None.

    ``maximum`` is the fixed lifetime of this acquisition, including renewals.
    """
    if ttl <= timedelta(0) or maximum <= timedelta(0):
        raise ValueError("lease durations must be positive")  # noqa: EM101, TRY003
    with transaction.atomic():
        row = _locked()
        now = _now()
        if row.expires_at and row.expires_at > now:
            return None
        token = uuid4()
        row.owner = token
        row.deadline = now + maximum
        row.expires_at = min(now + ttl, row.deadline)
        row.save(update_fields=["owner", "deadline", "expires_at"])
        return token


def renew(token, ttl: timedelta) -> bool:
    if ttl <= timedelta(0):
        raise ValueError("lease duration must be positive")  # noqa: EM101, TRY003
    with transaction.atomic():
        row = _locked()
        now = _now()
        if (
            row.owner != token
            or not row.expires_at
            or row.expires_at <= now
            or row.deadline <= now
        ):
            return False
        row.expires_at = min(now + ttl, row.deadline)
        row.save(update_fields=["expires_at"])
        return True


def release(token) -> bool:
    """Release only the caller's lease, including an expired one."""
    with transaction.atomic():
        row = _locked()
        if row.owner != token or token is None:
            return False
        row.owner = row.expires_at = row.deadline = None
        row.save(update_fields=["owner", "expires_at", "deadline"])
        return True


def admit(register):
    """Atomically check the lease and register work in the same DB transaction.

    Returns ``(False, None)`` while draining, otherwise ``(True, result)``.
    ``register`` must durably register the claim in this database before it
    returns, and must not start work or do external I/O inside the transaction.
    """
    with transaction.atomic():
        row = _locked()
        if row.expires_at and row.expires_at > _now():
            return False, None
        return True, register()


# A separate, session-bound PostgreSQL connection holds each execution's shared
# lock until its finally block (or process death) closes the connection. A row
# transaction alone cannot cover the gap before StructuredCall/Worker creation.
_EXECUTION_LOCK = 782346219044


@dataclass
class _Registration:
    scope: str | None = None
    active: bool = True


_inside = ContextVar("admission_execution", default=None)


def enabled():
    from django_ergo.settings import api_settings

    return api_settings.ADMISSION_DRAIN_ENABLED


@contextmanager
def execution(*, inherit=False, continuing=False, scope=None):
    """Yield admission before consuming work; retain registration through execution.

    Nested runtime calls register separately and may continue an admitted turn.
    Inherited context is invalidated when its owner exits. Independent
    workers and deliveries always register afresh, even inside an admitted turn.
    Requires direct PostgreSQL sessions, not transaction-mode pooling.
    """
    if not enabled():
        yield True
        return
    holder = connection.copy()
    holder.settings_dict["OPTIONS"] = dict(holder.settings_dict.get("OPTIONS", {}))
    holder.settings_dict["OPTIONS"].pop("pool", None)
    previous = _inside.get()
    registration = None
    try:
        with transaction.atomic():
            row = _locked()
            # Revalidate inherited ownership after waiting for the row lock;
            # the outer execution may have ended while this admission waited.
            inherited = (
                inherit
                and previous is not None
                and previous.active
                and previous.scope == scope
            )
            admitted = (
                continuing
                or inherited
                or not (row.expires_at and row.expires_at > _now())
            )
            if admitted:
                with holder.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_lock_shared(%s)", [_EXECUTION_LOCK]
                    )
        if admitted:
            registration = _Registration(scope=scope)
            _inside.set(registration)
        yield admitted
    finally:
        if registration is not None:
            registration.active = False
            _inside.set(previous)
        holder.close()


def executions_active():
    """Observe all registered executions, including pre-call and local work."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [_EXECUTION_LOCK])
        return not cursor.fetchone()[0]


def sync_execution(*, deferred=None, wait=False, inherit=False):
    """Fence a synchronous consumer before it mutates its durable queue."""

    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            while True:
                with execution(inherit=inherit) as admitted:
                    if admitted:
                        return function(*args, **kwargs)
                if not wait:
                    return deferred
                time.sleep(0.1)

        return run

    return decorate


def async_execution(function):
    """Direct runtime calls wait without consuming approvals or creating calls."""
    from asgiref.sync import sync_to_async

    @wraps(function)
    async def run(*args, **kwargs):
        session = args[1] if len(args) > 1 else kwargs.get("session")
        scope = str(session.pk) if session is not None else None
        while True:
            guard = execution(inherit=True, scope=scope)
            try:
                admitted = await sync_to_async(guard.__enter__, thread_sensitive=True)()
                if admitted:
                    return await function(*args, **kwargs)
            finally:
                # Queued on the same serial executor even when cancellation hides
                # a successful enter; do not leak the session-bound registration.
                await asyncio.shield(
                    sync_to_async(guard.__exit__, thread_sensitive=True)(
                        None, None, None
                    )
                )
            await asyncio.sleep(0.1)

    return run
