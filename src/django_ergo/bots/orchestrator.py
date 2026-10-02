"""Orchestrator tools: every bot's root session gets these.

- ``threads_list`` / ``threads_create`` / ``threads_send`` / ``threads_close``
  manage this bot's thread sessions with the same user. Creating threads
  needs ``sessions.allow_create: true`` in bot.yaml.
- ``ergo_bot_call`` sends a message to one of this bot's sub-bots (bot folders
  nested in its folder) or a bot listed in ``permissions.call_bots``. Each
  calling bot gets its own thread in the called bot, reused across calls.

Reading thread histories is done with the root's history tools, which cover
every session this bot has with the user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError

from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.models import CompactionMode
from django_ergo.conversation.models import ConversationSession

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.runtime import TurnResult
    from django_ergo.bots.tools import ToolContext


def _thread(ctx: ToolContext, thread_id: str) -> ConversationSession:
    try:
        return (
            ctx.bot.sessions()
            .filter(user_id=ctx.session.user_id, metadata__bot_role="thread")
            .get(id=thread_id)
        )
    except (ConversationSession.DoesNotExist, ValidationError, ValueError):
        msg = f"No thread {thread_id}"
        raise ValueError(msg) from None


def _reply(result: TurnResult) -> str:
    if result.reply is not None:
        prefix = "[The thread asks] " if result.reply.is_question else ""
        text = prefix + result.reply.as_message()
    else:
        text = f"(no reply: {result.error or 'the thread did not answer'})"
    if result.approvals:
        names = ", ".join(a.tool_name for a in result.approvals)
        text += (
            f"\n\n[The thread is paused waiting for the user to approve: {names}. "
            f"It continues after they decide.]"
        )
    return text


def _run(bot: Bot, session: ConversationSession, message: str) -> str:
    return _reply(async_to_sync(bot.ask)(session, message))


@bot_tool(takes_context=True)
def threads_list(ctx: ToolContext, include_closed: bool = False) -> list[dict]:  # noqa: FBT001, FBT002
    """List this bot's threads with the user: id, title, status and size."""
    qs = (
        ctx.bot.sessions()
        .filter(user_id=ctx.session.user_id, metadata__bot_role="thread")
        .order_by("-updated_at")
    )
    if not include_closed:
        qs = qs.exclude(status="completed")
    return [
        {
            "thread_id": str(s.id),
            "title": (s.metadata or {}).get("title", ""),
            "status": s.status,
            "compaction_mode": s.compaction_mode,
            "messages": s.claude_messages.count() or s.openai_messages.count(),
            "updated_at": s.updated_at.isoformat(),
            "history_source_id": f"session:{s.id}",
        }
        for s in qs
    ]


@bot_tool(
    takes_context=True,
    parameters={
        "title": {"type": "string", "description": "Short name for the thread"},
        "message": {
            "type": "string",
            "description": "Optional first message; the thread's reply is returned",
        },
        "compaction_mode": {
            "type": "string",
            "enum": list(CompactionMode.values),
            "description": "Defaults to the bot's configured mode",
        },
        "instructions": {
            "type": "string",
            "description": "Optional extra instructions appended to the bot's own",
        },
    },
    required=["title"],
)
def threads_create(
    ctx: ToolContext,
    title: str,
    message: str = "",
    compaction_mode: str = "",
    instructions: str = "",
) -> dict:
    """Start a new thread (a separate session of this bot) for a focused task."""
    bot = ctx.bot
    if not bot.definition.allow_create_sessions:
        msg = "This bot is not allowed to create threads (sessions.allow_create)."
        raise ValueError(msg)
    system_prompt = bot.definition.instructions
    if instructions:
        system_prompt = f"{system_prompt}\n\n{instructions}".strip()
    thread = async_to_sync(bot.create_session)(
        ctx.session.user,
        parent=ctx.session,
        title=title,
        compaction_mode=compaction_mode or None,
        system_prompt=system_prompt,
    )
    out = {"thread_id": str(thread.id), "title": title}
    if message:
        out["reply"] = _run(bot, thread, message)
    return out


@bot_tool(takes_context=True)
def threads_send(ctx: ToolContext, thread_id: str, message: str) -> str:
    """Send a message to one of this bot's threads and return its reply."""
    thread = _thread(ctx, thread_id)
    if thread.status == "completed":
        msg = f"Thread {thread_id} is closed"
        raise ValueError(msg)
    return _run(ctx.bot, thread, message)


@bot_tool(takes_context=True)
def threads_close(ctx: ToolContext, thread_id: str) -> str:
    """Close a thread that is done. Its history stays readable."""
    thread = _thread(ctx, thread_id)
    async_to_sync(ctx.bot.close_session)(thread)
    return f"Closed thread {thread_id}"


@bot_tool(takes_context=True)
def ergo_bot_call(ctx: ToolContext, bot: str, message: str) -> str:
    """Send a message to another bot this bot may call, and return its reply."""
    caller = ctx.bot
    registry = caller.registry
    if registry is None or not registry.may_call(caller, bot):
        allowed = ", ".join(b.name for b in registry.callable_bots(caller)) if registry else ""
        msg = f"This bot may not call {bot!r} (allowed: {allowed or 'none'})."
        raise ValueError(msg)
    if bot not in registry:
        msg = f"Bot {bot!r} is not loaded"
        raise ValueError(msg)
    target = caller.registry.get(bot)
    user = ctx.session.user
    session = (
        target.sessions(user)
        .filter(metadata__bot_role="thread", metadata__called_by=caller.name)
        .exclude(status="completed")
        .order_by("created_at")
        .first()
    )
    if session is None:
        session = async_to_sync(target.create_session)(
            user,
            title=f"Calls from {caller.name}",
            metadata={"called_by": caller.name},
        )
    return _run(target, session, message)


THREAD_TOOLS = [threads_list, threads_create, threads_send, threads_close]


def orchestrator_toolkit(ctx: ToolContext) -> FunctionToolkit:
    functions = list(THREAD_TOOLS)
    if not ctx.bot.definition.allow_create_sessions:
        functions.remove(threads_create)
    if ctx.bot.definition.call_bots or (
        ctx.bot.registry and ctx.bot.registry.children(ctx.bot)
    ):
        functions.append(ergo_bot_call)
    tools: list[BotTool] = [fn.__bot_tool__ for fn in functions]
    return FunctionToolkit(tools, ctx)
