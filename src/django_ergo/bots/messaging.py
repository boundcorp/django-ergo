"""Thread-to-thread messages between bot sessions.

A bot session sends a message to a root chat or thread (its own bot's or
another bot's) with ``send``. The recipient answers it in a turn of its own;
when that turn finishes, its reply goes back to the sender's session as a new
message, which starts a turn there. Sending returns at once, like Codex's
thread-to-thread delegation.

- A message with no sender session came from a person: nothing is routed back.
- A reply is never answered back, so two bots can't ping-pong; ``depth``
  caps chains of delegation (A asks B, which asks C...) at ``MAX_DEPTH``.
- A recipient that is mid-turn finishes that turn first.
- A turn that stops for approval leaves the message ``waiting``; the reply
  goes back once the user decides and the turn finishes.

Delivery runs through ``DJANGO_ERGO["THREAD_MESSAGE_RUNNER"]`` (Ergonaut
queues a Celery task), or a background thread by default.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from django.db import close_old_connections
from django.db import transaction

from django_ergo.bots import archival
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
IDLE_WAIT_SECONDS = 15 * 60
SNIPPET = 120


def label(session: ConversationSession) -> str:
    meta = session.metadata or {}
    title = meta.get("title") or (
        "Chat" if meta.get("bot_role") == "root" else "Thread"
    )
    return f"{session.bot_name} · {title}"


def snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET else text[: SNIPPET - 1] + "…"


def turn_text(message: ThreadMessage) -> str:
    """What the recipient sees: who it's from, and what to do with it."""
    sender = message.sender_session
    if message.in_reply_to_id is not None:
        original = message.in_reply_to.text if message.in_reply_to else ""
        header = (
            f"[Reply from {label(sender)} (thread {sender.id}) to your message: "
            f"“{snippet(original)}”]"
        )
    elif sender is not None:
        header = (
            f"[Message from {label(sender)} (thread {sender.id}). Your final reply "
            "goes back to that thread automatically; it is not shown to the user "
            "unless they open this chat.]"
        )
    else:
        header = "[Message from the user]"
    return f"{header}\n\n{message.text}"


# -- sending -----------------------------------------------------------------


def send(
    sender: ConversationSession | None,
    recipient: ConversationSession,
    text: str,
    *,
    in_reply_to: ThreadMessage | None = None,
    registry: BotRegistry | None = None,
) -> ThreadMessage:
    """Queue a message for ``recipient`` and start delivering it."""
    depth = 0
    if in_reply_to is not None:
        depth = in_reply_to.depth
    elif sender is not None:
        asked = (
            ThreadMessage.objects.filter(
                recipient_session=sender, in_reply_to__isnull=True
            )
            .exclude(sender_session=None)
            .order_by("-created_at")
            .first()
        )
        depth = asked.depth + 1 if asked else 1
    if depth > MAX_DEPTH:
        msg = f"Too many hops of delegation ({depth}); answer with what you have."
        raise ValueError(msg)
    message = ThreadMessage.objects.create(
        sender_session=sender,
        recipient_session=recipient,
        in_reply_to=in_reply_to,
        text=text,
        depth=depth,
    )
    transaction.on_commit(lambda: dispatch(str(message.id), registry))
    return message


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


def wait_until_idle(
    session: ConversationSession, timeout: float = IDLE_WAIT_SECONDS
) -> bool:
    """Wait for any turn in progress on ``session`` to finish."""
    deadline = time.monotonic() + timeout
    while session.structured_calls.filter(
        status=StructuredCallStatus.IN_PROGRESS
    ).exists():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def deliver(message_id: str, registry: BotRegistry | None = None) -> None:
    """Run the recipient's turn for a queued message (in a worker or thread)."""
    from django_ergo.bots import webhooks

    message = (
        ThreadMessage.objects.select_related(
            "recipient_session__user", "sender_session", "in_reply_to"
        )
        .filter(id=message_id, status=ThreadMessageStatus.QUEUED)
        .first()
    )
    if message is None:
        return
    recipient = message.recipient_session
    registry = registry or webhooks.get_registry()
    if registry is not None and recipient.bot_name in registry:
        bot = registry.get(recipient.bot_name)
    else:
        bot = KNOWN_BOTS.get(recipient.bot_name)
    if bot is None:
        fail(message, f"Bot {recipient.bot_name!r} is not loaded")
        return
    if not wait_until_idle(recipient):
        fail(message, "The recipient stayed busy too long")
        return
    archival.reopen(recipient)
    message.status = ThreadMessageStatus.DELIVERED
    message.save(update_fields=["status", "updated_at"])
    try:
        async_to_sync(bot.ask)(recipient, turn_text(message), thread_message=message)
    except Exception as exc:
        logger.exception("Delivering thread message %s failed", message_id)
        fail(message, str(exc))


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
    """After a turn: if it answered a thread message, route the reply."""
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
    if message.sender_session_id and message.in_reply_to_id is None:
        reply(message, text, registry=bot.registry)
