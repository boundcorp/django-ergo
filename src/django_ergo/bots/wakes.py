"""Self-wakes: a bot schedules one later turn of its own in a chat or thread.

The built-in ``wake`` skill is loaded in every chat and thread. It gives the
model two tools:

- ``ergo_wake_later`` schedules a wake ``delay_minutes`` from now or ``at`` a
  time, with a note the bot leaves itself,
- ``ergo_wake_cancel`` cancels this chat's pending wake.

It is for work the bot can't finish now and can't hand to anyone: waiting out a
rate limit ("try again in a few minutes") or a long external job. When a wake
is due, ``fire_due`` (Ergonaut's beat runs it every 30 seconds) sends the chat
an automated event (``messaging.send_event``) with the note and whether a human
wrote since; a busy chat keeps it queued like any thread message.

Each wake is a ``Wake`` row, and nothing about it can loop:

- It fires at most once. ``fire`` claims the row with a conditional UPDATE
  (pending -> fired) in the same transaction that creates its ThreadMessage, so
  a duplicate task, a double beat tick or a restart finds it already fired, and
  a crash before the commit leaves neither. The ThreadMessage is claimed once
  for delivery too (``messaging._claim_next``).
- A failure is final: a send that raises, a delivery that fails or a turn that
  ends without answering marks the wake ``failed``; nothing retries it.
- A chat holds at most one pending wake (a partial unique constraint).
- A delay is between ``MIN_DELAY`` and ``MAX_DELAY``.
- ``chain`` counts wakes in a row: a wake scheduled after one fired, with no
  human message and no bot.yaml schedule prompt in the chat since, is one more.
  Past ``MAX_CHAIN`` the bot is refused and has to tell the user instead.
  Messages the bot can cause itself (replies from threads, worker results)
  don't reset the count.
- A wake's message keeps the delegation depth of the message its scheduling
  turn answered, so waking never resets ``messaging.MAX_DEPTH``: a thread that
  a wake turn started can't wake and start another thread forever.
- A human message doesn't cancel a pending wake; the wake message says whether
  one came in, and the bot can cancel it with ``ergo_wake_cancel``.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.budget import forbid
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import ThreadMessageStatus
from django_ergo.conversation.models import Wake
from django_ergo.conversation.models import WakeStatus

if TYPE_CHECKING:
    from datetime import tzinfo

    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.skillset import SkillDef
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import ToolContext

logger = logging.getLogger(__name__)

MIN_DELAY = timedelta(minutes=1)
MAX_DELAY = timedelta(hours=24)
MAX_CHAIN = 3
MAX_NOTE_CHARS = 2000
# Authors of messages a person wrote (django_ergo.conversation.identity).
HUMAN_KINDS = ("django_user", "telegram_user")
SOURCE = "wake"
SOURCE_LABEL = "Self-wake"
FIRE_BATCH = 100

LATER_DESCRIPTION = f"""\
Schedule one automated turn of your own in this chat, delay_minutes from now
({MIN_DELAY.seconds // 60} to {int(MAX_DELAY.total_seconds() // 60)}) or at a
time, to carry on with work you can't finish now: waiting out a rate limit
("try again in a few minutes") or a long external job (a deploy, an import, a
report being generated). When it fires you get a message marked as an automated
self-wake (not the user) with your note and whether the user wrote meanwhile;
then continue. Write the note for your future self: what you were doing, what to
retry and what comes next. Tell the user when you'll be back, then end the turn.
Don't use it to wait for a person's answer (end the turn; they will write), to
remind anyone, or for a worker you started (its result comes back on its own).
A chat has one pending wake at most; it fires once and is never retried. A wake
turn may schedule another, but at most {MAX_CHAIN} run in a row before the user
writes; past that, tell the user what is blocked."""

CANCEL_DESCRIPTION = """\
Cancel this chat's pending self-wake (from ergo_wake_later): the work it waited
for is done, the user asked you to stop, or you want a different time (cancel,
then schedule again)."""


# -- reading -----------------------------------------------------------------


def pending_wake(session: ConversationSession | None) -> Wake | None:
    if session is None:
        return None
    return Wake.objects.filter(session=session, status=WakeStatus.PENDING).first()


def pending_wakes(session: ConversationSession | None) -> list[dict]:
    """The chat's pending wakes, for apps to show with it."""
    wake = pending_wake(session)
    return [describe(wake)] if wake else []


def describe(wake: Wake) -> dict:
    return {
        "id": str(wake.id),
        "due_at": wake.due_at,
        "note": wake.note,
        "chain": wake.chain,
        "status": wake.status,
    }


def human_wrote_since(session: ConversationSession, since: datetime) -> bool:
    """A person (web, API or Telegram) wrote in the chat after ``since``."""
    return SessionMessage.objects.filter(
        session=session,
        role="user",
        created_at__gt=since,
        author__kind__in=HUMAN_KINDS,
    ).exists()


def _outside_input_since(session: ConversationSession, since: datetime) -> bool:
    """Something the bot can't cause itself came in: a person's message or a
    bot.yaml schedule prompt."""
    return (
        human_wrote_since(session, since)
        or ThreadMessage.objects.filter(
            recipient_session=session,
            sender_session__isnull=True,
            created_at__gt=since,
            metadata__has_key="schedule",
        ).exists()
    )


def next_chain(session: ConversationSession) -> int:
    """The chain number a wake scheduled now would get (1: a fresh chain)."""
    last = (
        Wake.objects.filter(session=session, fired_at__isnull=False)
        .order_by("-fired_at")
        .first()
    )
    if last is None or _outside_input_since(session, last.fired_at):
        return 1
    return last.chain + 1


# -- scheduling --------------------------------------------------------------


def due_time(
    now: datetime,
    *,
    delay_minutes: float | None = None,
    at: str = "",
    tz: tzinfo = UTC,
) -> datetime:
    """When a wake asked for with ``delay_minutes`` or ``at`` is due; ValueError
    when neither or both are given, or it is outside MIN_DELAY..MAX_DELAY."""
    if (delay_minutes is None) == (not at):
        msg = "Give delay_minutes or at (one of them)"
        raise ValueError(msg)
    if at:
        try:
            due = datetime.fromisoformat(at.strip())
        except ValueError:
            msg = f"at must be an ISO 8601 time like 2026-11-07T15:30, not {at!r}"
            raise ValueError(msg) from None
        if due.tzinfo is None:
            due = due.replace(tzinfo=tz)
        if timezone.is_naive(now):  # USE_TZ = False: naive server-local times
            due = timezone.make_naive(due)
    else:
        try:
            due = now + timedelta(minutes=float(delay_minutes))
        except (TypeError, ValueError, OverflowError):
            msg = "delay_minutes must be a number of minutes"
            raise ValueError(msg) from None
    # A few seconds of slack, for an `at` worked out from the turn's own clock.
    if due < now + MIN_DELAY - timedelta(seconds=5):
        msg = f"A wake must be at least {MIN_DELAY.seconds // 60} minute ahead"
        raise ValueError(msg)
    if due > now + MAX_DELAY:
        hours = int(MAX_DELAY.total_seconds() // 3600)
        msg = f"A wake can be at most {hours} hours ahead"
        raise ValueError(msg)
    return due


def schedule(  # noqa: PLR0913
    session: ConversationSession,
    note: str,
    *,
    delay_minutes: float | None = None,
    at: str = "",
    tz: tzinfo = UTC,
    now: datetime | None = None,
) -> Wake:
    """Schedule the chat's one pending wake; ValueError (for the model) when it
    breaks a limit."""
    # A wake fires later as a message that starts a turn with no budget.
    forbid("schedule a wake")
    now = now or timezone.now()
    note = (note or "").strip()
    if not note:
        msg = "Leave yourself a note: what you were doing and what to do on waking"
        raise ValueError(msg)
    due = due_time(now, delay_minutes=delay_minutes, at=at, tz=tz)
    chain = next_chain(session)
    if chain > MAX_CHAIN:
        msg = (
            f"{MAX_CHAIN} wakes have already run in a row with no message from the "
            "user. Don't schedule another: tell the user what is blocked and what "
            "you'll do once they reply."
        )
        raise ValueError(msg)
    if existing := pending_wake(session):
        raise ValueError(_already_pending(existing))
    from django_ergo.bots.messaging import current_thread_message

    # The wake continues whatever chain of delegation this turn is part of, so
    # waking can't reset messaging.MAX_DEPTH across threads.
    handling = current_thread_message(session)
    if handling and (
        (handling.metadata or {}).get("recurring_wake")
        or (handling.metadata or {}).get("recurring_origin")
    ):
        msg = (
            "A recurring check cannot schedule one-shot wakes to extend its "
            "finite lifetime. Ask the user for a new schedule instead."
        )
        raise ValueError(msg)
    try:
        with transaction.atomic():
            wake = Wake.objects.create(
                session=session,
                bot_name=session.bot_name,
                note=note[:MAX_NOTE_CHARS],
                due_at=due,
                chain=chain,
                depth=handling.depth if handling else 0,
            )
    except IntegrityError:  # another call in this turn got there first
        raise ValueError(_already_pending(pending_wake(session))) from None
    _notify(session)
    return wake


def _already_pending(wake: Wake | None) -> str:
    when = f" (due {wake.due_at.isoformat(timespec='minutes')})" if wake else ""
    return (
        f"This chat already has a wake pending{when}. A chat has one at a time: "
        "cancel it with ergo_wake_cancel first to schedule a different one."
    )


def cancel(session: ConversationSession) -> int:
    """Cancel the chat's pending wake, and a fired one whose message is still
    queued behind a busy turn. Returns how many were cancelled."""
    cancelled = Wake.objects.filter(session=session, status=WakeStatus.PENDING).update(
        status=WakeStatus.CANCELLED, updated_at=timezone.now()
    )
    for wake in Wake.objects.filter(
        session=session,
        status=WakeStatus.FIRED,
        thread_message__status=ThreadMessageStatus.QUEUED,
    ):
        if ThreadMessage.objects.filter(
            id=wake.thread_message_id, status=ThreadMessageStatus.QUEUED
        ).update(status=ThreadMessageStatus.FAILED, error="cancelled by the bot"):
            Wake.objects.filter(id=wake.id, status=WakeStatus.FIRED).update(
                status=WakeStatus.CANCELLED, updated_at=timezone.now()
            )
            cancelled += 1
    if cancelled:
        _notify(session)
    return cancelled


# -- firing ------------------------------------------------------------------


def fire_due(
    now: datetime | None = None, registry: BotRegistry | None = None
) -> list[str]:
    """Fire every pending wake that is due. Returns the ids of those fired here
    (a wake another process fired first is skipped)."""
    now = now or timezone.now()
    due = Wake.objects.filter(status=WakeStatus.PENDING, due_at__lte=now).order_by(
        "due_at"
    )
    return [
        str(wake_id)
        for wake_id in list(due.values_list("id", flat=True)[:FIRE_BATCH])
        if fire(wake_id, now=now, registry=registry) is not None
    ]


def fire(
    wake_id, *, now: datetime | None = None, registry: BotRegistry | None = None
) -> Wake | None:
    """Send one due wake's message to its chat, once. None when it isn't pending
    and due (already fired, cancelled, or not yet due)."""
    now = now or timezone.now()
    with transaction.atomic():
        # The claim: a concurrent firer waits on this row's lock, then matches nothing.
        if not Wake.objects.filter(
            id=wake_id, status=WakeStatus.PENDING, due_at__lte=now
        ).update(status=WakeStatus.FIRED, fired_at=now, updated_at=now):
            return None
        wake = Wake.objects.select_related("session__user").get(id=wake_id)
        try:
            with transaction.atomic():
                wake.thread_message = _send(wake, now, registry)
        except Exception as exc:
            logger.exception("Sending wake %s failed", wake_id)
            wake.status = WakeStatus.FAILED
            wake.error = str(exc) or type(exc).__name__
        wake.save(update_fields=["status", "thread_message", "error", "updated_at"])
    _notify(wake.session)
    return wake


def _send(wake: Wake, now: datetime, registry: BotRegistry | None) -> ThreadMessage:
    from django_ergo.bots import messaging

    return messaging.send_event(
        wake.session,
        wake_text(wake, now),
        source=SOURCE,
        source_label=SOURCE_LABEL,
        registry=registry,
        metadata={"wake": str(wake.id)},
        depth=wake.depth,
    )


def wake_text(wake: Wake, now: datetime) -> str:
    """The message a fired wake sends: the bot's note and what changed since."""
    tz = _user_timezone(wake.session)
    scheduled = _local(wake.created_at, tz)
    minutes = max(0, round((now - wake.created_at).total_seconds() / 60))
    if human_wrote_since(wake.session, wake.created_at):
        meanwhile = (
            "The user has written in this chat since you scheduled it: read what "
            "they said first, it may change or replace your plan."
        )
    else:
        meanwhile = "Nobody has written in this chat since you scheduled it."
    if wake.chain >= MAX_CHAIN:
        more = (
            f"This is wake {wake.chain} in a row, the last allowed before the user "
            "writes: finish now, or tell the user what is blocked."
        )
    else:
        more = f"This is wake {wake.chain} in a row (at most {MAX_CHAIN})."
    return (
        f"[Self-wake: you scheduled this turn with ergo_wake_later at "
        f"{scheduled:%Y-%m-%d %H:%M %Z} ({minutes} min ago). It is not a message "
        "from the user.]\n"
        f"{meanwhile} {more}\n\n"
        f"Your note:\n{wake.note}"
    )


def _user_timezone(session: ConversationSession) -> tzinfo:
    from django_ergo.bots.tools import ToolContext

    return ToolContext(user=session.user).timezone


def _local(moment: datetime, tz: tzinfo) -> datetime:
    """``moment`` in ``tz``; a naive one (USE_TZ = False) is server-local already."""
    return moment.astimezone(tz) if timezone.is_aware(moment) else moment


def delivery_failed(message: ThreadMessage, error: str) -> None:
    """A wake's message failed to deliver, or its turn failed: mark the wake failed.
    Nothing sends it again."""
    wake_id = (message.metadata or {}).get("wake")
    if not wake_id:
        return
    if Wake.objects.filter(id=wake_id, status=WakeStatus.FIRED).update(
        status=WakeStatus.FAILED,
        error=error or "the turn did not answer",
        updated_at=timezone.now(),
    ):
        _notify(message.recipient_session)


def _notify(session: ConversationSession) -> None:
    from django_ergo.bots.todos import notify_session

    notify_session(session)


# -- the skill ---------------------------------------------------------------


def wake_toolkit(ctx: ToolContext) -> FunctionToolkit:  # noqa: C901 - tool registration
    """The ``wake`` skill's tools for one chat."""
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool

    @bot_tool(
        name="ergo_wake_later",
        description=LATER_DESCRIPTION,
        parameters={
            "note": {
                "type": "string",
                "description": "For your future self: what you were doing, what to "
                "retry, what comes next",
            },
            "delay_minutes": {
                "type": "number",
                "description": "Minutes from now; give this or at",
            },
            "at": {
                "type": "string",
                "description": "An ISO 8601 time instead, e.g. 2026-11-07T15:30 "
                "(without an offset: the user's timezone)",
            },
        },
        required=["note"],
    )
    def later(note: str, delay_minutes: float | None = None, at: str = "") -> dict:
        tz = ctx.timezone
        wake = schedule(
            ctx.session, note, delay_minutes=delay_minutes, at=at or "", tz=tz
        )
        minutes = round((wake.due_at - timezone.now()).total_seconds() / 60)
        return {
            "wake_id": str(wake.id),
            "due_at": _local(wake.due_at, tz).isoformat(timespec="minutes"),
            "in_minutes": minutes,
            "chain": wake.chain,
            "note": (
                "Scheduled. Tell the user when you'll be back, then end your turn. "
                "It fires once; cancel it with ergo_wake_cancel."
            ),
        }

    @bot_tool(name="ergo_wake_cancel", description=CANCEL_DESCRIPTION, parameters={})
    def cancel_wake() -> dict:
        count = cancel(ctx.session)
        return {
            "cancelled": count,
            "note": "Cancelled." if count else "This chat had no pending wake.",
        }

    from django_ergo.bots import recurring_wakes

    @bot_tool(
        name="ergo_wake_recurring",
        description=(
            "Check this chat repeatedly only when the user asks for recurring "
            "checks, e.g. every 5 minutes for 10 hours. Give a finite duration, "
            "explicit-offset expiry, or max_occurrences; never renew it yourself. "
            "Unchanged checks are quiet unless periodic_checkins is true. "
            "Worker/task results already arrive naturally: don't poll for them. "
            "This grants no new tool permissions, approvals, or spend authority."
        ),
        parameters={
            "note": {
                "type": "string",
                "description": "What to check and what changes matter",
            },
            "interval_minutes": {"type": "number"},
            "duration_minutes": {
                "type": "number",
                "description": "Finite elapsed duration from now",
            },
            "expires_at": {
                "type": "string",
                "description": "ISO 8601 expiry with UTC offset",
            },
            "max_occurrences": {
                "type": "integer",
                "description": "Maximum dispatched checks",
            },
            "periodic_checkins": {
                "type": "boolean",
                "description": "Only true if user asks for unchanged updates",
            },
        },
        required=["note", "interval_minutes"],
    )
    def recurring(  # noqa: PLR0913 - tool schema
        note: str,
        interval_minutes: float,
        duration_minutes: float | None = None,
        expires_at: str = "",
        max_occurrences: int | None = None,
        periodic_checkins: bool = False,
    ) -> dict:
        now = datetime.now(UTC)
        if duration_minutes is not None and expires_at:
            msg = "Give duration_minutes or expires_at, not both"
            raise ValueError(msg)
        expiry = None
        if duration_minutes is not None:
            try:
                expiry = now + timedelta(minutes=float(duration_minutes))
            except (TypeError, ValueError, OverflowError):
                msg = "duration_minutes must be a finite positive number"
                raise ValueError(msg) from None
        elif expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
            except ValueError:
                msg = "expires_at must be an ISO 8601 time with UTC offset"
                raise ValueError(msg) from None
            if timezone.is_naive(expiry):
                msg = "expires_at requires an explicit UTC offset"
                raise ValueError(msg)
        return recurring_wakes.create(
            ctx.session,
            note,
            interval_minutes=interval_minutes,
            expires_at=expiry,
            max_occurrences=max_occurrences,
            periodic_checkins=periodic_checkins,
            now=now,
        )

    @bot_tool(
        name="ergo_wake_recurring_list",
        description="List this chat's recurring checks, finite limits, next run and blocking reason.",
        parameters={},
    )
    def recurring_list() -> dict:
        return {"schedules": recurring_wakes.list_schedules(ctx.session)}

    @bot_tool(
        name="ergo_wake_recurring_control",
        description=(
            "Pause, resume, or cancel a recurring check in this chat by schedule_id. "
            "Resume never extends expiry or count or approves blocked tools. "
            "Cancellation prevents queued checks; a running turn must be stopped separately."
        ),
        parameters={
            "schedule_id": {"type": "string"},
            "action": {"type": "string", "enum": ["pause", "resume", "cancel"]},
        },
        required=["schedule_id", "action"],
    )
    def recurring_control(schedule_id: str, action: str) -> dict:
        return recurring_wakes.control(ctx.session, schedule_id, action)

    return FunctionToolkit(
        [
            later.__bot_tool__,
            cancel_wake.__bot_tool__,
            recurring.__bot_tool__,
            recurring_list.__bot_tool__,
            recurring_control.__bot_tool__,
        ],
        ctx,
    )


def wake_context(ctx: ToolContext, message: str) -> list:
    """The pending wake in the turn's context, while there is one."""
    from django_ergo.conversation.context import TextContextSource

    def text() -> str:
        wake = pending_wake(ctx.session)
        if wake is None:
            return ""
        due = _local(wake.due_at, ctx.timezone)
        return (
            f"Due {due:%Y-%m-%d %H:%M %Z}; your note: {wake.note}\n"
            "It still fires if the user writes; cancel it with ergo_wake_cancel "
            "if it is no longer needed."
        )

    def recurring_text() -> str:
        import json

        from django_ergo.bots.recurring_wakes import list_schedules

        return json.dumps(list_schedules(ctx.session), default=str)

    return [
        TextContextSource("Pending self-wake", text, weight=2),
        TextContextSource("Recurring checks", recurring_text, weight=2),
    ]


def wake_skills() -> list[SkillDef]:
    """The built-in ``wake`` skill, loaded in every chat and thread."""
    from django_ergo.bots.skillset import SkillDef

    return [
        SkillDef(
            "wake",
            "Schedule one later turn of your own in this chat, to wait out a rate "
            "limit or a long external job",
            toolkits=lambda ctx: [wake_toolkit(ctx)],
            context=wake_context,
            always=True,
            source="built-in",
        )
    ]
