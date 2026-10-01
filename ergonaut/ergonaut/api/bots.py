"""The web app's API: bots, sessions, transcripts and turns.

Everything is scoped to the signed-in user (a superuser sees everyone's
sessions). A turn runs inside the request and returns the bot's ChatReply.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from asgiref.sync import sync_to_async
from django.db.models import Q
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja import Schema
from ninja.errors import HttpError
from ninja.security import django_auth

from django_ergo.bots import webhooks
from django_ergo.bots.runtime import Bot
from django_ergo.bots.runtime import TurnResult
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall

router = Router(tags=["bots"], auth=django_auth)


# -- schemas -----------------------------------------------------------------


class BotOut(Schema):
    name: str
    description: str
    orchestration: bool
    root_session_id: str | None


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
    created_at: datetime


class CallDetailOut(CallOut):
    system_prompt: str
    transcript: list
    metadata: dict


class SessionDetailOut(Schema):
    session: SessionOut
    messages: list[MessageOut]
    calls: list[CallOut]


class NewThreadIn(Schema):
    title: str = ""


class MessageIn(Schema):
    text: str


class ApprovalIn(Schema):
    approve: bool


class TurnOut(Schema):
    session_id: str
    call_id: str | None
    type: str | None
    text: str
    suggestions: list[str]
    approvals: list[dict]
    error: str


# -- helpers -----------------------------------------------------------------


def registry():
    found = webhooks.get_registry()
    if found is None:
        raise HttpError(503, "No bots are loaded")
    return found


def get_bot(name: str) -> Bot:
    bots = registry()
    if name not in bots:
        raise HttpError(404, f"No bot {name!r}")
    return bots.get(name)


def session_out(session: ConversationSession) -> dict:
    meta = session.metadata or {}
    return {
        "id": str(session.id),
        "bot": session.bot_name,
        "title": meta.get("title") or ("Root chat" if meta.get("bot_role") == "root" else "Thread"),
        "role": meta.get("bot_role") or "",
        "parent_id": str(session.parent_id) if session.parent_id else None,
        "status": session.status,
        "username": session.user.get_username() if session.user_id else "",
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


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
        "created_at": call.created_at,
    }
    if detail:
        out.update(system_prompt=call.system_prompt, transcript=call.transcript, metadata=call.metadata)
    return out


def turn_out(result: TurnResult) -> dict:
    reply = result.reply
    return {
        "session_id": str(result.session.id),
        "call_id": str(result.call.id) if result.call else None,
        "type": reply.type if reply else None,
        "text": result.text,
        "suggestions": result.suggestions,
        "approvals": [
            {"id": a.tool_use_id, "name": a.tool_name, "input": a.arguments} for a in result.approvals
        ],
        "error": result.error,
    }


def visible_sessions(user):
    qs = ConversationSession.objects.exclude(bot_name="").select_related("user")
    return qs if user.is_superuser else qs.filter(user=user)


async def get_session(request, session_id) -> ConversationSession:
    return await aget_object_or_404(visible_sessions(request.auth), id=session_id)


# -- endpoints ---------------------------------------------------------------


@router.get("/bots", response=list[BotOut])
async def list_bots(request):
    out = []
    for bot in registry():
        root = await (
            bot.sessions(request.auth)
            .filter(parent__isnull=True, metadata__bot_role="root")
            .exclude(status="completed")
            .order_by("created_at")
            .afirst()
        )
        out.append(
            {
                "name": bot.name,
                "description": bot.definition.description,
                "orchestration": bot.definition.orchestration,
                "root_session_id": str(root.id) if root else None,
            }
        )
    return out


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
    return [session_out(s) for s in qs.order_by("-updated_at")[:200]]


@router.post("/bots/{bot}/root", response=SessionOut)
async def open_root(request, bot: str):
    session = await get_bot(bot).root_session(request.auth)
    session.user = request.auth
    return session_out(session)


@router.post("/bots/{bot}/threads", response=SessionOut)
async def new_thread(request, bot: str, data: NewThreadIn):
    found = get_bot(bot)
    root = await found.root_session(request.auth)
    session = await found.create_session(request.auth, parent=root, title=data.title)
    session.user = request.auth
    return session_out(session)


@router.get("/sessions/{session_id}", response=SessionDetailOut)
def session_detail(request, session_id: str):
    session = visible_sessions(request.auth).filter(id=session_id).first()
    if session is None:
        raise HttpError(404, "No such session")
    messages = [
        {"line": m.line, "role": m.role, "blocks": m.blocks, "timestamp": m.timestamp}
        for m in SessionSource(session).messages()
    ]
    calls = [call_out(c) for c in session.structured_calls.order_by("created_at")]
    return {"session": session_out(session), "messages": messages, "calls": calls}


@router.get("/calls/{call_id}", response=CallDetailOut)
def call_detail(request, call_id: str):
    call = StructuredCall.objects.select_related("session").filter(id=call_id).first()
    allowed = call is not None and (
        request.auth.is_superuser
        or call.user_id == request.auth.pk
        or (call.session is not None and call.session.user_id == request.auth.pk)
    )
    if not allowed:
        raise HttpError(404, "No such call")
    return call_out(call, detail=True)


@router.post("/sessions/{session_id}/messages", response=TurnOut)
async def send_message(request, session_id: str, data: MessageIn):
    session = await get_session(request, session_id)
    if not data.text.strip():
        raise HttpError(400, "Say something")
    result = await get_bot(session.bot_name).ask(session, data.text)
    return turn_out(result)


@router.post("/sessions/{session_id}/approvals", response=TurnOut)
async def answer_approval(request, session_id: str, data: ApprovalIn):
    session = await get_session(request, session_id)
    bot = get_bot(session.bot_name)
    if await bot.pending_call(session) is None:
        raise HttpError(409, "Nothing is waiting for approval")
    return turn_out(await bot.resume(session, data.approve))


@router.post("/sessions/{session_id}/close", response=SessionOut)
async def close_session(request, session_id: str):
    session = await get_session(request, session_id)
    await get_bot(session.bot_name).close_session(session)
    return await sync_to_async(session_out)(session)
