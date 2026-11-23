"""Database-fenced admission lease primitives; callers must opt in explicitly.

This module alone does not drain an Ergonaut deployment. Every work claim must
register under ``admit`` and the rollout must retain the lease through handoff
before a coordinator can use it to protect an upgrade.
"""

from datetime import timedelta
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
