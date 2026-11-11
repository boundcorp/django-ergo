import asyncio
from types import SimpleNamespace

import pytest
from django_ergo.conversation.models import ConversationSession

from ergonaut.api.realtime_voice import record_event
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.realtime_voice_outbox import completion_event


@pytest.fixture
def voice(test_user):
    user, _ = test_user
    chat = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    return RealtimeVoiceSession.objects.create(user=user, chat=chat)


def test_completion_persists_before_send_and_retries_after_reconnect(voice):
    task = {"event_id": "request-1", "status": "answered", "result": "Verified result"}
    first = completion_event(voice.id, task)
    # Simulate losing the connection after the durable write and before commentary.
    retried = completion_event(voice.id, task)
    assert first.pk == retried.pk
    assert retried.delivery == "unknown"
    assert voice.events.count() == 1
    assert retried.text == "Verified result"

    record_event(voice.id, first.event_id, "result", first.text, "interrupted", trusted_delivery=True)
    assert completion_event(voice.id, task).delivery == "interrupted"
    record_event(voice.id, first.event_id, "result", first.text, "delivered", trusted_delivery=True)
    assert completion_event(voice.id, task).delivery == "delivered"
    assert voice.events.count() == 1


def test_only_terminal_work_is_added_to_outbox(voice):
    task = {"event_id": "request-2", "status": "waiting", "result": ""}
    assert completion_event(voice.id, task) is None
    assert not voice.events.exists()
    task["status"] = "failed"
    event = completion_event(voice.id, task)
    assert event.text == "The background request failed."
    assert event.delivery == "unknown"


def test_bridge_retries_unconfirmed_completion_only_on_new_connection(monkeypatch):
    pytest.importorskip("livekit.agents")
    pytest.importorskip("livekit.plugins.openai")
    pytest.importorskip("livekit.plugins.silero")
    from ergonaut import realtime_voice_bridge as bridge

    event = SimpleNamespace(text="Verified result", delivery="unknown")
    monkeypatch.setattr(bridge, "completion_event", lambda voice_id, task: event)
    task = {"event_id": "request-1", "status": "answered", "result": event.text}
    snapshot = {"delegations": [task]}

    class Duplex:
        def __init__(self):
            self.messages = []

        def append_commentary(self, message):
            self.messages.append(message)

    first = SimpleNamespace(voice_id="voice", announced=set(), duplex_session=Duplex())
    second = SimpleNamespace(voice_id="voice", announced=set(), duplex_session=Duplex())
    asyncio.run(bridge.Assistant.deliver_results(first, snapshot))
    asyncio.run(bridge.Assistant.deliver_results(first, snapshot))
    assert len(first.duplex_session.messages) == 1
    asyncio.run(bridge.Assistant.deliver_results(second, snapshot))
    assert len(second.duplex_session.messages) == 1
    event.delivery = "delivered"
    third = SimpleNamespace(voice_id="voice", announced=set(), duplex_session=Duplex())
    asyncio.run(bridge.Assistant.deliver_results(third, snapshot))
    assert third.duplex_session.messages == []


def test_bridge_does_not_mark_failed_send_as_announced(monkeypatch):
    pytest.importorskip("livekit.agents")
    pytest.importorskip("livekit.plugins.openai")
    pytest.importorskip("livekit.plugins.silero")
    from ergonaut import realtime_voice_bridge as bridge

    monkeypatch.setattr(
        bridge, "completion_event", lambda voice_id, task: SimpleNamespace(text="Done", delivery="unknown")
    )

    def fail(message):
        raise RuntimeError("connection lost")

    assistant = SimpleNamespace(
        voice_id="voice", announced=set(), duplex_session=SimpleNamespace(append_commentary=fail)
    )
    snapshot = {"delegations": [{"event_id": "request-1"}]}
    with pytest.raises(RuntimeError):
        asyncio.run(bridge.Assistant.deliver_results(assistant, snapshot))
    assert assistant.announced == set()
