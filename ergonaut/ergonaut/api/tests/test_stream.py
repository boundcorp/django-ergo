import json

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ergonaut.api import stream


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(stream, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(stream, "STREAM_SECONDS", 2)


def chat(username: str, count: int) -> str:
    from django_ergo.conversation.models import ConversationSession, MessageBlock, SessionMessage

    user = get_user_model().objects.create_user(username, f"{username}@example.com", "pw")
    session = ConversationSession.objects.create(
        user=user, bot_name="kitchen", engine_type="openai", metadata={"bot_role": "main"}
    )
    for n in range(count):
        message = SessionMessage.objects.create(session=session, role="user" if n % 2 == 0 else "assistant", sequence=n)
        MessageBlock.objects.create(message=message, block_type="text", sequence=0, text=f"m{n}")
    return str(session.id)


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
    session_id = await sync_to_async(chat)("cook", 4)
    await async_client.alogin(username="cook", password="pw")
    url = f"/api/sessions/{session_id}/events?after=-1"

    ident, event = await first_event(await async_client.get(url))
    lines = [m["line"] for m in event["messages"]]
    assert lines[0] == 0 and ident == str(lines[-1])

    # The browser sends the id back when it reconnects to the same URL.
    _, event = await first_event(await async_client.get(url, headers={"Last-Event-ID": "2"}))
    assert min(m["line"] for m in event["messages"]) >= 2
