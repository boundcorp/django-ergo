import pytest
from django_ergo.conversation.models import ConversationSession

from ergonaut.api.realtime_voice import record_event
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.realtime_voice_outbox import claim_announcement, completion_event


@pytest.fixture
def voice(test_user):
    user, _ = test_user
    chat = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    return RealtimeVoiceSession.objects.create(user=user, chat=chat)


def test_completion_durable_and_announcement_at_most_once(voice):
    task = {"event_id": "request-1", "status": "answered", "result": "Verified result"}
    event = completion_event(voice.id, task)
    assert event.delivery == "unknown"
    assert claim_announcement(event)
    # The bridge may crash before sending. Reconnect still exposes the result,
    # but cannot claim it a second time or pretend playback was confirmed.
    restored = completion_event(voice.id, task)
    assert restored.pk == event.pk
    assert restored.text == "Verified result"
    assert restored.delivery == "announcement_claimed"
    assert not claim_announcement(restored)
    assert voice.events.count() == 1
    record_event(voice.id, event.event_id, "result", event.text, "delivered", trusted_delivery=True)
    assert not claim_announcement(completion_event(voice.id, task))


def test_only_terminal_work_is_added_to_outbox(voice):
    task = {"event_id": "request-2", "status": "waiting", "result": ""}
    assert completion_event(voice.id, task) is None
    task["status"] = "failed"
    event = completion_event(voice.id, task)
    assert event.text == "The background request failed."
    assert event.delivery == "unknown"
