"""Orchestration tools: sessions of bots with ``orchestration: true`` get these.

Who may message whom:

- Upward is always allowed: any chat may message its own bot's main chat, and a
  bot's main chat may message its parent bot's main chat. Bots with
  ``orchestration: false`` get only ``ergo_message_up`` for this.
- Downward (sub-bots nested in the bot's folder) needs ``orchestration``.
- Sideways (any other bot) needs ``permissions.call_bots``.
- A new thread is the target's decision: ``thread="new"`` needs the target
  bot's ``threads.allow_create``, whoever sends it.

- ``ergo_bot_list``: the bots this bot can message (sub-bots nested in its
  folder, bots in ``permissions.call_bots`` and its parent), with their
  descriptions (also in the "Bots and threads" context block, bots.overview).
- ``ergo_thread_list``: a bot's main chat, named chats and threads with this
  user, so the bot can choose where a message should go.
- ``ergo_thread_send``: message a main or named chat, a thread, or a new thread, of
  this bot or one it can message. It returns at once; the recipient's reply
  arrives later as a new message in this session (see
  ``django_ergo.bots.messaging``). Starting a thread needs the target bot's
  ``threads.allow_create: true``; a thread of another bot records who started it. A chat can't send another request to a chat
  while its earlier one there is still open (no nudges or acknowledgements: each
  message starts a turn there).
  Files from this chat can go with it (``attachments``): the recipient sees them
  listed, with ids, and opens them with the attachments tools.
- ``ergo_thread_stop``: stop the running turn of a thread this bot started (or
  one of its own threads) and cancel what this bot queued for it. Stopping a
  running turn needs ``DJANGO_ERGO["TURN_STOPPER"]``.
- ``ergo_thread_archive``: archive one of this bot's threads, or a sub-bot thread
  this bot started, that is done.
- ``ergo_message_up``: message this bot's main chat, or the parent bot's main
  chat from this bot's main chat (always available, even without orchestration).

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
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import ThreadMessageStatus
from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext

TOP_ROLES = {"root", "main", "chat"}  # main and named chats (not threads)
# In a turn that a chat's reply started, a message back to that chat this short is
# taken for a nudge ("please continue") unless the reply asked a question.
FOLLOW_UP_MIN_CHARS = 400
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


def chat_identity(session: ConversationSession) -> str:
    """Which chat this is, for its own context: a thread must know it's the
    thread, not the main chat its instructions also describe."""
    meta = session.metadata or {}
    role = meta.get("bot_role")
    if role in ("root", "main"):
        return f"You are {session.bot_name} · Main, the main chat."
    if role == "chat":
        return f"You are {session.bot_name} · {meta.get('chat') or meta.get('title')}, a named chat."
    started = meta.get("started_by_label")
    if not started and session.parent_id:
        started = messaging.label(session.parent)
    title = meta.get("title") or "Thread"
    if not started:
        return f"You are {session.bot_name} · {title}, a thread. Do its work here."
    return (
        f"You are {session.bot_name} · {title}, a thread started by {started}. "
        f"Do the work here; don't hand it to another {session.bot_name} thread. Your "
        f"final reply to a message from {started} goes back to it automatically, so "
        "report there with that reply, not with extra messages."
    )


def thread_status(session: ConversationSession) -> dict:
    """What a chat is doing, for orchestrators and UIs (the "Bots and threads"
    block uses it; so can an API).

    ``state`` is ``working``, ``waiting_for_approval`` or ``idle``;
    ``last_activity`` is when its latest turn moved (sessions aren't touched per
    turn); ``started_by``/``started_by_id`` name another bot's chat that started
    it; ``working_for``/``waiting_on`` count open requests in and out;
    ``workers_running`` counts queued or running workers.
    """
    from django_ergo.conversation.models import StructuredCallStatus
    from django_ergo.conversation.models import Worker
    from django_ergo.conversation.models import WorkerStatus

    calls = session.structured_calls
    if calls.filter(status=StructuredCallStatus.AWAITING_APPROVAL).exists():
        state = "waiting_for_approval"
    elif messaging.busy(session):
        state = "working"
    else:
        state = "idle"
    latest = calls.order_by("-updated_at").values_list("updated_at", flat=True).first()
    meta = session.metadata or {}
    other_bot = meta.get("started_by_bot") not in (None, "", session.bot_name)
    return {
        "state": state,
        "last_activity": max(latest, session.updated_at)
        if latest
        else session.updated_at,
        "started_by": str(meta.get("started_by_label") or "") if other_bot else "",
        "started_by_id": str(meta.get("started_by") or "") if other_bot else "",
        "working_for": session.thread_messages.filter(
            status__in=OPEN_STATUSES, in_reply_to__isnull=True
        ).count(),
        "waiting_on": session.sent_thread_messages.filter(
            status__in=OPEN_STATUSES, in_reply_to__isnull=True
        ).count(),
        "workers_running": Worker.objects.filter(
            session=session, status__in=[WorkerStatus.QUEUED, WorkerStatus.RUNNING]
        ).count(),
    }


def _is_main(session: ConversationSession) -> bool:
    return (session.metadata or {}).get("bot_role") in ("root", "main")


def _upward_only(ctx: ToolContext, target: Bot) -> bool:
    """``target`` is reachable only as this bot's parent (not downward or sideways)."""
    registry = ctx.bot.registry
    return (
        registry is not None
        and registry.is_parent(ctx.bot, target.name)
        and target.name not in ctx.bot.definition.call_bots
    )


def _started_here(ctx: ToolContext, session: ConversationSession) -> bool:
    """A chat of this bot (with this user) started ``session``."""
    started_by = (session.metadata or {}).get("started_by")
    if not started_by:
        return session.bot_name == ctx.bot.name
    return (
        ctx.bot.sessions().filter(user_id=ctx.session.user_id, id=started_by).exists()
    )


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
    meta = session.metadata or {}
    row = {
        "thread": _thread_key(session),
        "id": str(session.id),
        "title": messaging.label(session),
        "status": "archived" if session.status == "completed" else session.status,
        "last_activity": session.updated_at.isoformat(timespec="seconds"),
        "open_requests": open_requests,
    }
    if meta.get("started_by_label"):
        row["started_by"] = meta["started_by_label"]
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
        f"- {b.name}: {b.definition.description or 'no description'}"
        + (
            " (your parent bot: its main chat only)"
            if b.name == ctx.bot.parent_name
            else ""
        )
        for b in bots
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
        "ask": {
            "type": "boolean",
            "description": (
                "Only for messages upward (to your own main chat, or your parent bot's): "
                "true when you need an answer back. Otherwise it's a one-way report and "
                "no reply comes back."
            ),
        },
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
    ask: bool = False,
) -> dict:
    """Send a message to a bot's main chat, a named chat, a thread, or a new thread.

    Returns at once. The reply arrives later as a new message in this chat, except
    for a report upward (see ``ask``).
    """
    return _send(ctx, _target(ctx, bot), message, thread, title, attachments, ask=ask)


def _send(  # noqa: PLR0913
    ctx: ToolContext,
    target: Bot,
    message: str,
    thread: str = "main",
    title: str = "",
    attachments: list[str] | None = None,
    *,
    ask: bool = False,
) -> dict:
    user = ctx.session.user
    thread = (thread or "main").strip()
    if _upward_only(ctx, target) and (
        thread not in ("main", "root") or not _is_main(ctx.session)
    ):
        msg = (
            f"{target.name} is your parent bot: only your main chat may message it, "
            "and only its main chat."
        )
        raise ValueError(msg)
    shared = [
        messaging.shared_file(find_session_file(ctx.session, ref))
        for ref in attachments or []
    ]
    if thread in ("main", "root") or thread in target.definition.chats:
        recipient = async_to_sync(target.chat_session)(user, thread)
    elif thread == "new":
        if not target.definition.allow_create_sessions:
            if target is ctx.bot:
                msg = (
                    "This bot may not start threads of its own (threads.allow_create)."
                )
            else:
                msg = (
                    f"{target.name} doesn't take new threads (threads.allow_create); "
                    "message its main chat instead."
                )
            raise ValueError(msg)
        recipient = async_to_sync(target.create_session)(
            user,
            parent=ctx.session if target is ctx.bot else None,
            title=title or messaging.snippet(message)[:60],
            metadata={
                "started_by": str(ctx.session.id),
                "started_by_bot": ctx.bot.name,
                "started_by_label": messaging.label(ctx.session),
            },
        )
    else:
        recipient = _session(target, ctx, thread)
    if recipient.id == ctx.session.id:
        msg = "That is this chat; send it somewhere else."
        raise ValueError(msg)
    _refuse_nudge_after_reply(ctx, recipient, message)
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
    # Upward (a thread to its main chat, a main chat to its parent's) is a report
    # unless it asks: no reply goes back, so a status update doesn't cost a turn
    # here for the recipient's acknowledgement.
    upward = _is_main(recipient) and (
        (target is ctx.bot and not _is_main(ctx.session)) or _upward_only(ctx, target)
    )
    report = upward and not ask
    metadata = {"attachments": shared} if shared else {}
    if report:
        metadata["report"] = True
    sent = messaging.send(
        ctx.session,
        recipient,
        message,
        registry=ctx.bot.registry,
        metadata=metadata or None,
    )
    return {
        "sent_to": messaging.label(recipient),
        "thread_id": str(recipient.id),
        "message_id": str(sent.id),
        "note": (
            "Sent as a report: no reply comes back (ask=true when you need one)."
            if report
            else "The reply will arrive as a new message in this chat."
        ),
        **({"shared_files": [f["filename"] for f in shared]} if shared else {}),
    }


def _refuse_nudge_after_reply(
    ctx: ToolContext, recipient: ConversationSession, message: str
) -> None:
    """Refuse a short message back to the chat whose reply this turn is handling.

    "Please continue with the work already started" right after a reply starts a
    full turn there for nothing. Answering a question it asked is fine, and so is
    a complete new request (``FOLLOW_UP_MIN_CHARS`` or more).
    """
    handling = messaging.current_thread_message(ctx.session)
    if (
        handling is None
        or handling.in_reply_to_id is None
        or handling.sender_session_id != recipient.id
        or handling.text.startswith("[They ask]")
        or len(message.strip()) >= FOLLOW_UP_MIN_CHARS
    ):
        return
    msg = (
        f"{messaging.label(recipient)} just replied and this turn is handling that reply. "
        "Don't send it a nudge, thanks, or 'continue' message: tell the user what came "
        "back. If the user asked for a next step there, send one complete request with "
        "everything it needs, or wait for the user's next message."
    )
    raise ValueError(msg)


def _managed_thread(ctx: ToolContext, bot: str, thread_id: str) -> ConversationSession:
    """A thread this bot may stop or archive: one of its own, or another bot's
    thread that a chat of this bot started."""
    target = _target(ctx, bot)
    thread = _session(target, ctx, thread_id)
    if (thread.metadata or {}).get("bot_role") in TOP_ROLES:
        msg = "Main and named chats can't be stopped or archived from another chat."
        raise ValueError(msg)
    if target is not ctx.bot and not _started_here(ctx, thread):
        msg = f"{messaging.label(thread)} wasn't started by this bot."
        raise ValueError(msg)
    return thread


@bot_tool(takes_context=True)
def ergo_thread_stop(ctx: ToolContext, thread_id: str, bot: str = "") -> dict:
    """Stop the running turn of a thread this bot started (default bot: this one)
    and cancel the messages this bot queued for it. Use it for work that is no
    longer wanted or has gone wrong."""
    thread = _managed_thread(ctx, bot, thread_id)
    own = ctx.bot.sessions().filter(user_id=ctx.session.user_id)
    cancelled = ThreadMessage.objects.filter(
        recipient_session=thread,
        sender_session__in=own,
        status=ThreadMessageStatus.QUEUED,
    ).update(status=ThreadMessageStatus.FAILED, error="cancelled by the sender")
    stopper = api_settings.TURN_STOPPER
    stopped = None if stopper is None else bool(stopper(str(thread.id)))
    result = {"thread": messaging.label(thread), "cancelled_messages": cancelled}
    if stopped is None:
        result["note"] = (
            "This app can't stop a running turn; it will finish on its own."
        )
    else:
        result["stopped_running_turn"] = stopped
    return result


@bot_tool(takes_context=True)
def ergo_thread_archive(ctx: ToolContext, thread_id: str, bot: str = "") -> str:
    """Archive a thread that is done: one of this bot's, or a sub-bot thread this
    bot started (``bot``). Its history stays readable."""
    thread = _managed_thread(ctx, bot, thread_id)
    archival.archive(thread, reason=f"archived by {ctx.bot.name}")
    return f"Archived {messaging.label(thread)}"


@bot_tool(
    takes_context=True,
    parameters={
        "message": {"type": "string", "description": "What you want to report or ask"},
        "to": {
            "type": "string",
            "enum": ["main", "parent"],
            "description": (
                '"main" for this bot\'s main chat, "parent" for the parent bot\'s main '
                "chat (from this bot's main chat only)"
            ),
        },
        "attachments": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files from this chat to share (filenames or file ids)",
        },
        "ask": {
            "type": "boolean",
            "description": (
                "true when you need an answer back. Otherwise it's a one-way report and "
                "no reply comes back."
            ),
        },
    },
    required=["message", "to"],
)
def ergo_message_up(
    ctx: ToolContext,
    message: str,
    to: str,
    attachments: list[str] | None = None,
    ask: bool = False,
) -> dict:
    """Message upward: this bot's main chat, or the parent bot's main chat.

    Returns at once. A report gets no reply; with ``ask`` the reply arrives later
    as a new message in this chat.
    """
    if to == "parent":
        registry = ctx.bot.registry
        if registry is None or not registry.is_parent(ctx.bot, ctx.bot.parent_name):
            msg = "This bot has no parent bot."
            raise ValueError(msg)
        if not _is_main(ctx.session):
            msg = (
                "Only the main chat may message the parent bot; message your main chat."
            )
            raise ValueError(msg)
        target = registry.get(ctx.bot.parent_name)
    elif to == "main":
        target = ctx.bot
    else:
        msg = 'to must be "main" or "parent"'
        raise ValueError(msg)
    return _send(ctx, target, message, "main", attachments=attachments, ask=ask)


def upward_targets(ctx: ToolContext) -> list[str]:
    """Where ``ergo_message_up`` can send from this chat."""
    targets = [] if _is_main(ctx.session) else ["main"]
    registry = ctx.bot.registry
    if (
        _is_main(ctx.session)
        and registry
        and registry.is_parent(ctx.bot, ctx.bot.parent_name)
    ):
        targets.append("parent")
    return targets


def upward_toolkit(ctx: ToolContext) -> FunctionToolkit:
    """For bots without orchestration: just the upward messages."""
    if not upward_targets(ctx):
        return FunctionToolkit([], ctx)
    return FunctionToolkit([ergo_message_up.__bot_tool__], ctx)


def orchestrator_toolkit(ctx: ToolContext) -> FunctionToolkit:
    functions = [
        ergo_bot_list,
        ergo_thread_list,
        ergo_thread_send,
        ergo_thread_stop,
        ergo_thread_archive,
    ]
    tools: list[BotTool] = [fn.__bot_tool__ for fn in functions]
    # The bots it can reach, and their chats, are in the "Bots and threads"
    # context block (bots.overview), so nothing is pre-seeded.
    return FunctionToolkit(tools, ctx)
