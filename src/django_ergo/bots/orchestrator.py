"""Orchestration tools: sessions of bots with ``orchestration: true`` get these.

- ``ergo_bot_list``: the bots this bot can message (sub-bots nested in its
  folder and bots in ``permissions.call_bots``), with their descriptions.
  Pre-seeded into every session.
- ``ergo_thread_list``: a bot's main chat, named chats and threads with this
  user, so the bot can choose where a message should go.
- ``ergo_thread_send``: message a main or named chat, a thread, or a new thread, of
  this bot or one it can message. It returns at once; the recipient's reply
  arrives later as a new message in this session (see
  ``django_ergo.bots.messaging``). Starting a thread of this bot needs
  ``sessions.allow_create: true``. A chat can't send another request to a chat
  while its earlier one there is still open (no nudges or acknowledgements: each
  message starts a turn there).
  Files from this chat can go with it (``attachments``): the recipient sees them
  listed, with ids, and opens them with the attachments tools.
- ``ergo_thread_archive``: archive one of this bot's threads that is done.

Reading another session's history is done with the history tools, which
cover every session this bot has with the user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError
from django.db.models import Q

from django_ergo.bots import archival
from django_ergo.bots import messaging
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.attachments import find_session_file
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import ThreadMessageStatus

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext

TOP_ROLES = {"root", "main", "chat"}  # main and named chats (not threads)
OPEN_STATUSES = [
    ThreadMessageStatus.QUEUED,
    ThreadMessageStatus.DELIVERED,
    ThreadMessageStatus.WAITING,
]


def _target(ctx: ToolContext, bot: str) -> Bot:
    """This bot (``bot`` empty or its own name), or one it may message."""
    caller = ctx.bot
    if not bot or bot == caller.name:
        return caller
    registry = caller.registry
    if registry is None or not registry.may_call(caller, bot) or bot not in registry:
        allowed = (
            ", ".join(b.name for b in registry.callable_bots(caller))
            if registry
            else ""
        )
        msg = f"This bot may not message {bot!r} (allowed: {allowed or 'none'})."
        raise ValueError(msg)
    return registry.get(bot)


def _session(target: Bot, ctx: ToolContext, thread_id: str) -> ConversationSession:
    try:
        return target.sessions().filter(user_id=ctx.session.user_id).get(id=thread_id)
    except (ConversationSession.DoesNotExist, ValidationError, ValueError):
        msg = f"No thread {thread_id} in {target.name}"
        raise ValueError(msg) from None


def _thread_key(session: ConversationSession) -> str:
    """How ergo_thread_send names a session: main, a named chat, or a thread id."""
    meta = session.metadata or {}
    if meta.get("bot_role") in ("root", "main"):
        return "main"
    if meta.get("bot_role") == "chat":
        return str(meta.get("chat") or session.id)
    return str(session.id)


def _row(session: ConversationSession, current: ConversationSession) -> dict:
    open_requests = session.thread_messages.filter(
        status__in=OPEN_STATUSES, in_reply_to__isnull=True
    ).count()
    row = {
        "thread": _thread_key(session),
        "id": str(session.id),
        "title": messaging.label(session),
        "status": "archived" if session.status == "completed" else session.status,
        "last_activity": session.updated_at.isoformat(timespec="seconds"),
        "open_requests": open_requests,
    }
    if session.id == current.id:
        row["you_are_here"] = True
    return row


@bot_tool(takes_context=True)
def ergo_bot_list(ctx: ToolContext) -> str:
    """List the bots this bot can message, with their descriptions."""
    registry = ctx.bot.registry
    bots = registry.callable_bots(ctx.bot) if registry else []
    if not bots:
        return "There are no other bots you can message."
    lines = [
        f"- {b.name}: {b.definition.description or 'no description'}" for b in bots
    ]
    return "Bots you can message:\n" + "\n".join(lines)


@bot_tool(takes_context=True)
def ergo_thread_list(
    ctx: ToolContext, bot: str = "", include_archived: bool = False
) -> list[dict]:
    """List a bot's main chat, named chats and threads with the user (default: this bot), newest first."""
    target = _target(ctx, bot)
    qs = target.sessions().filter(user_id=ctx.session.user_id).order_by("-updated_at")
    if not include_archived:
        qs = qs.filter(
            ~Q(status="completed") | Q(metadata__bot_role__in=list(TOP_ROLES))
        )
    return [_row(s, ctx.session) for s in qs[:50]]


@bot_tool(
    takes_context=True,
    parameters={
        "message": {"type": "string", "description": "What you want done or asked"},
        "bot": {
            "type": "string",
            "description": "Which bot: empty for this bot, or a name from ergo_bot_list",
        },
        "thread": {
            "type": "string",
            "description": '"main" for its main chat, a named chat, "new" for a new thread, or a thread id',
        },
        "title": {"type": "string", "description": "Title for a new thread"},
        "attachments": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files from this chat to share (filenames or file ids). The recipient "
                "can open them; they stay in this chat."
            ),
        },
    },
    required=["message"],
)
def ergo_thread_send(  # noqa: PLR0913
    ctx: ToolContext,
    message: str,
    bot: str = "",
    thread: str = "main",
    title: str = "",
    attachments: list[str] | None = None,
) -> dict:
    """Send a message to a bot's main chat, a named chat, a thread, or a new thread.

    Returns at once. The reply arrives later as a new message in this chat.
    """
    target = _target(ctx, bot)
    user = ctx.session.user
    shared = [
        messaging.shared_file(find_session_file(ctx.session, ref))
        for ref in attachments or []
    ]
    thread = (thread or "main").strip()
    if thread in ("main", "root") or thread in target.definition.chats:
        recipient = async_to_sync(target.chat_session)(user, thread)
    elif thread == "new":
        if target is ctx.bot and not target.definition.allow_create_sessions:
            msg = "This bot may not start threads of its own (sessions.allow_create)."
            raise ValueError(msg)
        recipient = async_to_sync(target.create_session)(
            user,
            parent=ctx.session if target is ctx.bot else None,
            title=title or messaging.snippet(message)[:60],
            metadata={"started_by": str(ctx.session.id)},
        )
    else:
        recipient = _session(target, ctx, thread)
    if recipient.id == ctx.session.id:
        msg = "That is this chat; send it somewhere else."
        raise ValueError(msg)
    if open_request := (
        ctx.session.sent_thread_messages.filter(
            recipient_session=recipient,
            in_reply_to__isnull=True,
            status__in=OPEN_STATUSES,
        )
        .order_by("-created_at")
        .first()
    ):
        # Nudges and "thanks, keep going" messages each start a full turn there.
        msg = (
            f"Your request to {messaging.label(recipient)} from "
            f"{open_request.created_at:%H:%M} UTC (“{messaging.snippet(open_request.text)}”) "
            "is still open; its reply will arrive here. Don't nudge or add to it: wait "
            "for the reply, then send new work if there is any."
        )
        raise ValueError(msg)
    sent = messaging.send(
        ctx.session,
        recipient,
        message,
        registry=ctx.bot.registry,
        metadata={"attachments": shared} if shared else None,
    )
    return {
        "sent_to": messaging.label(recipient),
        "thread_id": str(recipient.id),
        "message_id": str(sent.id),
        "note": "The reply will arrive as a new message in this chat.",
        **({"shared_files": [f["filename"] for f in shared]} if shared else {}),
    }


@bot_tool(takes_context=True)
def ergo_thread_archive(ctx: ToolContext, thread_id: str) -> str:
    """Archive one of this bot's threads that is done. Its history stays readable."""
    thread = _session(ctx.bot, ctx, thread_id)
    if (thread.metadata or {}).get("bot_role") in TOP_ROLES:
        msg = "Main and named chats can't be archived."
        raise ValueError(msg)
    archival.archive(thread, reason="archived by the bot")
    return f"Archived {messaging.label(thread)}"


def orchestrator_toolkit(ctx: ToolContext) -> FunctionToolkit:
    registry = ctx.bot.registry
    reachable = registry.callable_bots(ctx.bot) if registry else []
    functions = [ergo_bot_list, ergo_thread_list, ergo_thread_send, ergo_thread_archive]
    tools: list[BotTool] = [fn.__bot_tool__ for fn in functions]
    # Every session starts knowing which bots it can reach.
    return FunctionToolkit(tools, ctx, seed=["ergo_bot_list"] if reachable else [])
