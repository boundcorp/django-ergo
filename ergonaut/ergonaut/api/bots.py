"""The web app's API: bots, sessions, transcripts and turns.

Everything is scoped to the signed-in user (a superuser sees everyone's
sessions). A turn runs inside the request and returns the bot's ChatReply.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from asgiref.sync import sync_to_async
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import aget_object_or_404
from django_ergo.bots import archival, webhooks
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.models import ConversationAttachment, ConversationSession, StructuredCall
from ninja import File, Router, Schema, UploadedFile
from ninja.errors import HttpError
from ninja.security import django_auth

from ergonaut.apps.bots.tasks import queue_turn

router = Router(tags=["bots"], auth=django_auth)


# -- schemas -----------------------------------------------------------------


class ChatOut(Schema):
    name: str  # "main" or a named chat from bot.yaml
    description: str
    session_id: str | None  # None until the user first opens it


class BotOut(Schema):
    name: str
    description: str
    orchestration: bool
    knowledge: bool
    parent: str
    root_session_id: str | None  # the main chat's session (name kept for clients)
    chats: list[ChatOut] = []


class ToolOut(Schema):
    name: str
    description: str
    requires_approval: bool


class SkillOut(Schema):
    name: str
    description: str
    body: str
    source: str = ""
    tools: list[ToolOut] = []
    always_in: list[str] = []  # chats (and "threads") that load it from the start


class BotDetailOut(BotOut):
    engine: str
    model: str
    timezone: str
    folder: str
    instructions: str
    plugins: list[str]
    tools: list[ToolOut]
    skills: list[SkillOut]
    manages_repo: bool = False  # has bot_management: show the Changes section
    schedules: list[dict] = []
    jobs: list[dict] = []


class SessionOut(Schema):
    id: str
    bot: str
    title: str
    role: str
    parent_id: str | None
    status: str
    username: str
    created_at: datetime
    updated_at: datetime
    open_in: int = 0  # delegated requests this session is still answering
    open_out: int = 0  # requests it sent that are still unanswered


class RequestOut(Schema):
    id: str
    direction: str  # "in": sent to this session; "out": sent by it
    other_session_id: str | None
    other: str
    text: str
    status: str
    reply: str
    created_at: datetime
    updated_at: datetime


class MessageOut(Schema):
    line: int
    role: str
    blocks: list[dict]
    timestamp: datetime | None


class CallOut(Schema):
    id: str
    kind: str
    status: str
    request: str
    response: Any
    error: str
    first_sequence: int | None
    last_sequence: int | None
    model_name: str
    input_tokens: int
    output_tokens: int
    turns_used: int
    pending_approvals: list[dict]
    tools: list[str]
    created_at: datetime


class CallDetailOut(CallOut):
    system_prompt: str
    transcript: list
    metadata: dict


class SessionDetailOut(Schema):
    session: SessionOut
    messages: list[MessageOut]
    calls: list[CallOut]
    requests: list[RequestOut] = []


class NewThreadIn(Schema):
    title: str = ""


class MessageIn(Schema):
    text: str
    # Files already uploaded to the session, to send with this message so
    # the model sees them (images and PDFs natively).
    attachment_ids: list[str] = []


class ApprovalIn(Schema):
    approve: bool
    # The tool calls the user was shown; if the turn now waits on others, nothing runs.
    approval_ids: list[str] | None = None


class TurnOut(Schema):
    session_id: str
    call_id: str | None
    type: str | None
    text: str
    suggestions: list[str]
    approvals: list[dict]
    error: str
    queued: bool = False  # a worker runs the turn; follow it over /events


# -- helpers -----------------------------------------------------------------


def registry():
    found = webhooks.get_registry()
    if found is None:
        raise HttpError(503, "No bots are loaded")
    return found


def schedules_out(bot: Bot, user) -> list[dict]:
    """The bot's schedules and their steps, with the next run in the viewer's timezone."""
    from django.utils import timezone
    from django_ergo.bots.schedules import local_now

    now = local_now(bot, user, timezone.now())
    out = []
    for schedule in bot.definition.schedules:
        upcoming = schedule.cron.next_after(now) if schedule.enabled else None
        out.append(
            {
                "name": schedule.name,
                "cron": schedule.cron.expression,
                "users": list(schedule.users),
                "enabled": schedule.enabled,
                "next_run": upcoming.isoformat() if upcoming else None,
                "actions": [
                    {
                        "kind": a.kind,
                        "message": a.message,
                        "to": a.to,
                        "thread_title": a.thread_title,
                        "thread_in": a.thread_in,
                        "run": f"{a.path}:{a.function}" if a.kind == "run" else "",
                        "args": a.args,
                    }
                    for a in schedule.actions
                ],
            }
        )
    return out


def jobs_out(bot: Bot) -> list[dict]:
    """The bot's latest background jobs (schedule run steps, tasks)."""
    from django_ergo.conversation.models import BotJob

    return [
        {
            "id": job.id,
            "name": job.name,
            "target": job.target,
            "status": job.status,
            "error": job.error,
            "result": job.result,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
        }
        for job in BotJob.objects.filter(bot_name=bot.name)[:20]
    ]


def may_use(bot: Bot, user) -> bool:
    """permissions.users in bot.yaml limits a bot to those people (admins always)."""
    allowed = bot.definition.allowed_users
    return not allowed or user is None or user.is_superuser or user.get_username() in allowed


def get_bot(name: str, user=None) -> Bot:
    bots = registry()
    if name not in bots or not may_use(bots.get(name), user):
        raise HttpError(404, f"No bot {name!r}")
    return bots.get(name)


ADMIN_ONLY_PLUGINS = ("bash", "orca", "bot_management")


def require_admin_for_risky_tools(bot: Bot, user) -> None:
    """Shell, Orca and repo tools act on the host: only an admin may approve them."""
    if not user.is_superuser and any(bot.plugin(name) for name in ADMIN_ONLY_PLUGINS):
        raise HttpError(403, "Only an admin can approve this bot's tools")


def uuid_or_404(value: str, what: str = "session") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise HttpError(404, f"No such {what}") from None


def session_out(session: ConversationSession) -> dict:
    meta = session.metadata or {}
    return {
        "id": str(session.id),
        "bot": session.bot_name,
        "title": meta.get("title") or ("Main" if meta.get("bot_role") in ("root", "main") else "Thread"),
        "role": meta.get("bot_role") or "",
        "parent_id": str(session.parent_id) if session.parent_id else None,
        "status": session.status,
        "username": session.user.get_username() if session.user_id else "",
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "open_in": getattr(session, "open_in", 0) or 0,
        "open_out": getattr(session, "open_out", 0) or 0,
    }


OPEN_REQUESTS = ["queued", "delivered", "waiting"]


def with_open_counts(qs):
    """Annotate sessions with their open delegated requests, in and out."""
    return qs.annotate(
        open_in=Count(
            "thread_messages",
            filter=Q(
                thread_messages__status__in=OPEN_REQUESTS,
                thread_messages__in_reply_to__isnull=True,
                thread_messages__sender_session__isnull=False,
            ),
            distinct=True,
        ),
        open_out=Count(
            "sent_thread_messages",
            filter=Q(
                sent_thread_messages__status__in=OPEN_REQUESTS,
                sent_thread_messages__in_reply_to__isnull=True,
            ),
            distinct=True,
        ),
    )


def requests_out(session: ConversationSession, limit: int = 20) -> list[dict]:
    """Recent delegated requests to and from a session, newest first."""
    from django_ergo.bots.messaging import label, snippet
    from django_ergo.conversation.models import ThreadMessage

    rows = (
        ThreadMessage.objects.filter(in_reply_to__isnull=True)
        .filter(Q(recipient_session=session) | Q(sender_session=session))
        .exclude(sender_session__isnull=True)
        .select_related("sender_session", "recipient_session")
        .order_by("-created_at")[:limit]
    )
    out = []
    for row in rows:
        incoming = row.recipient_session_id == session.id
        other = row.sender_session if incoming else row.recipient_session
        out.append(
            {
                "id": str(row.id),
                "direction": "in" if incoming else "out",
                "other_session_id": str(other.id) if other else None,
                "other": label(other) if other else "",
                "text": snippet(row.text),
                "status": row.status,
                "reply": snippet(row.reply_text or row.error),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
    return out


def call_out(call: StructuredCall, detail: bool = False) -> dict:
    out = {
        "id": str(call.id),
        "kind": call.kind,
        "status": call.status,
        "request": call.request,
        "response": call.response,
        "error": call.error,
        "first_sequence": call.first_sequence,
        "last_sequence": call.last_sequence,
        "model_name": call.model_name,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "turns_used": call.turns_used,
        "pending_approvals": (call.metadata or {}).get("pending_approvals", []),
        "tools": (call.metadata or {}).get("tools", []),
        "created_at": call.created_at,
    }
    if detail:
        out.update(system_prompt=call.system_prompt, transcript=call.transcript, metadata=call.metadata)
    return out


def visible_sessions(user):
    qs = ConversationSession.objects.exclude(bot_name="").select_related("user")
    return qs if user.is_superuser else qs.filter(user=user)


async def get_session(request, session_id) -> ConversationSession:
    return await aget_object_or_404(visible_sessions(request.auth), id=uuid_or_404(session_id))


# -- endpoints ---------------------------------------------------------------


@router.get("/bots", response=list[BotOut])
async def list_bots(request):
    out = []
    for bot in [b for b in registry() if may_use(b, request.auth)]:
        chats = await sync_to_async(chats_out)(bot, request.auth)
        out.append(
            {
                "name": bot.name,
                "description": bot.definition.description,
                "orchestration": bot.definition.orchestration,
                "knowledge": any(p.name == "ergo_kb" for p in bot.plugins),
                "parent": bot.parent_name,
                "root_session_id": chats[0]["session_id"],
                "chats": chats,
            }
        )
    return out


def chats_out(bot: Bot, user) -> list[dict]:
    """The bot's main chat and named chats, with the user's session for each."""
    sessions = {
        (s.metadata or {}).get("chat") or "main": str(s.id)
        for s in bot.sessions(user)
        .filter(parent__isnull=True, metadata__bot_role__in=["root", "main", "chat"])
        .exclude(status="completed")
        .order_by("-created_at")
    }
    return [
        {"name": name, "description": chat.description, "session_id": sessions.get(name)}
        for name, chat in bot.definition.chats.items()
    ]


@router.get("/sessions", response=list[SessionOut])
def list_sessions(request, bot: str = "", q: str = "", status: str = ""):
    qs = visible_sessions(request.auth)
    if bot:
        qs = qs.filter(bot_name=bot)
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(claude_messages__content_blocks__text__icontains=q)
            | Q(openai_messages__content__icontains=q)
            | Q(metadata__title__icontains=q)
        ).distinct()
    return [session_out(s) for s in with_open_counts(qs).order_by("-updated_at")[:200]]


@router.post("/bots/{bot}/root", response=SessionOut)
async def open_root(request, bot: str):
    """The user's main chat (the endpoint keeps its old name)."""
    session = await get_bot(bot, request.auth).main_session(request.auth)
    session.user = request.auth
    return session_out(session)


@router.post("/bots/{bot}/chats/{name}", response=SessionOut)
async def open_chat(request, bot: str, name: str):
    """Open (creating on first use) the main chat or a named chat."""
    found = get_bot(bot, request.auth)
    if name != "main" and name not in found.definition.chats:
        raise HttpError(404, f"{bot} has no chat named {name!r}")
    session = await found.chat_session(request.auth, name)
    session.user = request.auth
    return session_out(session)


def _tools_and_skills(bot: Bot, user) -> dict:
    skills = skills_out(bot, user)
    tools = [tool for skill in skills for tool in skill["tools"]]
    return {"skills": skills, "tools": tools}


def skills_out(bot: Bot, user) -> list[dict]:
    """Every skill the bot can load, with its tools and where it loads from the start."""
    from django_ergo.bots.skillset import SkillSet
    from django_ergo.conversation.adapters import ClaudeToolAdapter

    probe = ConversationSession(bot_name=bot.name, user=user, metadata={"bot_role": "main"})
    skillset = SkillSet(bot.tool_context(probe), bot.skill_defs)
    always = {name: [] for name in skillset.skills}
    for chat in bot.definition.chats.values():
        for name in SkillSet(bot.tool_context(probe), bot.skill_defs, always=set(chat.skills)).always:
            always.setdefault(name, []).append(chat.name)
    for name in SkillSet(bot.tool_context(probe), bot.skill_defs, always=set(bot.definition.thread_skills)).always:
        always.setdefault(name, []).append("threads")
    out = []
    for name, skill in skillset.skills.items():
        tools = []
        for toolkit in skillset.toolkits_of(name):
            for schema in toolkit.get_tools_schema(ClaudeToolAdapter()):
                tools.append(
                    {
                        "name": schema["name"],
                        "description": schema.get("description", ""),
                        "requires_approval": bool(toolkit.requires_approval(schema["name"])),
                    }
                )
        if not tools and not skill.instructions:
            continue
        out.append(
            {
                "name": name,
                "description": skill.description,
                "body": skill.instructions,
                "source": skill.source,
                "tools": tools,
                "always_in": always.get(name, []),
            }
        )
    return out


@router.get("/bots/{bot}", response=BotDetailOut)
async def bot_detail(request, bot: str):
    found = get_bot(bot, request.auth)
    chats = await sync_to_async(chats_out)(found, request.auth)
    definition = found.definition
    try:
        spec = found.engine_spec()
        engine, model = spec.engine_type, str(spec.config.get("model") or "")
    except Exception:  # noqa: BLE001 - e.g. its API key isn't set
        engine, model = definition.engine_type, str(definition.engine_config.get("model") or "")
    return {
        "name": found.name,
        "description": definition.description,
        "orchestration": definition.orchestration,
        "knowledge": any(p.name == "ergo_kb" for p in found.plugins),
        "parent": found.parent_name,
        "root_session_id": chats[0]["session_id"],
        "chats": chats,
        "engine": engine,
        "model": model,
        "timezone": definition.timezone,
        "folder": str(definition.root_dir or ""),
        "instructions": definition.instructions,
        "plugins": [type(p).__name__ for p in found.plugins],
        "manages_repo": found.plugin("bot_management") is not None,
        "schedules": schedules_out(found, request.auth),
        "jobs": await sync_to_async(jobs_out)(found),
        **await sync_to_async(_tools_and_skills)(found, request.auth),
    }


@router.post("/bots/{bot}/threads", response=SessionOut)
async def new_thread(request, bot: str, data: NewThreadIn):
    found = get_bot(bot, request.auth)
    if not found.definition.orchestration:
        raise HttpError(409, f"{bot} has threads turned off (orchestration: false)")
    root = await found.root_session(request.auth)
    session = await found.create_session(request.auth, parent=root, title=data.title)
    session.user = request.auth
    return session_out(session)


@router.get("/sessions/{session_id}", response=SessionDetailOut)
def session_detail(request, session_id: str):
    session = visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).first()
    if session is None:
        raise HttpError(404, "No such session")
    messages = [
        {"line": m.line, "role": m.role, "blocks": m.blocks, "timestamp": m.timestamp}
        for m in SessionSource(session).messages()
    ]
    calls = [call_out(c) for c in session.structured_calls.order_by("created_at")]
    session = with_open_counts(visible_sessions(request.auth).filter(id=session.id)).first()
    return {
        "session": session_out(session),
        "messages": messages,
        "calls": calls,
        "requests": requests_out(session),
    }


@router.get("/calls/{call_id}", response=CallDetailOut)
def call_detail(request, call_id: str):
    call = StructuredCall.objects.select_related("session").filter(id=uuid_or_404(call_id, "call")).first()
    allowed = call is not None and (
        request.auth.is_superuser
        or call.user_id == request.auth.pk
        or (call.session is not None and call.session.user_id == request.auth.pk)
    )
    if not allowed:
        raise HttpError(404, "No such call")
    return call_out(call, detail=True)


def latest_turn(session: ConversationSession, *, queued: bool) -> dict:
    """The session's newest chat reply as a TurnOut (empty while it's queued)."""
    call = None if queued else session.structured_calls.order_by("-created_at").first()
    response = (call.response if call else None) or {}
    return {
        "session_id": str(session.id),
        "call_id": str(call.id) if call else None,
        "type": response.get("type") if isinstance(response, dict) else None,
        "text": response.get("text", "") if isinstance(response, dict) else "",
        "suggestions": response.get("suggestions", []) if isinstance(response, dict) else [],
        "approvals": (call.metadata or {}).get("pending_approvals", []) if call else [],
        "error": call.error if call else "",
        "queued": queued,
    }


def queue_and_report(session: ConversationSession, **turn) -> dict:
    queued = queue_turn(session.id, **turn)
    return latest_turn(session, queued=queued)


@router.post("/sessions/{session_id}/messages", response=TurnOut)
async def send_message(request, session_id: str, data: MessageIn):
    session = await get_session(request, session_id)
    if not data.text.strip() and not data.attachment_ids:
        raise HttpError(400, "Say something")
    get_bot(session.bot_name, request.auth)
    if data.attachment_ids:
        try:
            [uuid.UUID(str(a)) for a in data.attachment_ids]
        except ValueError:
            raise HttpError(400, "Attach files uploaded to this session") from None
        found = await session.attachments.filter(id__in=data.attachment_ids, message_sequence__isnull=True).acount()
        if found != len(set(data.attachment_ids)):
            raise HttpError(400, "Attach files uploaded to this session")
    if session.status == "completed" and (session.metadata or {}).get("bot_role") == "thread":
        await sync_to_async(archival.reopen)(session)  # a message brings an archived thread back
    return await sync_to_async(queue_and_report)(
        session, message=data.text or "(see the attached files)", attachment_ids=data.attachment_ids
    )


@router.post("/sessions/{session_id}/approvals", response=TurnOut)
async def answer_approval(request, session_id: str, data: ApprovalIn):
    session = await get_session(request, session_id)
    bot = get_bot(session.bot_name, request.auth)
    pending = await bot.pending_call(session)
    if pending is None:
        raise HttpError(409, "Nothing is waiting for approval")
    if data.approve:
        require_admin_for_risky_tools(bot, request.auth)
    waiting = sorted(a["id"] for a in (pending.metadata or {}).get("pending_approvals", []))
    if data.approval_ids is not None and sorted(data.approval_ids) != waiting:
        raise HttpError(409, "That approval was already answered")
    return await sync_to_async(queue_and_report)(session, approve=data.approve, approval_ids=data.approval_ids)


@router.post("/sessions/{session_id}/close", response=SessionOut)
async def close_session(request, session_id: str):
    """Archive a thread (a root chat can't be archived)."""
    session = await get_session(request, session_id)
    if (session.metadata or {}).get("bot_role") in ("root", "main", "chat"):
        raise HttpError(409, "Main and named chats can't be archived")
    await sync_to_async(archival.archive)(session, "archived by the user")
    return await sync_to_async(session_out)(session)


# -- knowledge bases ---------------------------------------------------------


class KBArticleOut(Schema):
    path: str
    title: str
    root: bool = False


class KBOut(Schema):
    id: str
    name: str
    kind: str
    location: str
    articles: list[KBArticleOut]


class KBArticleDetailOut(Schema):
    path: str
    title: str
    body: str


def bot_kbs(bot: Bot) -> list[dict]:
    """The knowledge bases attached to ``bot`` through its ergo_kb plugins."""
    from django_ergo.models import Knowledgebase

    found = []
    for index, plugin in enumerate(p for p in bot.plugins if p.name == "ergo_kb"):
        folder = getattr(plugin, "folder", None)
        if folder is not None:
            root = folder.root_article()
            articles = [
                {"path": a.path, "title": a.title, "root": root is not None and a.path == root.path}
                for a in folder.articles()
            ]
            articles.sort(key=lambda a: (not a["root"], a["path"]))
            found.append(
                {
                    "id": f"folder-{index}",
                    "name": folder.root.name,
                    "kind": "folder",
                    "location": str(folder.root),
                    "articles": articles,
                    "_source": folder,
                }
            )
        for kb in Knowledgebase.objects.filter(name__in=plugin.config.get("knowledgebases") or []):
            found.append(
                {
                    "id": str(kb.id),
                    "name": kb.name,
                    "kind": "database",
                    "location": kb.description,
                    "articles": [
                        {"path": str(a.id), "title": a.title}
                        for a in kb.articles.order_by("hierarchy_code", "title")[:2000]
                    ],
                    "_source": kb,
                }
            )
    return found


@router.get("/bots/{bot}/kbs", response=list[KBOut])
def list_kbs(request, bot: str):
    return bot_kbs(get_bot(bot, request.auth))


@router.get("/bots/{bot}/kbs/{kb_id}/article", response=KBArticleDetailOut)
def read_kb_article(request, bot: str, kb_id: str, path: str):
    kb = next((k for k in bot_kbs(get_bot(bot, request.auth)) if k["id"] == kb_id), None)
    if kb is None:
        raise HttpError(404, "No such knowledge base")
    source = kb["_source"]
    if kb["kind"] == "folder":
        try:
            article = source.read(path)
        except (ValueError, FileNotFoundError, OSError) as e:
            raise HttpError(404, str(e)) from e
        return {"path": article.path, "title": article.title, "body": article.body}
    article = source.articles.filter(id=int(path)).first() if str(path).isdigit() else None
    if article is None:
        raise HttpError(404, "No such article")
    return {"path": str(article.id), "title": article.title, "body": article.content or ""}


# -- attachments ---------------------------------------------------------------

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AttachmentOut(Schema):
    id: str
    filename: str
    media_type: str
    kind: str
    size: int | None
    source: str
    message_sequence: int | None
    pinned: bool = False
    created_at: datetime
    updated_at: datetime


def attachment_out(row: ConversationAttachment) -> dict:
    return {
        "id": str(row.id),
        "filename": row.filename,
        "media_type": row.media_type,
        "kind": row.kind,
        "size": row.size,
        "source": row.source,
        "message_sequence": row.message_sequence,
        "pinned": bool((row.metadata or {}).get("pinned")),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def visible_attachment(user, attachment_id: str) -> ConversationAttachment:
    row = (
        ConversationAttachment.objects.select_related("session")
        .filter(id=uuid_or_404(attachment_id, "file"), session__in=visible_sessions(user))
        .first()
    )
    if row is None:
        raise HttpError(404, "No such file")
    return row


@router.get("/sessions/{session_id}/attachments", response=list[AttachmentOut])
def list_attachments(request, session_id: str):
    session = visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).first()
    if session is None:
        raise HttpError(404, "No such session")
    return [attachment_out(r) for r in session.attachments.order_by("-updated_at")]


@router.post("/sessions/{session_id}/attachments", response=AttachmentOut)
def upload_attachment(request, session_id: str, file: UploadedFile = File(...)):  # noqa: B008
    session = visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).first()
    if session is None:
        raise HttpError(404, "No such session")
    if file.size and file.size > MAX_UPLOAD_BYTES:
        raise HttpError(413, f"Files are limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    row = save_session_file(
        session,
        file.name or "upload",
        file.read(),
        media_type=file.content_type if file.content_type not in ("", "application/octet-stream") else "",
        source="upload",
        metadata={"uploaded_by": request.auth.get_username()},
    )
    return attachment_out(row)


# Bot-written pages run scripts, so they get their own opaque origin: no cookies, no app API.
SANDBOXED = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads"


def page_response(html: str, *, sandboxed: bool) -> HttpResponse:
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["X-Frame-Options"] = "SAMEORIGIN"  # shown in the chat's page viewer
    if sandboxed:
        response["Content-Security-Policy"] = SANDBOXED
    return response


def render_or_error(bot: Bot, source: str, user, title: str) -> str:
    from django_ergo.bots.pages import PageError, error_page, render_page

    try:
        return render_page(bot, source, user=user, title=title)
    except PageError as exc:
        return error_page(str(exc), title)


@router.get("/attachments/{attachment_id}/download")
def download_attachment(request, attachment_id: str, inline: bool = False):
    row = visible_attachment(request.auth, attachment_id)
    if not row.file:
        raise HttpError(404, "This file has no stored copy")
    if inline and row.filename.endswith(".jhtml"):
        # A live page: rendered now, over the bot's tables.
        from django_ergo.conversation.attachments import read_text

        bot = get_bot(row.session.bot_name, request.auth)
        title = (row.metadata or {}).get("title") or row.filename
        return page_response(render_or_error(bot, read_text(row), request.auth, title), sandboxed=True)
    if inline and row.media_type == "text/html":
        with row.file.open("rb") as handle:
            return page_response(handle.read().decode("utf-8", "replace"), sandboxed=True)
    # Inline only for images, so a stored HTML or SVG file can't run in the app's origin.
    show = inline and row.media_type in ("image/png", "image/jpeg", "image/gif", "image/webp")
    return FileResponse(row.file.open("rb"), as_attachment=not show, filename=row.filename, content_type=row.media_type)


class PinIn(Schema):
    pinned: bool = True


@router.post("/attachments/{attachment_id}/pin", response=AttachmentOut)
def pin_attachment(request, attachment_id: str, payload: PinIn):
    row = visible_attachment(request.auth, attachment_id)
    row.metadata = {**(row.metadata or {}), "pinned": payload.pinned}
    row.save(update_fields=["metadata", "updated_at"])
    return attachment_out(row)


@router.delete("/attachments/{attachment_id}")
def delete_attachment(request, attachment_id: str):
    row = visible_attachment(request.auth, attachment_id)
    if row.message_sequence is not None:
        raise HttpError(409, "Files sent with a message stay with the message")
    if row.file:
        row.file.delete(save=False)
    row.delete()
    return {"ok": True}


# -- pins and bot files --------------------------------------------------------------


@router.get("/sessions/{session_id}/pins")
def session_pins(request, session_id: str):
    from django_ergo.bots.pages import session_pins as pins_of

    session = visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).first()
    if session is None:
        raise HttpError(404, "No such session")
    bot = get_bot(session.bot_name, request.auth)
    pins = pins_of(bot, session)
    for pin in pins:
        if pin["kind"] == "bot_file":
            pin["url"] = f"/api/bots/{bot.name}/files/{pin['path']}"
        else:
            pin["url"] = f"/api/attachments/{pin['id']}/download?inline=true"
    return pins


@router.get("/bots/{bot_name}/files/{path:path}")
def bot_file(request, bot_name: str, path: str):
    """A page or asset from the bot folder (reviewed in the bot repo, so it runs in the app's origin)."""
    from django_ergo.bots.pages import SERVED_SUFFIXES
    from django_ergo.bots.pages import bot_file as find

    bot = get_bot(bot_name, request.auth)
    found = find(bot, path)
    if found is None:
        raise HttpError(404, "No such file")
    if found.suffix == ".jhtml":
        return page_response(render_or_error(bot, found.read_text(), request.auth, found.stem), sandboxed=False)
    response = FileResponse(found.open("rb"), content_type=SERVED_SUFFIXES[found.suffix.lower()])
    response["Cache-Control"] = "no-cache"
    response["X-Frame-Options"] = "SAMEORIGIN"
    return response


# -- changes: proposals to the bot repo ------------------------------------------


class PullRequestOut(Schema):
    number: int
    title: str
    url: str
    branch: str
    author: str
    created_at: str
    body: str
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


class ChangesOut(Schema):
    managed_by: str
    repo: str
    mode: str
    draft_diff: str
    pull_requests: list[PullRequestOut]
    error: str = ""


def change_manager(bot: str, user=None):
    """The bot_management plugin that maintains ``bot``'s repo (its own, or a parent's)."""
    found = get_bot(bot, user)
    seen = set()
    current = found
    while current is not None and current.name not in seen:
        seen.add(current.name)
        plugin = current.plugin("bot_management")
        if plugin is not None:
            return current, plugin
        current = registry().get(current.parent_name) if current.parent_name in registry() else None
    raise HttpError(404, f"No bot manages {bot}'s repository (bot_management plugin)")


def require_admin(request):
    if not request.auth.is_superuser:
        raise HttpError(403, "Only an admin can merge or close changes")


@router.get("/bots/{bot}/changes", response=ChangesOut)
def bot_changes(request, bot: str):
    manager, plugin = change_manager(bot, request.auth)
    out = {
        "managed_by": manager.name,
        "repo": str(plugin.repo),
        "mode": plugin.mode,
        "draft_diff": "",
        "pull_requests": [],
    }
    try:
        out["draft_diff"] = plugin.draft_diff()
        if plugin.mode == "propose_pr":
            out["pull_requests"] = [
                {
                    "number": pr["number"],
                    "title": pr.get("title", ""),
                    "url": pr.get("url", ""),
                    "branch": pr.get("headRefName", ""),
                    "author": (pr.get("author") or {}).get("login", ""),
                    "created_at": pr.get("createdAt", ""),
                    "body": pr.get("body", ""),
                    "additions": pr.get("additions") or 0,
                    "deletions": pr.get("deletions") or 0,
                    "changed_files": pr.get("changedFiles") or 0,
                }
                for pr in plugin.pull_requests()
            ]
    except (ValueError, OSError) as e:
        out["error"] = str(e)
    return out


@router.get("/bots/{bot}/changes/{number}/diff")
def bot_change_diff(request, bot: str, number: int):
    _, plugin = change_manager(bot, request.auth)
    try:
        return {"diff": plugin.pull_request_diff(number)}
    except ValueError as e:
        raise HttpError(400, str(e)) from e


@router.post("/bots/{bot}/changes/{number}/merge")
def merge_bot_change(request, bot: str, number: int):
    require_admin(request)
    _, plugin = change_manager(bot, request.auth)
    try:
        return {"result": plugin.merge_pull_request(number)}
    except ValueError as e:
        raise HttpError(400, str(e)) from e


@router.post("/bots/{bot}/changes/{number}/close")
def close_bot_change(request, bot: str, number: int):
    require_admin(request)
    _, plugin = change_manager(bot, request.auth)
    try:
        return {"result": plugin.close_pull_request(number)}
    except ValueError as e:
        raise HttpError(400, str(e)) from e


@router.post("/bots/{bot}/changes/draft/discard")
def discard_bot_draft(request, bot: str):
    require_admin(request)
    _, plugin = change_manager(bot, request.auth)
    return {"result": plugin.discard()}
