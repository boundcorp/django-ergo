"""Payload-free lifecycle observations, not attribution of the actor who ended a call."""

import json
import logging
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from ergonaut.apps.bots.models import RealtimeVoiceEvent, RealtimeVoiceSession

logger = logging.getLogger(__name__)

# Unknown provider/SDK values are deliberately not retained, even if short.
REASONS = {
    "provider_closed": {"close_requested", "expired", "content", "remote_hangup", "connection_lost"},
    "agent_session_closed": {"error", "job_shutdown", "participant_disconnected", "user_initiated", "task_completed"},
    "bridge_exit": {
        "owner_disconnected",
        "room_disconnected",
        "agent_session_closed",
        "lease_rejected",
        "session_ended",
        "standby",
        "exception",
        "cancelled",
    },
    "session_ended": {"explicit_end", "bridge_closed", "lease_expired", "initialization_failed"},
}
RTC_REASONS = (
    "UNKNOWN_REASON",
    "CLIENT_INITIATED",
    "DUPLICATE_IDENTITY",
    "SERVER_SHUTDOWN",
    "PARTICIPANT_REMOVED",
    "ROOM_DELETED",
    "STATE_MISMATCH",
    "JOIN_FAILURE",
    "MIGRATION",
    "SIGNAL_CLOSE",
    "ROOM_CLOSED",
    "USER_UNAVAILABLE",
    "USER_REJECTED",
    "SIP_TRUNK_FAILURE",
    "CONNECTION_TIMEOUT",
    "MEDIA_FAILURE",
    "AGENT_ERROR",
)
for _boundary in ("owner_disconnected", "room_disconnected", "browser_disconnected"):
    REASONS[_boundary] = set(RTC_REASONS)
SOURCES = {
    "provider_started": "provider",
    "provider_closed": "provider",
    "provider_close_sent": "application",
    "provider_wire_closed": "transport",
    "provider_error": "sdk",
    "agent_session_closed": "sdk",
    "owner_disconnected": "room",
    "room_disconnected": "room",
    "bridge_exit": "application",
    "session_ended": "application",
    "browser_disconnected": "browser",
    "browser_visibility": "browser",
    "browser_pagehide": "browser",
}
ERROR_TYPES = {
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "TimeoutError",
    "CancelledError",
    "ConnectionError",
    "ClientConnectionError",
    "ClientConnectorError",
    "ServerDisconnectedError",
    "ClientOSError",
    "RuntimeError",
    "ValueError",
    "OSError",
}


def rtc_reason(value):
    return RTC_REASONS[value] if type(value) is int and 0 <= value < len(RTC_REASONS) else "unknown"


@contextmanager
def diagnostic_transaction():
    # SET LOCAL is restored on success or savepoint rollback; never change the
    # surrounding End/lease transaction's timeout policy.
    with transaction.atomic():
        if connection.vendor != "postgresql":
            yield
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            old_lock, old_statement = cursor.fetchone()
            cursor.execute(
                "SELECT set_config('lock_timeout', '250ms', true), set_config('statement_timeout', '1000ms', true)"
            )
            yield
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                [old_lock, old_statement],
            )


def _record_lifecycle(
    voice_id,
    claim_id,
    boundary,
    *,
    reason=None,
    error_type=None,
    recoverable=None,
    teardown=False,
    code=None,
    observed_at=None,
    event_id=None,
    visibility=None,
):
    """Keep first observation per bridge boundary, including teardown after logical End.

    Caller-controlled payloads never become text. A replaced worker cannot append to a
    new claim. Browser observations are owner-authenticated by the API, bounded separately,
    and accepted briefly after End so its disconnect race does not erase the evidence.
    """
    if boundary not in SOURCES:
        raise ValueError("Unknown voice lifecycle boundary")
    observed_at = observed_at or timezone.now()
    with diagnostic_transaction():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        if claim_id is not None and (not claim_id or session.bridge_claim_id != claim_id):
            return None
        browser = SOURCES[boundary] == "browser"
        if browser and (
            not event_id
            or (
                session.status != "active"
                and (not session.ended_at or timezone.now() > session.ended_at + timedelta(seconds=120))
            )
        ):
            return None
        identity = f"lifecycle:{claim_id or 'session'}:{boundary}"
        if browser:
            identity = f"lifecycle:browser:{event_id}"
        old = session.events.filter(event_id=identity).first()
        if old:
            return old
        if browser:
            count = session.events.filter(event_id__startswith="lifecycle:browser:").count()
            if count >= (40 if boundary == "browser_disconnected" else 39):
                return None
        data = {
            "boundary": boundary,
            "source": SOURCES[boundary],
            "teardown": teardown is True,
            "bridge_claim_id": claim_id,
            "observed_at": observed_at.isoformat(),
        }
        if boundary in REASONS:
            data["reason"] = reason if isinstance(reason, str) and reason in REASONS[boundary] else "unknown"
        if error_type is not None:
            data["exception_class"] = (
                error_type if isinstance(error_type, str) and error_type in ERROR_TYPES else "other"
            )
        if type(recoverable) is bool:
            data["recoverable"] = recoverable
        if type(code) is int and 1000 <= code <= 4999:
            data["wire_close_code"] = code
        if browser:
            data["visibility"] = visibility if visibility in ("visible", "hidden") else "unknown"
        if session.bridge_started_at:
            data["elapsed_seconds"] = round(max(0, (observed_at - session.bridge_started_at).total_seconds()), 3)
        return RealtimeVoiceEvent.objects.create(
            session=session,
            event_id=identity,
            kind="lifecycle",
            text=json.dumps(data, sort_keys=True),
            received_at=observed_at,
            processed_at=timezone.now(),
        )


def record_lifecycle(voice_id, claim_id, boundary, **facts):
    """Diagnostics are best effort; their storage must not prevent End or teardown."""
    try:
        return _record_lifecycle(voice_id, claim_id, boundary, **facts)
    except Exception as exc:
        logger.error("voice lifecycle storage failed exception_type=%s", type(exc).__name__)
        return None


def record_worker_lifecycle(voice_id, claim_id, boundary, **facts):
    """A pool-thread writer owns its connection; it never occupies the turn/End lane."""
    try:
        return record_lifecycle(voice_id, claim_id, boundary, **facts)
    finally:
        connection.close()
