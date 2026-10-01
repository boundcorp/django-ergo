import asyncio
import json

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationSession
from ergonaut.api import stream


async def read_events(response, count):
    events = []
    async for chunk in response.streaming_content:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.startswith("data: "):
            events.append(json.loads(text[6:]))
            if len(events) == count:
                break
    return events


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(stream, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(stream, "STREAM_SECONDS", 3)


async def login(async_client, name):
    await sync_to_async(get_user_model().objects.create_user)(name, f"{name}@example.com", "pw")
    response = await async_client.post(
        "/api/auth/login", {"username": name, "password": "pw"}, content_type="application/json"
    )
    assert response.status_code == 200
    return await get_user_model().objects.aget(username=name)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_sends_new_messages_and_call_changes(async_client, fast):
    user = await login(async_client, "cook")
    session = await ConversationSession.objects.acreate(user=user, bot_name="kitchen", engine_type="claude")

    response = await async_client.get(f"/api/sessions/{session.id}/events")
    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"

    async def write():
        await asyncio.sleep(0.2)
        message = await session.claude_messages.acreate(role="user", sequence=0)
        await message.content_blocks.acreate(block_type="text", text="How many eggs?", sequence=0)
        call = await session.structured_calls.acreate(kind="chat_reply", status="in_progress")
        await asyncio.sleep(0.2)
        call.status = "completed"
        await call.asave()

    writer = asyncio.create_task(write())
    events = await asyncio.wait_for(read_events(response, 3), timeout=5)
    await writer

    latest = {m["line"]: m for e in events for m in e["messages"]}
    texts = [b["text"] for m in latest.values() for b in m["blocks"] if b["type"] == "text"]
    assert texts == ["How many eggs?"]
    statuses = [c["status"] for e in events for c in e["calls"]]
    assert statuses[-1] == "completed"
    assert "in_progress" in statuses


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_needs_login_and_visibility(async_client, fast):
    other = await sync_to_async(get_user_model().objects.create_user)("other", "o@example.com", "pw")
    theirs = await ConversationSession.objects.acreate(user=other, bot_name="kitchen", engine_type="claude")
    assert (await async_client.get(f"/api/sessions/{theirs.id}/events")).status_code == 401
    await login(async_client, "cook")
    assert (await async_client.get(f"/api/sessions/{theirs.id}/events")).status_code == 404
