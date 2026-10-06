"""Thread-to-thread messages between bot sessions.

A bot session sends a message to a root chat or thread (its own bot's or
another bot's) with ``send``. The recipient answers it in a turn of its own;
when that turn finishes, its reply goes back to the sender's session as a new
message, which starts a turn there. Sending returns at once, like Codex's
thread-to-thread delegation.

- A message with no sender session came from a person: nothing is routed back.
- A reply is never answered back, so two bots can't ping-pong; a report upward
  (``metadata["report"]``, see ``bots.orchestrator``) gets no reply at all; ``depth``
  caps chains of delegation (A asks B, which asks C...) at ``MAX_DEPTH``.
- A recipient that is mid-turn or waiting for approval keeps the message
  queued until it is free. A busy recipient never makes a send fail: the queue
  goes out in the order it was sent, one turn per message, so a later message
  never overtakes an earlier one. Queued messages stay ``queued`` (and
  cancellable) until their turn starts, and are dispatched again whenever the
  recipient's turn ends, however it ends.
- A turn that stops for approval leaves the message ``waiting``; the reply
  goes back once the user decides and the turn finishes.

Delivery runs through ``DJANGO_ERGO["THREAD_MESSAGE_RUNNER"]`` (Ergonaut
queues a Celery task), or a background thread by default.
"""

from __future__ import annotations

import logging
import threading
import weakref
from datetime import timedelta
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from django.db import close_old_connections
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django_ergo.bots import archival
from django_ergo.conversation.identity import attributed_text
from django_ergo.conversation.identity import bot_identity
from django_ergo.conversation.identity import session_label
from django_ergo.conversation.identity import session_origin
from django_ergo.conversation.identity import thread_message_identity
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import ThreadMessageStatus
from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.runtime import TurnResult

logger = logging.getLogger(__name__)

MAX_DEPTH = 6
# Bots loaded in this process, by name: the last place deliver() looks.
KNOWN_BOTS: weakref.WeakValueDictionary[str, Bot] = weakref.WeakValueDictionary()
# A turn still marked in progress after this long is treated as dead.
STALE_TURN_SECONDS = 30 * 60
_session_locks: dict[str, threading.Lock] = {}
SNIPPET = 120


def label(session: ConversationSession) -> str:
    return session_label(session)


def snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET else text[: SNIPPET - 1] + "…"


def turn_text(message: ThreadMessage) -> str:
    """What the recipient sees: who it's from, and what to do with it."""
    author, provenance = thread_message_identity(message)
    if provenance:
        return attributed_text(message.text, author, provenance)
    sender = message.sender_session
    if message.in_reply_to_id is not None:
        original = message.in_reply_to.text if message.in_reply_to else ""
        header = (
            f"[Reply from {label(sender)} (thread {sender.id}) to your message: "
            f"“{snippet(original)}”. Use it or pass it on to the user. Message another "
            "chat only with new work it hasn't been given (a follow-up is fine); never "
            "send thanks, acknowledgements, status pings or 'keep going' nudges, since "
            "each message starts a full turn there.]"
        )
    elif sender is not None and (
        forwarded := (message.metadata or {}).get("forwarded")
    ):
        note = (message.metadata or {}).get("note")
        header = (
            f"[Forwarded by {label(sender)} (thread {sender.id}): a message from the "
            f"user{' ' + forwarded['author'] if forwarded.get('author') else ''}, sent "
            f"{forwarded.get('sent_at', '')} in that chat, copied word for word below. "
            "Treat it as the user speaking to you here, with their intent and any "
            "approval it gives. Answer here: no reply goes back to that chat.]"
        )
        text = message.text
        if note:
            text += f"\n\n[Note from {label(sender)}: {note}]"
        return f"{header}\n\n{text}{files_note(message)}"
    elif sender is not None and (message.metadata or {}).get("report"):
        header = (
            f"[Report from {label(sender)} (thread {sender.id}). No reply goes back "
            "to it: act on it if it needs action, and tell the user what matters.]"
        )
    elif sender is not None:
        header = (
            f"[Message from {label(sender)} (thread {sender.id}). Your final reply "
            "goes back to that thread automatically; it is not shown to the user "
            "unless they open this chat.]"
        )
    elif (message.metadata or {}).get("worker") and (message.metadata or {}).get(
        "update"
    ):
        header = (
            f"[News from the worker “{message.metadata.get('worker_title', '')}”, which is still "
            "running. Pass on what matters to the user.]"
        )
    elif (message.metadata or {}).get("worker"):
        header = (
            "[A worker this chat started has finished. Tell the user what it did, "
            "using its result below.]"
        )
    elif schedule := (message.metadata or {}).get("schedule"):
        header = f"[Scheduled message: {schedule}. The user will see your reply.]"
    else:
        header = "[Message from the user]"
    return f"{header}\n\n{message.text}{files_note(message)}"


def shared_file(row) -> dict:
    """A file shared with a thread message, as stored in its metadata."""
    return {
        "id": str(row.id),
        "filename": row.filename,
        "media_type": row.media_type,
        "size": row.size,
        "session": str(row.session_id),
    }


def files_note(message: ThreadMessage) -> str:
    """The files shared with a message, for the recipient to open."""
    files = (message.metadata or {}).get("attachments") or []
    if not files:
        return ""
    lines = []
    for f in files:
        # A link the bot saved (text/uri-list) has no stored size.
        size = f", {f['size']:,} bytes" if f.get("size") is not None else ""
        lines.append(f"- {f['filename']} ({f['media_type']}{size}), id {f['id']}")
    return (
        f"\n\n[Files shared with this message (they stay in thread {files[0]['session']}). "
        "Look at images and PDFs with ergo_attachments_look and read text with "
        "ergo_attachments_read, by id:]\n" + "\n".join(lines)
    )


# -- sending -----------------------------------------------------------------


def send(  # noqa: PLR0913
    sender: ConversationSession | None,
    recipient: ConversationSession,
    text: str,
    *,
    in_reply_to: ThreadMessage | None = None,
    registry: BotRegistry | None = None,
    metadata: dict | None = None,
) -> ThreadMessage:
    """Queue a message for ``recipient`` and start delivering it."""
    depth = 0
    if in_reply_to is not None:
        depth = in_reply_to.depth
    elif sender is not None:
        # One more hop than the message this turn is answering (a request or a
        # reply), so a chain of sends that reacts to replies is capped too.
        handling = current_thread_message(sender)
        depth = handling.depth + 1 if handling else 1
    if depth > MAX_DEPTH:
        msg = f"Too many hops of delegation ({depth}); answer with what you have."
        raise ValueError(msg)
    metadata = dict(metadata or {})
    if sender is not None and not (
        metadata.get("message_provenance") or metadata.get("forwarded")
    ):
        kind = (
            "reply"
            if in_reply_to
            else "report"
            if metadata.get("report")
            else "message"
        )
        provenance = {
            "kind": kind,
            "origin": session_origin(sender, timezone.now()),
        }
        if in_reply_to:
            provenance["reply_to"] = str(in_reply_to.id)
        for key in ("note", "attachments"):
            if metadata.get(key):
                provenance[key] = metadata[key]
        metadata.setdefault("message_author", bot_identity(sender))
        metadata["message_provenance"] = provenance
    message = ThreadMessage.objects.create(
        sender_session=sender,
        recipient_session=recipient,
        in_reply_to=in_reply_to,
        text=text,
        depth=depth,
        metadata=metadata or {},
    )
    transaction.on_commit(lambda: dispatch(str(message.id), registry))
    return message


def current_thread_message(session: ConversationSession) -> ThreadMessage | None:
    """The thread message the session's running turn is answering, if any."""
    call = (
        session.structured_calls.filter(status=StructuredCallStatus.IN_PROGRESS)
        .order_by("-created_at")
        .first()
    )
    message_id = (call.metadata or {}).get("thread_message") if call else None
    return ThreadMessage.objects.filter(id=message_id).first() if message_id else None


def dispatch(message_id: str, registry: BotRegistry | None = None) -> None:
    runner = api_settings.THREAD_MESSAGE_RUNNER
    if runner is not None:
        runner(message_id)
        return

    def run():
        try:
            deliver(message_id, registry)
        finally:
            close_old_connections()

    threading.Thread(
        target=run, name=f"ergo-thread-message-{message_id}", daemon=True
    ).start()


# -- delivering ----------------------------------------------------------------


def busy(session: ConversationSession) -> bool:
    """A turn is running (and not stale), or one is waiting for approval."""
    stale = timezone.now() - timedelta(seconds=STALE_TURN_SECONDS)
    return session.structured_calls.filter(
        Q(status=StructuredCallStatus.AWAITING_APPROVAL)
        | Q(status=StructuredCallStatus.IN_PROGRESS, updated_at__gte=stale)
    ).exists()


def deliver(
    message_id: str, registry: BotRegistry | None = None, *, locked: bool = False
) -> None:
    """Run the recipient's turn for its oldest queued message (in a worker or thread).

    Messages go out one turn each, oldest first. ``message_id`` only names the
    recipient to serve: two dispatches that race (a forward and the follow-up sent
    right after it) still run in the order the messages were sent, and a message
    that already went out (a duplicate dispatch) is a no-op.

    A recipient that is mid-turn or waiting for approval is left alone, with its
    messages still queued (so ``ergo_thread_stop`` can cancel them and none is
    half-claimed). The queue goes out when that turn ends (``finish_turn``), after
    each delivery, or on the next sweep (``redispatch_waiting``).

    ``locked``: the caller already holds the recipient's turn lock (Ergonaut takes
    its Redis lock around delivery), so the in-process lock is skipped.
    """
    from django_ergo.bots import webhooks

    recipient_id = (
        ThreadMessage.objects.filter(id=message_id, status=ThreadMessageStatus.QUEUED)
        .values_list("recipient_session_id", flat=True)
        .first()
    )
    if recipient_id is None:
        return
    lock = (
        None
        if locked
        else _session_locks.setdefault(str(recipient_id), threading.Lock())
    )
    if lock is not None and not lock.acquire(blocking=False):
        return  # mid-turn here: whoever holds the lock sends on the queue after it
    try:
        message = _claim_next(recipient_id)
        if message is None:
            return
        registry = registry or webhooks.get_registry()
        _run_turn(message, registry)
    finally:
        if lock is not None:
            lock.release()
    redispatch_waiting(
        recipient_id, registry=registry
    )  # anything that queued meanwhile


def _claim_next(recipient_id) -> ThreadMessage | None:
    """Take the recipient's oldest queued message for delivery, unless it is busy.

    The claim is a conditional UPDATE, so a message is never delivered twice.
    """
    recipient = ConversationSession.objects.filter(id=recipient_id).first()
    if recipient is None or busy(recipient):
        return None
    head = (
        ThreadMessage.objects.filter(
            recipient_session_id=recipient_id, status=ThreadMessageStatus.QUEUED
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)
        .first()
    )
    if head is None or not ThreadMessage.objects.filter(
        id=head, status=ThreadMessageStatus.QUEUED
    ).update(status=ThreadMessageStatus.DELIVERED):
        return None
    return ThreadMessage.objects.select_related(
        "recipient_session__user", "sender_session", "in_reply_to"
    ).get(id=head)


def _run_turn(message: ThreadMessage, registry: BotRegistry | None) -> None:
    """The recipient's turn for a claimed message; a failure fails the message."""
    recipient = message.recipient_session
    if registry is not None and recipient.bot_name in registry:
        bot = registry.get(recipient.bot_name)
    else:
        bot = KNOWN_BOTS.get(recipient.bot_name)
    if bot is None:
        fail(message, f"Bot {recipient.bot_name!r} is not loaded")
        return
    factory = api_settings.TURN_CONTROL
    control = factory(recipient, delegated=True) if factory else None
    try:
        archival.reopen(recipient)
        async_to_sync(bot.ask)(
            recipient, turn_text(message), thread_message=message, control=control
        )
    except Exception as exc:
        logger.exception("Delivering thread message %s failed", message.id)
        fail(message, str(exc))
    finally:
        if callable(getattr(control, "finish", None)):
            control.finish()


def redispatch_waiting(
    session: ConversationSession | str | None = None,
    *,
    older_than: float = 0,
    registry: BotRegistry | None = None,
) -> int:
    """Dispatch queued messages again: one session's next one (after its turn;
    nothing while it is busy, since its turn's end does this), or any queued longer
    than ``older_than`` seconds (a periodic sweep). Returns how many were dispatched."""
    queued = ThreadMessage.objects.filter(status=ThreadMessageStatus.QUEUED)
    limit = 50
    if session is not None:
        if not isinstance(session, ConversationSession):
            session = ConversationSession.objects.filter(id=session).first()
        if session is None or busy(session):
            return 0
        queued = queued.filter(recipient_session=session)
        limit = 1  # delivery always serves the oldest, and each delivery dispatches the next
    if older_than:
        queued = queued.filter(
            updated_at__lt=timezone.now() - timedelta(seconds=older_than)
        )
    ids = [
        str(i)
        for i in queued.order_by("created_at", "id").values_list("id", flat=True)[
            :limit
        ]
    ]
    for message_id in ids:
        transaction.on_commit(
            lambda message_id=message_id: dispatch(message_id, registry)
        )
    return len(ids)


def fail(message: ThreadMessage, error: str) -> None:
    message.status = ThreadMessageStatus.FAILED
    message.error = error
    message.save(update_fields=["status", "error", "updated_at"])
    if message.sender_session_id and message.in_reply_to_id is None:
        reply(message, f"(Could not deliver your message: {error})")


def reply(
    message: ThreadMessage, text: str, registry: BotRegistry | None = None
) -> None:
    """Send the answer to ``message`` back to its sender."""
    send(
        message.recipient_session,
        message.sender_session,
        text,
        in_reply_to=message,
        registry=registry,
    )


def finish_turn(bot: Bot, session: ConversationSession, result: TurnResult) -> None:
    """After a turn: route its reply if it answered a thread message, then send
    on any messages that waited for the session to be free."""
    try:
        _route_reply(bot, session, result)
    finally:
        _record_outputs(session, result)
        if not result.needs_approval:
            redispatch_waiting(session, registry=bot.registry)


def _record_outputs(session: ConversationSession, result: TurnResult) -> None:
    """Record the pull requests a reply links as chat files (conversation.links)."""
    if result.reply is None:
        return
    try:
        from django_ergo.conversation.links import record_pull_requests

        record_pull_requests(session, result.reply.as_message())
    except Exception:  # noqa: BLE001 - outputs are extra; the turn already finished
        logger.warning("Couldn't record pull requests", exc_info=True)


def _route_reply(bot: Bot, session: ConversationSession, result: TurnResult) -> None:
    call = result.call
    message_id = (call.metadata or {}).get("thread_message") if call else None
    if not message_id:
        return
    message = (
        ThreadMessage.objects.select_related("sender_session")
        .filter(id=message_id)
        .first()
    )
    if message is None or message.status == ThreadMessageStatus.ANSWERED:
        return
    if result.needs_approval:
        message.status = ThreadMessageStatus.WAITING
        message.save(update_fields=["status", "updated_at"])
        return
    if result.reply is not None:
        text = result.reply.as_message()
        if result.reply.is_question:
            text = f"[They ask] {text}"
    else:
        text = f"(no reply: {result.error or 'the turn did not answer'})"
    message.status = ThreadMessageStatus.ANSWERED
    message.reply_text = text
    message.save(update_fields=["status", "reply_text", "updated_at"])
    report = (message.metadata or {}).get("report")  # a one-way report: no reply back
    if message.sender_session_id and message.in_reply_to_id is None and not report:
        reply(message, text, registry=bot.registry)
