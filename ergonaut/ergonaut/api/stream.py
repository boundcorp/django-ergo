"""Live session updates for the web app, as server-sent events.

    GET /api/sessions/<id>/events?after=<line>

Each event is JSON: ``{"messages": [...], "calls": [...]}`` with the
messages from line ``after`` on and every structured call that changed.
The last message the client has is sent again, since its blocks may still
have been arriving, so clients replace messages by line.
The stream reads the database, so a turn shows its tool calls as they
happen whichever process runs it (the web app, a Celery worker, Telegram, a
scheduled job). With ``REDIS_URL`` set it wakes as soon as a session-changed
notice arrives (see ``ergonaut.apps.bots.tasks``) and otherwise checks every
few seconds; without Redis it checks twice a second. The stream ends after a
few minutes; EventSource reconnects.
"""

import asyncio
import json
import os

from asgiref.sync import sync_to_async
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, StreamingHttpResponse
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.models import ClaudeContentBlock, ConversationSession

from ergonaut.api.bots import call_out, requests_out, visible_sessions

POLL_SECONDS = 0.5
PUBSUB_POLL_SECONDS = 3.0
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
    requests = requests_out(session)
    key = [(r["id"], r["status"]) for r in requests]
    if seen.get("requests") == key:
        requests = None
    else:
        seen["requests"] = key
    calls = []
    for call in session.structured_calls.order_by("created_at"):
        key = (call.status, call.updated_at.isoformat(), call.output_tokens)
        if seen.setdefault("calls", {}).get(str(call.id)) != key:
            seen["calls"][str(call.id)] = key
            calls.append(call_out(call))
    return messages, calls, requests


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

    async def changes():
        """Wait for this session to change: a Redis notice, or the poll interval."""
        url = os.environ.get("REDIS_URL")
        if not url:
            while True:
                await asyncio.sleep(POLL_SECONDS)
                yield POLL_SECONDS
        import redis.asyncio as aioredis

        from ergonaut.apps.bots.tasks import CHANNEL

        client = aioredis.Redis.from_url(url)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)
        target = str(session_id).encode()
        loop = asyncio.get_running_loop()
        try:
            while True:
                started = loop.time()
                deadline = started + PUBSUB_POLL_SECONDS
                while loop.time() < deadline:
                    notice = await pubsub.get_message(ignore_subscribe_messages=True, timeout=deadline - loop.time())
                    if notice and notice.get("data") == target:
                        break
                yield loop.time() - started
        finally:
            await pubsub.aclose()
            await client.aclose()

    async def stream():
        seen: dict = {}
        last = after
        quiet = 0.0
        loop = asyncio.get_running_loop()
        end = loop.time() + STREAM_SECONDS
        waits = changes()
        while loop.time() < end:
            messages, calls, requests = await sync_to_async(_snapshot)(session_id, last, seen)
            # The first event carries every call and request too: something may
            # have changed between the client's load (or the last stream) and now,
            # and the client merges by id.
            if messages or calls or requests is not None:
                if messages:
                    last = max(m["line"] for m in messages)
                event = {"messages": messages, "calls": calls}
                if requests is not None:
                    event["requests"] = requests
                yield _event(event)
                quiet = 0.0
            elif quiet >= KEEPALIVE_SECONDS:
                yield ": keepalive\n\n"
                quiet = 0.0
            quiet += await anext(waits)
        await waits.aclose()

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
