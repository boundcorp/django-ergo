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
  ``threads.allow_create: true``; a thread of another bot records who started it.
  A busy recipient never makes a send fail: the message is queued
  (``status: "queued"``, with its ``queue_position``) and goes out in order as the
  recipient's next turn, one turn per message, without interrupting the current
  one. The one refusal is the no-nudge rule: a chat can't send another message to
  a chat while its earlier *request* there (one whose reply comes back) is still
  open. A report or a forward gets no reply, so it never blocks a later send.
  ``interrupt=true`` replaces a request of this chat that the recipient is working
  on right now (it stops that turn, and the message goes out next in the queue);
  it never stops a turn answering the user or another chat, and then just queues.
  Files from this chat can go with it (``attachments``): the recipient sees them
  listed, with ids, and opens them with the attachments tools.
- ``ergo_thread_forward``: hand the user's own message (the one this turn is
  answering) to another chat or thread, copied verbatim with its author, time
  and files, plus a short note. The recipient treats it as the user speaking and
  answers there; nothing comes back here, so this chat just says where it went.
- ``ergo_thread_stop``: stop the running turn of a thread this bot started (or
  one of its own threads) and cancel what this bot queued for it. Stopping a
  running turn needs ``DJANGO_ERGO["TURN_STOPPER"]``.
- ``ergo_thread_resolve``: resolve a finished thread (this one, one of this
  bot's, or another bot's thread this bot started). Refused while workers run, a
  request is open, an approval is pending, or its last reply asks the user
  something. History stays readable; a new message reopens it.
  ``ergo_thread_archive`` is a deprecated alias.
- ``ergo_message_up``: message this bot's main chat, or the parent bot's main
  chat from this bot's main chat (always available, even without orchestration).

Reading another session's history is done with the history tools, which
cover every session this bot has with the user.
"""

from __future__ import annotations

from datetime import timedelta
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
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCallStatus
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
# A thread this quiet, with nothing open, is shown as ready to resolve.
READY_AFTER = timedelta(minutes=15)
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


ORCHESTRATION_INSTRUCTIONS = """\
Routing: when the user's message belongs to work that another chat or thread
already owns (or should own), hand it over with ergo_thread_forward instead of
retelling it with ergo_thread_send. Forwarding copies the user's own words, so
their intent and any approval reach the thread unchanged; add a note only for
context the thread lacks. Then reply here in one line saying where it went.
Use ergo_thread_send for your own requests and questions.

Resolving threads: keep the thread list to work that is still going on.
- Resolve a thread (ergo_thread_resolve, with a one-line summary) when its work is
  finished: its PR was merged or closed, its answer was delivered, or the user
  wrapped it up. A thread resolves itself after its final report when nothing is
  left open.
- Never resolve one that is waiting on the user (its last reply asks something),
  on a worker, on a reply, or for an approval, or that has an open PR. The tool
  refuses the first ones; open PRs are up to you.
- Each turn, check the Bots and threads block: threads marked "ready to resolve"
  look finished, and you may resolve them. A finished thread of another bot that
  is not marked that way is its owner's to close: leave it.
- Resolving keeps the history readable, and a new message reopens the thread."""


def thread_status(session: ConversationSession) -> dict:
    """What a chat is doing, for orchestrators and UIs (the "Bots and threads"
    block uses it; so can an API).

    ``state`` is ``working``, ``waiting_for_approval`` or ``idle``;
    ``last_activity`` is when its latest turn moved (sessions aren't touched per
    turn); ``started_by``/``started_by_id`` name another bot's chat that started
    it; ``working_for``/``waiting_on`` count open requests in and out;
    ``workers_running`` counts queued or running workers; ``last_asks`` is
    whether its last reply was a question to the user; ``ready_to_resolve`` marks
    a thread that looks finished (quiet, nothing open, not asking anything).
    """
    from django.utils import timezone

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
    last_reply = (
        calls.filter(kind="chat_reply", status=StructuredCallStatus.COMPLETED)
        .order_by("-created_at")
        .values_list("response", flat=True)
        .first()
    )
    last_asks = isinstance(last_reply, dict) and last_reply.get("type") == "question"
    other_bot = meta.get("started_by_bot") not in (None, "", session.bot_name)
    status = {
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
        "last_asks": last_asks,
    }
    quiet = timezone.now() - status["last_activity"] > READY_AFTER
    status["ready_to_resolve"] = (
        meta.get("bot_role") not in TOP_ROLES
        and session.status != "completed"
        and quiet
        and not resolve_blockers(status)
    )
    return status


def resolve_blockers(
    status: dict, *, skip_open: int = 0, own_turn: bool = False
) -> list[str]:
    """Why a thread can't be resolved yet (empty: it can). In the thread's own turn
    (``own_turn``), the request it is answering (``skip_open``) and a question its
    previous reply asked (this turn is the answer) don't count."""
    blockers = []
    if status["state"] == "waiting_for_approval":
        blockers.append("an approval is pending")
    if status["workers_running"]:
        blockers.append(f"{status['workers_running']} worker(s) are running")
    if status["working_for"] - skip_open > 0:
        blockers.append("it is still working on a request")
    if status["waiting_on"]:
        blockers.append("it is waiting on a reply")
    if status["last_asks"] and not own_turn:
        blockers.append("its last reply asks the user something")
    return blockers


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


def can_resolve(bot: Bot, user_id, session: ConversationSession) -> bool:
    """Whether ``bot`` may stop or resolve ``session`` (of a chat with ``user_id``):
    the thread belongs to ``bot``, or a chat of ``bot`` with this user started it.
    One bot closing another's thread could end work its owner still tracks, so a
    thread a bot didn't start stays with the bot it belongs to (or its starter).
    The tools check this and the "Bots and threads" block marks only the threads
    that pass."""
    if session.bot_name == bot.name:
        return True
    started_by = (session.metadata or {}).get("started_by")
    return bool(
        started_by and bot.sessions().filter(user_id=user_id, id=started_by).exists()
    )


def _refusal(session: ConversationSession, action: str) -> str:
    """Why this bot can't ``action`` the thread, and who can."""
    meta = session.metadata or {}
    owner = session.bot_name
    starter = meta.get("started_by_bot")
    if starter and starter != owner:
        who = f"{owner} (its bot) or {starter} (which started it)"
        why = f"belongs to {owner} and was started by {meta.get('started_by_label') or starter}"
    else:
        who = owner
        why = f"was started by {owner}"
    tail = (
        "It resolves itself when its work is done"
        if action == "resolve"
        else "If it has gone wrong"
    )
    return (
        f"{messaging.label(session)} {why}, not by this bot, so only {who} can "
        f"{action} it. {tail}; if it needs attention, message {owner} with "
        "ergo_thread_send."
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
        "status": "resolved" if session.status == "completed" else session.status,
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
        "interrupt": {
            "type": "boolean",
            "description": (
                "Only to replace a request of yours that the thread is working on right "
                "now: stops that turn, and this message goes out next in the thread's "
                "queue. It never stops a turn that answers the user (their message, or "
                "one you forwarded) or another chat; then the message just queues, and "
                "the result says so. Default false: it waits for the current turn."
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
    interrupt: bool = False,
) -> dict:
    """Send a message to a bot's main chat, a named chat, a thread, or a new thread.

    Returns at once. The reply arrives later as a new message in this chat, except
    for a report upward (see ``ask``). A busy thread doesn't refuse it: it is
    queued (``status: "queued"``) and goes out in order as the thread's next turn.
    """
    return _send(
        ctx,
        _target(ctx, bot),
        message,
        thread,
        title,
        attachments,
        ask=ask,
        interrupt=interrupt,
    )


def user_message(session: ConversationSession) -> dict | None:
    """The user's own message this session's running turn answers: its text,
    author, time and files. None when the turn answers another chat, a worker
    or a schedule rather than the user."""
    call = (
        session.structured_calls.filter(status=StructuredCallStatus.IN_PROGRESS)
        .order_by("-created_at")
        .first()
    )
    if call is None or (call.metadata or {}).get("thread_message"):
        return None
    files = ConversationAttachment.objects.filter(
        session=session, message_sequence__gte=call.first_sequence or 0
    )
    if call.last_sequence is not None:
        files = files.filter(message_sequence__lte=call.last_sequence)
    user = session.user
    return {
        "text": call.request,
        "author": (user.get_full_name() or user.get_username()) if user else "",
        "user_id": session.user_id,
        "sent_at": call.created_at.isoformat(timespec="seconds"),
        "source_call": str(call.id),
        "attachments": [messaging.shared_file(f) for f in files],
    }


@bot_tool(
    takes_context=True,
    parameters={
        "thread": {
            "type": "string",
            "description": (
                'Where it goes: a thread id, "main", a named chat, or "new" for a '
                "new thread"
            ),
        },
        "bot": {
            "type": "string",
            "description": "Which bot: empty for this bot, or a name from the Bots and threads block",
        },
        "note": {
            "type": "string",
            "description": (
                "Optional: one or two lines of context the thread lacks. The user's "
                "words go as they are; don't retell them here."
            ),
        },
        "title": {"type": "string", "description": "Title for a new thread"},
    },
    required=["thread"],
)
def ergo_thread_forward(
    ctx: ToolContext, thread: str, bot: str = "", note: str = "", title: str = ""
) -> dict:
    """Hand the user's message (the one you're answering) to the chat or thread
    that owns that work, word for word with its author, time and files.

    The recipient treats it as the user speaking and answers there; nothing comes
    back here. Then tell the user in one line where it went.
    """
    original = user_message(ctx.session)
    if original is None:
        msg = (
            "This turn isn't answering a message from the user, so there's nothing "
            "to forward; use ergo_thread_send for your own request."
        )
        raise ValueError(msg)
    target = _target(ctx, bot)
    if _upward_only(ctx, target):
        msg = f"{target.name} is your parent bot; report to it with ergo_message_up."
        raise ValueError(msg)
    thread = (thread or "").strip()
    if not thread:
        msg = 'Name the thread: an id, "main", a named chat, or "new".'
        raise ValueError(msg)
    recipient = _recipient(
        ctx, target, thread, title or messaging.snippet(original["text"])[:60]
    )
    files = original.pop("attachments")
    metadata = {
        "forwarded": {**original, "from_label": messaging.label(ctx.session)},
        # The recipient answers the user where it is: no reply comes back here.
        "report": True,
        **({"note": note.strip()} if note.strip() else {}),
        **({"attachments": files} if files else {}),
    }
    sent = messaging.send(
        ctx.session,
        recipient,
        original["text"],
        registry=ctx.bot.registry,
        metadata=metadata,
    )
    where = messaging.label(recipient)
    return {
        "forwarded_to": where,
        "thread_id": str(recipient.id),
        "message_id": str(sent.id),
        **_delivery(recipient, sent),
        "note": (
            f"Forwarded verbatim; {where} answers the user there and nothing comes "
            f"back here. Tell the user in one line: sent to {where}."
        ),
        **({"shared_files": [f["filename"] for f in files]} if files else {}),
    }


def _recipient(
    ctx: ToolContext, target: Bot, thread: str, title: str
) -> ConversationSession:
    """The chat ``thread`` names on ``target``: main, a named chat, "new" or an id."""
    user = ctx.session.user
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
            title=title,
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
    return recipient


def _send(  # noqa: PLR0913
    ctx: ToolContext,
    target: Bot,
    message: str,
    thread: str = "main",
    title: str = "",
    attachments: list[str] | None = None,
    *,
    ask: bool = False,
    interrupt: bool = False,
) -> dict:
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
    recipient = _recipient(
        ctx, target, thread, title or messaging.snippet(message)[:60]
    )
    _refuse_nudge_after_reply(ctx, recipient, message)
    interrupting, not_interrupted = (
        _interruptible(ctx, recipient) if interrupt else (None, "")
    )
    _refuse_while_request_open(
        ctx,
        recipient,
        replacing=interrupting,
        interrupt_note=not_interrupted if interrupt else "",
    )
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
    interrupted = False
    if interrupting:
        # Before storing the message: a stop that landed after it could hit the turn
        # that answers it, and the message is queued whenever that turn ends.
        interrupted = bool(api_settings.TURN_STOPPER(str(recipient.id)))
    sent = messaging.send(
        ctx.session,
        recipient,
        message,
        registry=ctx.bot.registry,
        metadata=metadata or None,
    )
    delivery = _delivery(recipient, sent)
    note = (
        "Sent as a report: no reply comes back (ask=true when you need one)."
        if report
        else "The reply will arrive as a new message in this chat."
    )
    if interrupt:
        note += " " + _interrupt_outcome(interrupting, interrupted, not_interrupted)
    if delivery["status"] == "queued":
        note += (
            f" Queued (position {delivery['queue_position']}): it goes out in order, "
            "as a turn of its own, after what the thread is doing and has waiting."
        )
    return {
        "sent_to": messaging.label(recipient),
        "thread_id": str(recipient.id),
        "message_id": str(sent.id),
        **delivery,
        **({"interrupted": interrupted} if interrupt else {}),
        "note": note,
        **({"shared_files": [f["filename"] for f in shared]} if shared else {}),
    }


def _interrupt_outcome(
    interrupting: ThreadMessage | None, interrupted: bool, not_interrupted: str
) -> str:
    if interrupted:
        return "Its running turn was stopped; this goes out in its place."
    if interrupting:
        return "Its running turn had already ended; nothing was stopped."
    return f"Not interrupted: {not_interrupted}."


def _refuse_while_request_open(
    ctx: ToolContext,
    recipient: ConversationSession,
    *,
    replacing: ThreadMessage | None,
    interrupt_note: str,
) -> None:
    """The no-nudge rule: refuse a message to a chat while this chat's earlier request
    there is still open (its reply is coming; each message starts a full turn there).

    A report or a forward gets no reply, so it isn't a request to wait on, and the
    request an ``interrupt`` is replacing (``replacing``) is already being ended.
    """
    open_requests = ctx.session.sent_thread_messages.filter(
        recipient_session=recipient,
        in_reply_to__isnull=True,
        status__in=OPEN_STATUSES,
    )
    if replacing:
        open_requests = open_requests.exclude(id=replacing.id)
    open_request = next(
        (
            m
            for m in open_requests.order_by("-created_at")
            if not (m.metadata or {}).get("report")
        ),
        None,
    )
    if open_request is None:
        return
    msg = (
        f"Your request to {messaging.label(recipient)} from "
        f"{open_request.created_at:%H:%M} UTC (“{messaging.snippet(open_request.text)}”) "
        "is still open; its reply will arrive here. Don't nudge or add to it: wait "
        "for the reply, then send new work if there is any."
    )
    if interrupt_note:
        msg += f" (interrupt didn't apply: {interrupt_note}.)"
    raise ValueError(msg)


def _delivery(recipient: ConversationSession, sent: ThreadMessage) -> dict:
    """Whether a message just sent waits its turn, and where in line: a busy
    recipient (or earlier messages for it) queues it, in order."""
    status = (
        ThreadMessage.objects.filter(id=sent.id)
        .values_list("status", flat=True)
        .first()
    )
    if status == ThreadMessageStatus.QUEUED:
        ahead = ThreadMessage.objects.filter(
            recipient_session=recipient,
            status=ThreadMessageStatus.QUEUED,
            created_at__lt=sent.created_at,
        ).count()
        if ahead or messaging.busy(recipient):
            return {"status": "queued", "queue_position": ahead + 1}
    return {"status": "sent"}


def _interruptible(
    ctx: ToolContext, recipient: ConversationSession
) -> tuple[ThreadMessage | None, str]:
    """The request of this chat whose running turn an ``interrupt`` may stop, or why
    none can be.

    Only a turn this chat started by messaging the thread qualifies. A turn that
    answers the user (their message, a forward of it, a schedule or a worker) or
    another chat is never stopped from here: the new message queues behind it.
    """
    if not messaging.busy(recipient):
        return None, "nothing is running there"
    running = messaging.current_thread_message(recipient)
    if running is not None and "forwarded" in (running.metadata or {}):
        return None, (
            "its running turn answers a message of the user's that you forwarded, "
            "and a bot never cancels the user's request"
        )
    if running is None or running.sender_session_id != ctx.session.id:
        return None, (
            "its running turn isn't answering a request of yours (only your own "
            "request can be interrupted, never the user's)"
        )
    if api_settings.TURN_STOPPER is None:
        return None, "this app can't stop a running turn"
    return running, ""


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


def _managed_thread(
    ctx: ToolContext, bot: str, thread_id: str, action: str
) -> ConversationSession:
    """A thread this bot may stop or resolve (``can_resolve``)."""
    target = _target(ctx, bot)
    thread = _session(target, ctx, thread_id)
    if (thread.metadata or {}).get("bot_role") in TOP_ROLES:
        msg = "Main and named chats can't be stopped or resolved from another chat."
        raise ValueError(msg)
    if not can_resolve(ctx.bot, ctx.session.user_id, thread):
        raise ValueError(_refusal(thread, action))
    return thread


@bot_tool(takes_context=True)
def ergo_thread_stop(ctx: ToolContext, thread_id: str, bot: str = "") -> dict:
    """Stop the running turn of a thread this bot started (default bot: this one)
    and cancel the messages this bot queued for it. Use it for work that is no
    longer wanted or has gone wrong."""
    thread = _managed_thread(ctx, bot, thread_id, "stop")
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


def resolve(
    ctx: ToolContext, thread_id: str = "", bot: str = "", summary: str = ""
) -> str:
    """Resolve a finished thread (see ``ergo_thread_resolve``)."""
    current = ctx.session
    if not thread_id or thread_id == str(current.id):
        if (current.metadata or {}).get("bot_role") in TOP_ROLES:
            msg = "Main and named chats aren't resolved; only threads are."
            raise ValueError(msg)
        thread, own_turn = current, True
    else:
        thread, own_turn = _managed_thread(ctx, bot, thread_id, "resolve"), False
    status = thread_status(thread)
    if not own_turn and status["state"] == "working":
        msg = f"{messaging.label(thread)} is working; stop it with ergo_thread_stop first."
        raise ValueError(msg)
    handling = messaging.current_thread_message(thread) if own_turn else None
    open_here = int(bool(handling and handling.in_reply_to_id is None))
    if blockers := resolve_blockers(status, skip_open=open_here, own_turn=own_turn):
        msg = f"{messaging.label(thread)} isn't finished: " + "; ".join(blockers) + "."
        raise ValueError(msg)
    archival.archive(thread, reason=f"resolved by {messaging.label(current)}")
    meta = {**(thread.metadata or {}), "resolved_by": messaging.label(current)}
    if summary:
        meta["resolved_summary"] = summary[:2000]
    ConversationSession.objects.filter(pk=thread.pk).update(metadata=meta)
    thread.metadata = meta
    return f"Resolved {messaging.label(thread)}. Its history stays readable; a new message reopens it."


@bot_tool(
    takes_context=True,
    parameters={
        "thread_id": {
            "type": "string",
            "description": "The thread to resolve; empty for this thread",
        },
        "bot": {
            "type": "string",
            "description": "Its bot, for a thread of another bot this bot started",
        },
        "summary": {
            "type": "string",
            "description": "One line: how it ended (e.g. 'PR #88 merged')",
        },
    },
)
def ergo_thread_resolve(
    ctx: ToolContext, thread_id: str = "", bot: str = "", summary: str = ""
) -> str:
    """Resolve a finished thread: this one (empty thread_id), one of this bot's, or
    another bot's thread this bot started (the Bots and threads block marks the
    ones you may resolve as "ready to resolve"; any other bot's thread is its
    owner's to close). Resolve when the work is finished (PR merged or closed, the
    answer delivered, the user wrapped it up); never while it waits on the user, a
    worker or a reply, or has an open PR. Its history stays readable, and a new
    message reopens it."""
    return resolve(ctx, thread_id, bot, summary)


@bot_tool(takes_context=True)
def ergo_thread_archive(ctx: ToolContext, thread_id: str, bot: str = "") -> str:
    """Deprecated: use ergo_thread_resolve (the same thing)."""
    return resolve(ctx, thread_id, bot)


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
    tools = [ergo_message_up.__bot_tool__] if upward_targets(ctx) else []
    if (
        not _is_main(ctx.session)
        and (ctx.session.metadata or {}).get("bot_role") not in TOP_ROLES
    ):
        tools.append(ergo_thread_resolve.__bot_tool__)  # a thread may resolve itself
    return FunctionToolkit(tools, ctx)


def orchestrator_toolkit(ctx: ToolContext) -> FunctionToolkit:
    functions = [
        ergo_bot_list,
        ergo_thread_list,
        ergo_thread_send,
        ergo_thread_forward,
        ergo_thread_stop,
        ergo_thread_resolve,
        ergo_thread_archive,
    ]
    tools: list[BotTool] = [fn.__bot_tool__ for fn in functions]
    # The bots it can reach, and their chats, are in the "Bots and threads"
    # context block (bots.overview), so nothing is pre-seeded.
    return FunctionToolkit(tools, ctx)
