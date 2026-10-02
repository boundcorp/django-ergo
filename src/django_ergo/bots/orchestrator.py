"""Orchestration tools: sessions of bots with ``orchestration: true`` get these.

- ``ergo_bot_list``: the bots this bot can message (sub-bots nested in its
  folder and bots in ``permissions.call_bots``), with their descriptions.
  Pre-seeded into every session.
- ``ergo_thread_list``: a bot's root chat and threads with this user, so the
  bot can choose where a message should go.
- ``ergo_thread_send``: message a root chat, a thread, or a new thread, of
  this bot or one it can message. It returns at once; the recipient's reply
  arrives later as a new message in this session (see
  ``django_ergo.bots.messaging``). Starting a thread of this bot needs
  ``sessions.allow_create: true``.
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
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import ThreadMessageStatus

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext

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


def _row(session: ConversationSession, current: ConversationSession) -> dict:
    meta = session.metadata or {}
    open_requests = session.thread_messages.filter(
        status__in=OPEN_STATUSES, in_reply_to__isnull=True
    ).count()
    row = {
        "thread": "root" if meta.get("bot_role") == "root" else str(session.id),
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
    """List a bot's root chat and threads with the user (default: this bot), newest first."""
    target = _target(ctx, bot)
    qs = target.sessions().filter(user_id=ctx.session.user_id).order_by("-updated_at")
    if not include_archived:
        qs = qs.filter(~Q(status="completed") | Q(metadata__bot_role="root"))
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
            "description": '"root" for its main chat, "new" for a new thread, or a thread id',
        },
        "title": {"type": "string", "description": "Title for a new thread"},
    },
    required=["message"],
)
def ergo_thread_send(
    ctx: ToolContext, message: str, bot: str = "", thread: str = "root", title: str = ""
) -> dict:
    """Send a message to a bot's root chat, a thread, or a new thread.

    Returns at once. The reply arrives later as a new message in this chat.
    """
    target = _target(ctx, bot)
    user = ctx.session.user
    thread = (thread or "root").strip()
    if thread == "root":
        recipient = async_to_sync(target.root_session)(user)
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
    sent = messaging.send(ctx.session, recipient, message, registry=ctx.bot.registry)
    return {
        "sent_to": messaging.label(recipient),
        "thread_id": str(recipient.id),
        "message_id": str(sent.id),
        "note": "The reply will arrive as a new message in this chat.",
    }


@bot_tool(takes_context=True)
def ergo_thread_archive(ctx: ToolContext, thread_id: str) -> str:
    """Archive one of this bot's threads that is done. Its history stays readable."""
    thread = _session(ctx.bot, ctx, thread_id)
    if (thread.metadata or {}).get("bot_role") == "root":
        msg = "The root chat can't be archived."
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
