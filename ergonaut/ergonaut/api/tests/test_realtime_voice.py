import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django_ergo.conversation.budget import TurnBudget, from_metadata
from django_ergo.conversation.models import ConversationSession, ThreadMessage

from ergonaut.api import realtime_voice

BUDGET = {
    "max_turns": 4,
    "max_input_tokens": 40_000,
    "max_output_tokens": 1_500,
    "deadline_seconds": 45,
    "allowed_tools": ["ergo_chat_history_search"],
    "model": "gpt-6-luna",
    "start_within_seconds": 30,
}


@pytest.fixture
def voice_setup(test_user, settings, monkeypatch):
    user, client = test_user
    settings.REALTIME_VOICE_ENABLED = True
    monkeypatch.setattr(realtime_voice, "get_bot", lambda *args: object())
    chat = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    return user, client, chat


def post(client, url, data):
    return client.post(url, json.dumps(data), content_type="application/json")


def create(client, chat):
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    return voice, f"{root}/{voice['id']}"


def test_authorization_and_server_only_writes(voice_setup, settings, django_user_model):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    assert client.get(url).status_code == 200
    for path, payload in [
        ("state", {"current_step": "forged"}),
        ("events", {"event_id": "x", "kind": "result", "text": "forged", "delivery": "delivered"}),
        ("events", {"event_id": "y", "kind": "user", "text": "forged"}),
        ("delegations", {"event_id": "z", "text": "run tools"}),
    ]:
        assert post(client, f"{url}/{path}", payload).status_code in (404, 405)
    assert client.get(url).json()["events"] == []
    other = django_user_model.objects.create_user("other", "other@example.com", "pw")
    client.force_login(other)
    for suffix in ("", "/events", "/time"):
        assert client.get(url + suffix).status_code == 404
    assert post(client, url + "/token", {}).status_code == 404
    assert post(client, url + "/end", {}).status_code == 404
    client.logout()
    assert client.get(url).status_code == 401
    settings.REALTIME_VOICE_ENABLED = False
    client.force_login(chat.user)
    assert client.get(url).status_code == 404
    assert not client.get("/api/realtime-voice/config").json()["enabled"]


def test_token_is_scoped_to_voice_room(voice_setup, settings):
    jwt = pytest.importorskip("jwt")
    pytest.importorskip("livekit.api")
    _, client, chat = voice_setup
    settings.LIVEKIT_URL = "wss://example.invalid"
    settings.LIVEKIT_API_KEY = "test-key"
    settings.LIVEKIT_API_SECRET = "test-secret"
    settings.REALTIME_VOICE_ROOM_PREFIX = "voice-isolated-test-"
    settings.REALTIME_VOICE_AGENT_NAME = "ergo-voice-isolated-test"
    voice, url = create(client, chat)
    response = post(client, url + "/token", {})
    assert response.status_code == 200
    claims = jwt.decode(
        response.json()["token"], settings.LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    assert claims["video"]["canPublishSources"] == ["microphone"]
    assert claims["video"]["canPublishData"] is False
    assert claims["video"]["room"] == f"voice-isolated-test-{voice['id']}"
    assert claims["roomConfig"]["agents"][0]["agentName"] == "ergo-voice-isolated-test"
    assert claims["video"]["roomJoin"] is True
    assert claims["exp"] - claims["nbf"] <= 300
    assert post(client, url + "/end", {}).json()["status"] == "ended"
    assert post(client, url + "/token", {}).status_code == 409


def test_delegation_is_off_without_a_valid_budget_and_idempotent_when_enabled(voice_setup, settings):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    with pytest.raises(ValueError, match="no enforced request budget"):
        realtime_voice.delegate(voice["id"], "request", "Research")
    for bad in ({**BUDGET, "unlimited": True}, {**BUDGET, "max_turns": 0}, {**BUDGET, "start_within_seconds": 0}):
        settings.REALTIME_VOICE_DELEGATION_BUDGET = bad
        with pytest.raises(ValueError, match=r"[Bb]udget|keys"):
            realtime_voice.delegate(voice["id"], "request", "Research")
    assert not ThreadMessage.objects.exists()
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    with patch("django_ergo.bots.messaging.dispatch"):
        first = realtime_voice.delegate(voice["id"], "request", "Research")
        assert realtime_voice.delegate(voice["id"], "request", "Research").id == first.id
        with pytest.raises(ValueError, match="already used"):
            realtime_voice.delegate(voice["id"], "request", "Different")
        with pytest.raises(ValueError, match="one background"):
            realtime_voice.delegate(voice["id"], "another", "Research")
    assert ThreadMessage.objects.count() == 1
    # The budget travels with the queued message, with a start window of start_within_seconds.
    carried = from_metadata(first.metadata)
    expected = TurnBudget.from_dict({k: v for k, v in BUDGET.items() if k != "start_within_seconds"})
    assert replace(carried, expires_at=None) == expected
    assert 25 < carried.expires_at - first.created_at.timestamp() <= 30
    first.status, first.reply_text = "answered", "Verified result"
    first.save()
    assert client.get(url).json()["delegations"][0]["result"] == "Verified result"


def test_cooking_across_topic_compaction_and_reconnect(voice_setup, settings):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    vid = voice["id"]
    facts = [
        "I'm going with creamy gochujang coconut chicken using boneless thighs.",
        "Missing gochugaru, MSG, lemongrass and lime leaves.",
        "The 30-minute marinade is complete; ready to pan-fry.",
    ]
    for index, fact in enumerate(facts):
        realtime_voice.record_event(vid, f"fact-{index}", "user", fact)
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    flip = start + timedelta(minutes=3)
    with patch("django.utils.timezone.now", return_value=start):
        realtime_voice.record_event(
            vid, "start-pan", "user", "I started pan-frying the chicken.", client_event_at=start - timedelta(seconds=30)
        )
    for index in range(105):
        realtime_voice.record_event(vid, f"topic-{index}", "user", "Let's discuss LiveKit.")
    with patch("django.utils.timezone.now", return_value=flip):
        realtime_voice.record_event(vid, "flip", "user", "I just flipped the chicken.")
    realtime_voice.record_event(vid, "negated", "user", "I did not flip the chicken again.")
    realtime_voice.record_event(vid, "generated", "assistant", "Ready", "unknown")
    realtime_voice.record_event(vid, "interrupted", "assistant", "Turn it", "interrupted")
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    with patch("django_ergo.bots.messaging.dispatch"):
        message = realtime_voice.delegate(vid, "research", "Research LiveKit")
    for fact in facts:
        assert fact in message.text
    assert "start-pan" in message.text and "flip" in message.text
    restored = client.get(url).json()
    assert restored["id"] == vid
    assert facts[0] in restored["state"]["decisions"]
    assert facts[1] in restored["state"]["constraints"]
    assert facts[2] in restored["state"]["completed_steps"]
    assert restored["state"]["current_step"] == "I just flipped the chicken."
    assert [a["event_id"] for a in restored["activities"]] == ["start-pan", "flip"]
    assert [e["delivery"] for e in restored["events"][-2:]] == ["unknown", "interrupted"]
    with patch("django.utils.timezone.now", return_value=flip + timedelta(seconds=45)):
        assert client.get(url + "/time", {"activity_id": "start-pan"}).json()["elapsed_since_report_seconds"] == 225
        assert client.get(url + "/time", {"activity_id": "flip"}).json()["elapsed_since_report_seconds"] == 45
    assert realtime_voice.bridge_state(vid)["activities"][0]["reported_at_utc"] == start.isoformat()
    message.status, message.reply_text = "answered", "Research complete"
    message.save()
    assert client.get(url).json()["delegations"][0]["result"] == "Research complete"


@pytest.mark.django_db(transaction=True)
def test_concurrent_delegations_create_one_message(voice_setup, settings):
    from django.db import close_old_connections

    _, client, chat = voice_setup
    voice, _ = create(client, chat)
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    barrier = Barrier(2)

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return realtime_voice.delegate(voice["id"], "same-request", "Research").id
        finally:
            close_old_connections()

    with patch("django_ergo.bots.messaging.dispatch"), ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert results[0] == results[1]
    assert ThreadMessage.objects.count() == 1
