"""Finite durable elapsed-time wakes for one thread.

All transitions take the session lock before the schedule lock. Admission also
holds a global singleton row lock, so quotas work even for the first schedule.
The queue is the existing ThreadMessage queue: no second runner or paid retry.
"""

from __future__ import annotations

import math
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Exists
from django.db.models import F
from django.db.models import Max
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone

from django_ergo.conversation.budget import forbid
from django_ergo.conversation.lifecycle import record_event
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import RecurringWake
from django_ergo.conversation.models import RecurringWakeLock
from django_ergo.conversation.models import RecurringWakeOccurrence
from django_ergo.conversation.models import (
    RecurringWakeOccurrenceStatus as OccurrenceStatus,
)
from django_ergo.conversation.models import RecurringWakeStatus as Status
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import ThreadMessageStatus as MessageStatus
from django_ergo.settings import api_settings

LIVE = (Status.ACTIVE, Status.PAUSED, Status.BLOCKED)
OUTSTANDING = (
    OccurrenceStatus.QUEUED,
    OccurrenceStatus.DELIVERED,
    OccurrenceStatus.WAITING,
)
FIRE_BATCH = 100
# A claim may commit before bot.ask persists its StructuredCall.
CLAIM_START_GRACE_SECONDS = 60
MAX_NOTE_CHARS = 2000
MAX_INTEGER = 2_147_483_647
POLICY_KEYS = (
    "voice_session_id",
    "voice_event_id",
    "voice_execution_mode",
    "tool_execution_policy",
    "model_retry_policy",
)


def _time(value: datetime | None = None) -> datetime:
    value = (
        (timezone.now() if settings.USE_TZ else datetime.now(UTC))
        if value is None
        else value
    )
    if not isinstance(value, datetime):
        msg = "Give a datetime with a UTC offset"
        raise ValueError(msg)  # noqa: TRY004 — public validation uses ValueError
    if timezone.is_naive(value):
        if settings.USE_TZ:
            msg = "Give a datetime with a UTC offset"
            raise ValueError(msg)
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value if settings.USE_TZ else timezone.make_naive(value, UTC)


def _django_time(value: datetime) -> datetime:
    # Unlike scheduler slots, Django auto_now timestamps are host-local when
    # USE_TZ=False. Interpret that source before comparing canonical UTC times.
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_default_timezone())
    return _time(value)


def _archived(session: ConversationSession) -> bool:
    return session.status == "completed" or bool(
        (session.metadata or {}).get("archived_at")
    )


def _display_time(value: datetime | None) -> datetime | None:
    # Naive database values under USE_TZ=False are still canonical UTC here.
    return (
        value.replace(tzinfo=UTC)
        if value is not None and timezone.is_naive(value)
        else value
    )


def describe(schedule: RecurringWake) -> dict:
    return {
        "id": str(schedule.id),
        "note": schedule.note,
        "status": schedule.status,
        "interval_seconds": schedule.interval_seconds,
        "next_run_at": _display_time(schedule.next_run_at),
        "expires_at": _display_time(schedule.expires_at),
        "max_occurrences": schedule.max_occurrences,
        "occurrences": schedule.occurrences,
        "periodic_checkins": schedule.periodic_checkins,
        "blocked_reason": schedule.blocked_reason,
    }


def list_schedules(session: ConversationSession | None) -> list[dict]:
    if session is None:
        return []
    return [
        describe(schedule) for schedule in RecurringWake.objects.filter(session=session)
    ]


def _notify(session: ConversationSession) -> None:
    from django_ergo.bots.todos import notify_session

    transaction.on_commit(lambda: notify_session(session))


def _interval_seconds(interval_minutes: float) -> int:
    try:
        seconds = float(interval_minutes) * 60
    except (TypeError, ValueError, OverflowError):
        msg = "interval_minutes must be a finite number"
        raise ValueError(msg) from None
    if isinstance(interval_minutes, bool) or not math.isfinite(seconds):
        msg = "interval_minutes must be a finite number"
        raise ValueError(msg)
    minimum = max(1, api_settings.RECURRING_WAKE_MIN_INTERVAL_SECONDS)
    if seconds < minimum or seconds > MAX_INTEGER:
        msg = (
            f"A recurring wake interval must be at least {minimum} seconds "
            f"and at most {MAX_INTEGER} seconds"
        )
        raise ValueError(msg)
    return math.ceil(seconds)


def _finite_limits(max_occurrences: int | None, expires_at: datetime | None) -> None:
    if max_occurrences is not None and (
        isinstance(max_occurrences, bool)
        or not isinstance(max_occurrences, int)
        or not 1 <= max_occurrences <= MAX_INTEGER
    ):
        msg = "max_occurrences must be a positive whole number"
        raise ValueError(msg)
    if expires_at is None and max_occurrences is None:
        msg = (
            "A recurring wake needs expires_at or max_occurrences; "
            "it cannot run forever"
        )
        raise ValueError(msg)


def create(  # noqa: PLR0913
    session: ConversationSession,
    note: str,
    *,
    interval_minutes: float,
    expires_at: datetime | None = None,
    max_occurrences: int | None = None,
    periodic_checkins: bool = False,
    now: datetime | None = None,
) -> dict:
    """Admit one finite schedule. The first occurrence is one interval ahead."""
    forbid("schedule recurring wakes")
    now = _time(now)
    if not isinstance(note, str) or not note.strip():
        msg = "Leave a note explaining what to do on waking"
        raise ValueError(msg)
    seconds = _interval_seconds(interval_minutes)
    _finite_limits(max_occurrences, expires_at)
    if not isinstance(periodic_checkins, bool):
        msg = "periodic_checkins must be a boolean"
        raise ValueError(msg)  # noqa: TRY004 — public validation uses ValueError
    due = now + timedelta(seconds=seconds)
    if expires_at is not None:
        expires_at = _time(expires_at)
        if expires_at <= due:
            msg = "expires_at must be after the first occurrence"
            raise ValueError(msg)
    from django_ergo.bots.messaging import current_thread_message

    with transaction.atomic():
        RecurringWakeLock.objects.get_or_create(pk=1)
        RecurringWakeLock.objects.select_for_update().get(pk=1)
        session = ConversationSession.objects.select_for_update().get(pk=session.pk)
        if _archived(session):
            msg = "An archived thread cannot schedule recurring wakes"
            raise ValueError(msg)
        live = RecurringWake.objects.filter(status__in=LIVE)
        if live.count() >= api_settings.RECURRING_WAKE_MAX_GLOBAL:
            msg = "The global recurring wake quota is full"
            raise ValueError(msg)
        if (
            live.filter(session=session).count()
            >= api_settings.RECURRING_WAKE_MAX_PER_THREAD
        ):
            msg = "This thread's recurring wake quota is full"
            raise ValueError(msg)
        handling = current_thread_message(session)
        metadata = (
            handling.metadata
            if handling and isinstance(handling.metadata, dict)
            else {}
        )
        # A context-less caller must not turn a stored budget into an unbudgeted wake.
        if "turn_budget" in metadata:
            msg = "A budgeted turn cannot schedule recurring wakes"
            raise ValueError(msg)
        if metadata.get("recurring_wake") or metadata.get("recurring_origin"):
            msg = "A recurring wake turn cannot create another recurring schedule"
            raise ValueError(msg)
        schedule = RecurringWake.objects.create(
            session=session,
            bot_name=session.bot_name,
            note=note.strip()[:MAX_NOTE_CHARS],
            interval_seconds=seconds,
            next_run_at=due,
            expires_at=expires_at,
            max_occurrences=max_occurrences,
            periodic_checkins=periodic_checkins,
            depth=handling.depth if handling else 0,
            metadata={key: metadata[key] for key in POLICY_KEYS if key in metadata},
        )
        record_event(
            "requested",
            key=f"requested:{schedule.id}",
            occurred_at=_display_time(now),
            task_id=schedule.id,
            thread_id=session.id,
            category="handoff",
            actor="system",
            state="queued",
        )
        _notify(session)
    return describe(schedule)


def _save(schedule: RecurringWake, now: datetime) -> None:
    # Explicit timestamp supports deterministic sweeps and never rewinds scheduling.
    schedule.updated_at = now
    schedule.save(
        update_fields=[
            "status",
            "blocked_reason",
            "next_run_at",
            "occurrences",
            "updated_at",
        ]
    )


def _record_terminal(occurrence: RecurringWakeOccurrence, session_id) -> None:
    state = {
        OccurrenceStatus.ANSWERED: "completed",
        OccurrenceStatus.FAILED: "failed",
        OccurrenceStatus.CANCELLED: "cancelled",
    }.get(occurrence.status)
    if state:
        record_event(
            "finished",
            key=f"finished:{occurrence.id}",
            occurred_at=_display_time(occurrence.completed_at),
            task_id=occurrence.schedule_id,
            related_task_id=occurrence.id,
            thread_id=session_id,
            message_id=occurrence.thread_message_id or "",
            category="handoff",
            actor="system",
            state=state,
        )


def _cancel_queued(schedule: RecurringWake, reason: str, now: datetime) -> None:
    for occurrence in schedule.scheduled_occurrences.filter(
        status=OccurrenceStatus.QUEUED
    ):
        if occurrence.thread_message_id:
            changed = ThreadMessage.objects.filter(
                pk=occurrence.thread_message_id, status=MessageStatus.QUEUED
            ).update(status=MessageStatus.FAILED, error=reason, updated_at=now)
            if not changed:
                continue  # Already claimed: cancellation does not interrupt a running turn.
        occurrence.status = OccurrenceStatus.CANCELLED
        occurrence.error = reason
        occurrence.completed_at = now
        occurrence.save(update_fields=["status", "error", "completed_at", "updated_at"])
        _record_terminal(occurrence, schedule.session_id)


def control(  # noqa: C901, PLR0912 — explicit owner-controlled state transitions
    session: ConversationSession, schedule_id, action: str, now: datetime | None = None
) -> dict:
    if action not in ("pause", "resume", "cancel"):
        msg = "action must be pause, resume or cancel"
        raise ValueError(msg)
    if action == "resume":
        forbid("resume recurring wakes")
    now = _time(now)
    try:
        schedule_id = UUID(str(schedule_id))
    except (ValueError, TypeError, AttributeError):
        msg = "Recurring wake not found in this thread"
        raise LookupError(msg) from None
    with transaction.atomic():
        session = ConversationSession.objects.select_for_update().get(pk=session.pk)
        schedule = (
            RecurringWake.objects.select_for_update()
            .filter(pk=schedule_id, session=session)
            .first()
        )
        if schedule is None:
            msg = "Recurring wake not found in this thread"
            raise LookupError(msg)
        _reconcile_thread(session, now)
        schedule.refresh_from_db()
        if action == "cancel":
            if schedule.status != Status.COMPLETED:
                schedule.status = Status.CANCELLED
                schedule.blocked_reason = ""
                _cancel_queued(schedule, "Recurring wake cancelled", now)
        elif schedule.status in (Status.CANCELLED, Status.COMPLETED):
            msg = "A cancelled or completed recurring wake cannot be changed"
            raise ValueError(msg)
        elif action == "pause":
            schedule.status = Status.PAUSED
            schedule.blocked_reason = ""
            _cancel_queued(schedule, "Recurring wake paused", now)
        elif _archived(session):
            msg = "An archived thread cannot resume recurring wakes"
            raise ValueError(msg)
        elif schedule.expires_at is not None and schedule.expires_at <= now:
            schedule.status = Status.COMPLETED
            schedule.blocked_reason = ""
            _cancel_queued(schedule, "Recurring wake expired", now)
        elif (
            schedule.max_occurrences is not None
            and schedule.occurrences >= schedule.max_occurrences
        ):
            if schedule.scheduled_occurrences.filter(status__in=OUTSTANDING).exists():
                msg = "This recurring wake has used all its occurrences"
                raise ValueError(msg)
            schedule.status = Status.COMPLETED
            schedule.blocked_reason = ""
        else:
            schedule.status = Status.ACTIVE
            schedule.blocked_reason = ""
            # No catch-up on resume, including when the wall clock went backward.
            schedule.next_run_at = max(
                schedule.next_run_at, now + timedelta(seconds=schedule.interval_seconds)
            )
        _save(schedule, now)
        _notify(session)
    return describe(schedule)


def cancel_session(session: ConversationSession, now: datetime | None = None) -> int:
    """Archive hook: durable cancellation survives later reopening the session."""
    now = _time(now)
    with transaction.atomic():
        session = ConversationSession.objects.select_for_update().get(pk=session.pk)
        _reconcile_thread(session, now)
        schedules = list(
            RecurringWake.objects.select_for_update().filter(
                session=session, status__in=LIVE
            )
        )
        for schedule in schedules:
            schedule.status = Status.CANCELLED
            schedule.blocked_reason = ""
            _cancel_queued(schedule, "Thread archived", now)
            _save(schedule, now)
        if schedules:
            _notify(session)
    return len(schedules)


def _block(schedule: RecurringWake, error: str, now: datetime) -> None:
    if schedule.status in LIVE:
        schedule.status = Status.BLOCKED
        schedule.blocked_reason = error or "The recurring wake turn did not answer"
        _save(schedule, now)


def _retire_abandoned(
    message: ThreadMessage, session: ConversationSession, now: datetime
) -> bool:
    """An unknown outcome is final, never evidence that it is safe to retry."""
    from django_ergo.bots import messaging

    matching = session.structured_calls.filter(metadata__thread_message=str(message.pk))
    if matching.filter(
        messaging.running_turn() | messaging.awaiting_approval()
    ).exists():
        return False
    last_activity = _django_time(message.updated_at)
    latest_call = matching.order_by("-updated_at").first()
    if latest_call is not None:
        last_activity = max(last_activity, _django_time(latest_call.updated_at))
    # A completed call can still be handing its result through after_turn hooks.
    if last_activity > now - timedelta(seconds=CLAIM_START_GRACE_SECONDS):
        return False
    reason = (
        "Unknown outcome: the recurring wake has no live turn or actionable "
        "approval; it will not be retried"
    )
    if ThreadMessage.objects.filter(
        pk=message.pk, status__in=[MessageStatus.DELIVERED, MessageStatus.WAITING]
    ).update(status=MessageStatus.FAILED, error=reason, updated_at=now):
        message.status = MessageStatus.FAILED
        message.error = reason
        return True
    # A completion can persist before its lifecycle hook takes the session lock.
    message.refresh_from_db()
    return False


def _reconcile_thread(session: ConversationSession, now: datetime) -> bool:
    """Recover lifecycle state after a crash without dispatching or retrying work."""
    changed = False
    outstanding = RecurringWakeOccurrence.objects.filter(
        schedule__session=session, status__in=OUTSTANDING
    ).select_related("thread_message", "schedule")
    for occurrence in outstanding:
        previous_status = occurrence.status
        message = occurrence.thread_message
        schedule = occurrence.schedule
        if message is not None and message.status in (
            MessageStatus.DELIVERED,
            MessageStatus.WAITING,
        ):
            _retire_abandoned(message, session, now)
        if message is None:
            occurrence.status = OccurrenceStatus.FAILED
            occurrence.error = "The recurring wake message is missing"
            _block(schedule, occurrence.error, now)
        elif message.status == MessageStatus.FAILED:
            occurrence.status = OccurrenceStatus.FAILED
            occurrence.error = message.error or "The recurring wake turn failed"
            _block(schedule, occurrence.error, now)
        else:
            occurrence.status = message.status
        if occurrence.status not in OUTSTANDING:
            occurrence.completed_at = now
        occurrence.save(update_fields=["status", "error", "completed_at", "updated_at"])
        changed |= previous_status != occurrence.status
        _record_terminal(occurrence, session.pk)
        if (
            occurrence.status == OccurrenceStatus.WAITING
            and schedule.status == Status.ACTIVE
        ):
            schedule.blocked_reason = "awaiting_approval"
            _save(schedule, now)
        if (
            occurrence.status == OccurrenceStatus.ANSWERED
            and schedule.status == Status.ACTIVE
        ):
            schedule.blocked_reason = ""
            if (
                schedule.max_occurrences is not None
                and schedule.occurrences >= schedule.max_occurrences
            ):
                schedule.status = Status.COMPLETED
            _save(schedule, now)
    return changed


def delivery_failed(message: ThreadMessage, error: str) -> None:
    """Persist a final failure. Only an explicit owner resume admits future work."""
    occurrence_id = (message.metadata or {}).get("recurring_occurrence")
    if not occurrence_id:
        return
    now = _time()
    with transaction.atomic():
        session = (
            ConversationSession.objects.select_for_update()
            .filter(pk=message.recipient_session_id)
            .first()
        )
        if session is None:
            return
        occurrence = (
            RecurringWakeOccurrence.objects.select_related("schedule")
            .filter(pk=occurrence_id, thread_message=message)
            .first()
        )
        if occurrence is None or occurrence.status == OccurrenceStatus.CANCELLED:
            return
        occurrence.status = OccurrenceStatus.FAILED
        occurrence.error = error or "The recurring wake turn did not answer"
        occurrence.completed_at = now
        occurrence.save(update_fields=["status", "error", "completed_at", "updated_at"])
        _record_terminal(occurrence, session.pk)
        _block(occurrence.schedule, occurrence.error, now)
        _notify(session)


def delivery_finished(message: ThreadMessage, now: datetime | None = None) -> None:
    """Observe persisted WAITING/ANSWERED status; never send another message."""
    if not (message.metadata or {}).get("recurring_occurrence"):
        return
    now = _time(now)
    with transaction.atomic():
        session = (
            ConversationSession.objects.select_for_update()
            .filter(pk=message.recipient_session_id)
            .first()
        )
        if session is not None:
            _reconcile_thread(session, now)
            _notify(session)


def validate_delivery(  # noqa: C901, PLR0911 — transactional claim state machine
    message: ThreadMessage, now: datetime | None = None, *, claim: bool = False
) -> bool:
    """Claim QUEUED once (claim=True), or validate DELIVERED before bot.ask.

    The claim and pre-run check are distinct: a duplicate claim never accepts
    another worker's DELIVERED message.
    Pause/cancel/archive prevent unclaimed work; they cannot stop an ongoing turn.
    """
    if not (message.metadata or {}).get("recurring_occurrence"):
        return True
    now = _time(now)
    with transaction.atomic():
        session = (
            ConversationSession.objects.select_for_update()
            .filter(pk=message.recipient_session_id)
            .first()
        )
        if session is None:
            return False
        occurrence = (
            RecurringWakeOccurrence.objects.select_related("schedule")
            .filter(thread_message=message)
            .first()
        )
        if occurrence is None:
            ThreadMessage.objects.filter(
                pk=message.pk, status=MessageStatus.QUEUED
            ).update(
                status=MessageStatus.FAILED,
                error="Recurring wake occurrence missing",
                updated_at=now,
            )
            schedule_id = (message.metadata or {}).get("recurring_wake")
            try:
                schedule_id = UUID(str(schedule_id))
            except (ValueError, TypeError, AttributeError):
                return False
            schedule = (
                RecurringWake.objects.select_for_update()
                .filter(pk=schedule_id, session=session)
                .first()
            )
            if schedule is not None:
                _block(schedule, "Recurring wake occurrence missing", now)
            record_event(
                "finished",
                key=f"finished:{message.metadata['recurring_occurrence']}",
                occurred_at=_display_time(now),
                task_id=schedule_id,
                related_task_id=message.metadata["recurring_occurrence"],
                thread_id=session.pk,
                message_id=message.pk,
                category="handoff",
                actor="system",
                state="failed",
            )
            return False
        schedule = RecurringWake.objects.select_for_update().get(
            pk=occurrence.schedule_id
        )
        row = ThreadMessage.objects.select_for_update().get(pk=message.pk)
        expected = MessageStatus.QUEUED if claim else MessageStatus.DELIVERED
        if row.status != expected:
            return False
        valid = (
            schedule.status == Status.ACTIVE
            and not _archived(session)
            and (schedule.expires_at is None or now < schedule.expires_at)
            and occurrence.status in OUTSTANDING
            and row.status == expected
        )
        if not valid:
            if row.status in (MessageStatus.QUEUED, MessageStatus.DELIVERED):
                row.status = MessageStatus.FAILED
                row.error = "Recurring wake is no longer active or has expired"
                row.save(update_fields=["status", "error", "updated_at"])
                occurrence.status = OccurrenceStatus.CANCELLED
                occurrence.error = row.error
                occurrence.completed_at = now
                occurrence.save(
                    update_fields=["status", "error", "completed_at", "updated_at"]
                )
                _record_terminal(occurrence, session.pk)
            if _archived(session) and schedule.status in LIVE:
                schedule.status = Status.CANCELLED
                schedule.blocked_reason = ""
                _save(schedule, now)
            elif (
                schedule.expires_at is not None
                and now >= schedule.expires_at
                and schedule.status in LIVE
            ):
                schedule.status = Status.COMPLETED
                schedule.blocked_reason = ""
                _save(schedule, now)
            return False
        if claim:
            row.status = MessageStatus.DELIVERED
            row.save(update_fields=["status", "updated_at"])
            occurrence.status = OccurrenceStatus.DELIVERED
            occurrence.save(update_fields=["status", "updated_at"])
        message.status = row.status
        return True


def _advance(schedule: RecurringWake, now: datetime) -> datetime:
    """Coalesce missed slots to the newest due slot, always moving forward."""
    interval = timedelta(seconds=schedule.interval_seconds)
    skipped = (now - schedule.next_run_at) // interval
    scheduled_for = schedule.next_run_at + skipped * interval
    schedule.next_run_at = scheduled_for + interval
    return scheduled_for


def _fire(  # noqa: C901, PLR0911, PLR0912, PLR0915 — atomic sweep state machine
    schedule_id, now: datetime, registry=None
) -> str | None:
    from django_ergo.bots import messaging

    session_id = (
        RecurringWake.objects.filter(pk=schedule_id)
        .values_list("session_id", flat=True)
        .first()
    )
    if session_id is None:
        return None
    with transaction.atomic():
        session = (
            ConversationSession.objects.select_for_update()
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            return None
        schedule = (
            RecurringWake.objects.select_for_update().filter(pk=schedule_id).first()
        )
        if schedule is None or schedule.status not in LIVE:
            return None
        _reconcile_thread(session, now)
        schedule.refresh_from_db()
        if _archived(session):
            cancel_session(session, now)
            return None
        if schedule.expires_at is not None and now >= schedule.expires_at:
            schedule.status = Status.COMPLETED
            schedule.blocked_reason = ""
            _cancel_queued(schedule, "Recurring wake expired", now)
            _save(schedule, now)
            _notify(session)
            return None
        if schedule.status != Status.ACTIVE or schedule.next_run_at > now:
            return None
        outstanding = RecurringWakeOccurrence.objects.filter(
            schedule__session=session, status__in=OUTSTANDING
        )
        if (
            schedule.max_occurrences is not None
            and schedule.occurrences >= schedule.max_occurrences
        ):
            if not outstanding.filter(schedule=schedule).exists():
                schedule.status = Status.COMPLETED
                schedule.blocked_reason = ""
                _save(schedule, now)
            return None
        scheduled_for = _advance(schedule, now)
        if outstanding.exists():
            schedule.blocked_reason = (
                "awaiting_approval"
                if outstanding.filter(status=OccurrenceStatus.WAITING).exists()
                else "previous_occurrence_outstanding"
            )
            _save(schedule, now)
            return None
        if messaging.busy(session):
            schedule.blocked_reason = (
                "awaiting_approval"
                if session.structured_calls.filter(
                    messaging.awaiting_approval()
                ).exists()
                else "thread_busy"
            )
            _save(schedule, now)
            return None
        schedule.blocked_reason = ""
        recent = RecurringWakeOccurrence.objects.filter(
            schedule__session=session,
            dispatched_at__gt=now
            - timedelta(
                seconds=max(1, api_settings.RECURRING_WAKE_MIN_INTERVAL_SECONDS)
            ),
        )
        if recent.exists():
            schedule.blocked_reason = "thread_rate_limit"
            _save(schedule, now)
            return None
        try:
            with transaction.atomic():
                occurrence = RecurringWakeOccurrence.objects.create(
                    schedule=schedule, scheduled_for=scheduled_for, dispatched_at=now
                )
                occurrence.thread_message = messaging.send_event(
                    session,
                    f"[Recurring thread wake: automated notification, not a message from the user. Scheduled for {scheduled_for.isoformat()}.]\n\nYour note:\n{schedule.note}",
                    source="recurring_wake",
                    source_label="Recurring thread wake",
                    registry=registry,
                    metadata={
                        **schedule.metadata,
                        "recurring_wake": str(schedule.id),
                        "recurring_occurrence": str(occurrence.id),
                        "periodic_checkins": schedule.periodic_checkins,
                    },
                    depth=schedule.depth,
                )
                occurrence.save(update_fields=["thread_message", "updated_at"])
                for stage in ("requested", "queued"):
                    record_event(
                        stage,
                        key=f"{stage}:{occurrence.id}",
                        occurred_at=_display_time(now),
                        task_id=schedule.id,
                        related_task_id=occurrence.id,
                        thread_id=session.id,
                        message_id=occurrence.thread_message_id,
                        category="handoff",
                        actor="system",
                        state="queued",
                    )
        except Exception as exc:  # noqa: BLE001 - a failed send is final, never a retry
            schedule.status = Status.BLOCKED
            schedule.blocked_reason = str(exc) or type(exc).__name__
            _save(schedule, now)
            _notify(session)
            return None
        schedule.occurrences += 1
        _save(schedule, now)
        _notify(session)
    return str(schedule.id)


def _outstanding_blocker(outstanding: list[tuple]) -> str:
    if any(status == OccurrenceStatus.WAITING for _, status in outstanding):
        return "awaiting_approval"
    return "previous_occurrence_outstanding" if outstanding else ""


def _maintain_session(session_id, now: datetime) -> None:
    """Reconcile every outstanding lifecycle, independently of the firing batch."""
    with transaction.atomic():
        session = (
            ConversationSession.objects.select_for_update()
            .filter(pk=session_id)
            .first()
        )
        if session is None:
            return
        changed = _reconcile_thread(session, now)
        if _archived(session):
            cancel_session(session, now)
            return
        outstanding = list(
            RecurringWakeOccurrence.objects.filter(
                schedule__session=session, status__in=OUTSTANDING
            ).values_list("schedule_id", "status")
        )
        outstanding_ids = {schedule_id for schedule_id, _ in outstanding}
        blocker = _outstanding_blocker(outstanding)
        schedules = RecurringWake.objects.select_for_update().filter(
            session=session, status__in=LIVE
        )
        for schedule in schedules:
            expired = schedule.expires_at is not None and schedule.expires_at <= now
            exhausted = (
                schedule.status == Status.ACTIVE
                and schedule.max_occurrences is not None
                and schedule.occurrences >= schedule.max_occurrences
                and schedule.pk not in outstanding_ids
            )
            if expired or exhausted:
                schedule.status = Status.COMPLETED
                schedule.blocked_reason = ""
                if expired:
                    _cancel_queued(schedule, "Recurring wake expired", now)
                _save(schedule, now)
                changed = True
            elif schedule.status == Status.ACTIVE and (
                blocker
                or schedule.blocked_reason
                in ("awaiting_approval", "previous_occurrence_outstanding")
            ):
                if schedule.blocked_reason != blocker:
                    schedule.blocked_reason = blocker
                    _save(schedule, now)
                    changed = True
        if changed:
            _notify(session)


def fire_due(now: datetime | None = None, registry=None) -> list[str]:
    """At most one new occurrence per schedule per sweep, with no catch-up burst."""
    now = _time(now)
    # Maintenance is not capped by FIRE_BATCH: claimed, approval-waiting or
    # exhausted schedules must not prevent another thread from being considered.
    sessions = (
        RecurringWake.objects.filter(
            Q(status__in=LIVE) | Q(scheduled_occurrences__status__in=OUTSTANDING)
        )
        .order_by("session_id")
        .values_list("session_id", flat=True)
        .distinct()
    )
    for session_id in sessions.iterator(chunk_size=FIRE_BATCH):
        _maintain_session(session_id, now)
    outstanding = RecurringWakeOccurrence.objects.filter(
        schedule__session_id=OuterRef("session_id"), status__in=OUTSTANDING
    )
    due = (
        RecurringWake.objects.filter(status=Status.ACTIVE, next_run_at__lte=now)
        .filter(
            Q(max_occurrences__isnull=True) | Q(occurrences__lt=F("max_occurrences"))
        )
        .annotate(
            thread_outstanding=Exists(outstanding),
            last_served_at=Max("scheduled_occurrences__dispatched_at"),
        )
        .filter(thread_outstanding=False)
        .order_by(F("last_served_at").asc(nulls_first=True), "next_run_at", "id")
    )
    ids = list(due.values_list("id", flat=True)[:FIRE_BATCH])
    return [
        fired
        for schedule_id in ids
        if (fired := _fire(schedule_id, now, registry)) is not None
    ]
