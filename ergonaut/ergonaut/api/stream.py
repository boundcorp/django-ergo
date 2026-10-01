"""Live session updates for the web app, as server-sent events.

    GET /api/sessions/<id>/events?after=<line>

Each event is JSON: ``{"messages": [...], "calls": [...]}`` with the
messages from line ``after`` on and every structured call that changed.
The last message the client has is sent again, since its blocks may still
have been arriving, so clients replace messages by line.
The server checks the database twice a second, so a turn shows its tool
calls as they happen, whichever process runs it (the web app, Telegram, a
scheduled job). The stream ends after a few minutes; EventSource reconnects.
"""

import asyncio
import json

from asgiref.sync import sync_to_async
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.http import StreamingHttpResponse

from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ConversationSession
from ergonaut.api.bots import call_out
from ergonaut.api.bots import visible_sessions

POLL_SECONDS = 0.5
STREAM_SECONDS = 240
KEEPALIVE_SECONDS = 15


def _snapshot(session_id, after: int, seen: dict):
    session = ConversationSession.objects.get(id=session_id)
    count = (
        session.claude_messages.count(),
        ClaudeContentBlock.objects.filter(message__session=session).count(),
        session.openai_messages.count(),
    )
    messages = []
    if count != seen.get("count"):
        seen["count"] = count
        messages = [
            {"line": m.line, "role": m.role, "blocks": m.blocks, "timestamp": m.timestamp}
            for m in SessionSource(session).messages()
            if m.line >= after
        ]
    calls = []
    for call in session.structured_calls.order_by("created_at"):
        key = (call.status, call.updated_at.isoformat(), call.output_tokens)
        if seen.setdefault("calls", {}).get(str(call.id)) != key:
            seen["calls"][str(call.id)] = key
            calls.append(call_out(call))
    return messages, calls


def _event(payload) -> str:
    return f"data: {json.dumps(payload, cls=DjangoJSONEncoder)}\n\n"


async def session_events(request, session_id):
    user = await request.auser()
    if not user.is_authenticated:
        return HttpResponse(status=401)
    allowed = await visible_sessions(user).filter(id=session_id).aexists()
    if not allowed:
        return HttpResponse(status=404)
    try:
        after = int(request.GET.get("after", -1))
    except ValueError:
        after = -1

    async def stream():
        seen: dict = {}
        last = after
        quiet = 0.0
        loop = asyncio.get_running_loop()
        end = loop.time() + STREAM_SECONDS
        first = True
        while loop.time() < end:
            messages, calls = await sync_to_async(_snapshot)(session_id, last, seen)
            if first:
                # The client already has everything up to `after`; only
                # remember the calls it has seen.
                calls, first = [], False
            if messages or calls:
                if messages:
                    last = max(m["line"] for m in messages)
                yield _event({"messages": messages, "calls": calls})
                quiet = 0.0
            else:
                quiet += POLL_SECONDS
                if quiet >= KEEPALIVE_SECONDS:
                    yield ": keepalive\n\n"
                    quiet = 0.0
            await asyncio.sleep(POLL_SECONDS)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
