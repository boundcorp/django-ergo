import asyncio
import json

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import ConversationSession

from ergonaut.api import stream


async def read_events(response, done):
    """Events until ``done(events)`` is true. A change can span several polls
    (the message, then its blocks), so tests wait for a state, not a count."""
    events = []
    async for chunk in response.streaming_content:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        _, found, data = text.partition("data: ")
        if found:
            events.append(json.loads(data))
            if done(events):
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
        message = await session.messages.acreate(role="user", sequence=0)
        await message.content_blocks.acreate(block_type="text", text="How many eggs?", sequence=0)
        call = await session.structured_calls.acreate(kind="chat_reply", status="in_progress")
        await asyncio.sleep(0.2)
        call.status = "completed"
        await call.asave()

    writer = asyncio.create_task(write())

    def completed(events):
        return any(c["status"] == "completed" for e in events for c in e["calls"])

    events = await asyncio.wait_for(read_events(response, completed), timeout=5)
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


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_exposes_identity_and_verbatim_forwarded_body(async_client, fast):
    user = await login(async_client, "owner")
    session = await ConversationSession.objects.acreate(user=user, bot_name="kitchen", engine_type="claude")
    author = {"kind": "bot", "ref": "research", "display_name": "Research bot"}
    provenance = {
        "kind": "report",
        "origin": {"session_id": "origin", "label": "Research chat", "timestamp": "2026-10-05T10:30:00Z"},
        "note": "Attribution must not be prepended",
    }
    message = await session.messages.acreate(role="user", sequence=0, author=author, provenance=provenance)
    await message.content_blocks.acreate(block_type="text", text="Verbatim report", sequence=0)
    response = await async_client.get(f"/api/sessions/{session.id}/events")
    events = await asyncio.wait_for(read_events(response, lambda events: any(e["messages"] for e in events)), timeout=5)
    forwarded = next(m for e in events for m in e["messages"])
    assert forwarded["author"] == author
    assert forwarded["provenance"] == provenance
    assert forwarded["blocks"] == [{"type": "text", "text": "Verbatim report"}]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_and_detail_send_the_todo_list(async_client, fast):
    from django_ergo.bots.todos import save_todos

    user = await login(async_client, "planner")
    session = await ConversationSession.objects.acreate(user=user, bot_name="kitchen", engine_type="claude")
    todos = [{"content": "Shop", "status": "completed"}, {"content": "Cook", "status": "in_progress"}]
    response = await async_client.get(f"/api/sessions/{session.id}/events")

    async def write():
        await asyncio.sleep(0.2)
        await sync_to_async(save_todos)(session, todos)

    writer = asyncio.create_task(write())
    events = await asyncio.wait_for(
        read_events(response, lambda events: any(e.get("todos") for e in events)), timeout=5
    )
    await writer
    assert next(e["todos"] for e in events if e.get("todos")) == todos
    assert (await async_client.get(f"/api/sessions/{session.id}")).json()["todos"] == todos


async def first_event(response) -> tuple[str, dict]:
    async for chunk in response.streaming_content:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.startswith("id: "):
            ident, _, data = text.partition("\ndata: ")
            return ident[4:], json.loads(data)
    raise AssertionError("no event")


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_reconnect_resumes_after_the_last_event_id(async_client, fast):
    user = await login(async_client, "cook")
    session = await ConversationSession.objects.acreate(user=user, bot_name="kitchen", engine_type="claude")
    for n in range(4):
        message = await session.messages.acreate(role="user" if n % 2 == 0 else "assistant", sequence=n)
        await message.content_blocks.acreate(block_type="text", text=f"m{n}", sequence=0)
    url = f"/api/sessions/{session.id}/events?after=-1"

    ident, event = await first_event(await async_client.get(url))
    lines = [m["line"] for m in event["messages"]]
    assert lines[0] == 0 and ident == str(lines[-1])

    # The browser sends the id back when it reconnects to the same URL.
    _, event = await first_event(await async_client.get(url, headers={"Last-Event-ID": "2"}))
    assert min(m["line"] for m in event["messages"]) >= 2
