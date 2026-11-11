import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django_ergo.conversation.models import ConversationSession, ThreadMessage

from ergonaut.api import realtime_voice


@pytest.fixture
def voice_setup(test_user, settings, monkeypatch):
    user, client = test_user
    settings.REALTIME_VOICE_ENABLED = True
    monkeypatch.setattr(realtime_voice, "get_bot", lambda *args: object())
    chat = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    return user, client, chat


def post(client, url, data):
    return client.post(url, json.dumps(data), content_type="application/json")


def test_authorization_and_feature_flag(voice_setup, settings, django_user_model):
    user, client, chat = voice_setup
    root = "/api/realtime-voice/sessions"
    created = post(client, root, {"chat_id": str(chat.id)}).json()
    url = f"{root}/{created['id']}"
    assert client.get(url).status_code == 200
    settings.REALTIME_VOICE_ENABLED = False
    assert client.get(url).status_code == 404
    assert not client.get("/api/realtime-voice/config").json()["enabled"]
    settings.REALTIME_VOICE_ENABLED = True
    other = django_user_model.objects.create_user("other", "other@example.com", "pw")
    client.force_login(other)
    assert client.get(url).status_code == 404
    assert post(client, url + "/token", {}).status_code == 404
    assert post(client, url + "/end", {}).status_code == 404
    assert post(client, url + "/events", {"event_id": "x", "kind": "user_transcript", "text": "x"}).status_code == 404
    assert post(client, url + "/delegations", {"event_id": "x", "text": "x"}).status_code == 404
    assert post(client, root, {"chat_id": str(chat.id)}).status_code == 404
    client.logout()
    assert client.get(url).status_code == 401


def test_token_is_scoped_to_voice_room(voice_setup, settings):
    jwt = pytest.importorskip("jwt")
    pytest.importorskip("livekit.api")
    _, client, chat = voice_setup
    settings.LIVEKIT_URL = "wss://example.invalid"
    settings.LIVEKIT_API_KEY = "test-key"
    settings.LIVEKIT_API_SECRET = "test-secret"
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    response = post(client, f"{root}/{voice['id']}/token", {})
    assert response.status_code == 200
    claims = jwt.decode(
        response.json()["token"], settings.LIVEKIT_API_SECRET, algorithms=["HS256"], options={"verify_aud": False}
    )
    assert claims["video"]["room"] == f"voice-{voice['id']}"
    assert claims["video"]["roomJoin"] is True
    assert claims["exp"] - claims["nbf"] <= 300


def test_reconnect_context_and_idempotent_delegation(voice_setup):
    user, client, chat = voice_setup
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    url = f"{root}/{voice['id']}"
    assert (
        post(
            client,
            url + "/state",
            {"decisions": ["use blue"], "constraints": ["no deployment"], "current_step": "research"},
        ).status_code
        == 200
    )
    event = {"event_id": "turn-1", "kind": "user_transcript", "text": "Choose blue"}
    assert post(client, url + "/events", event).status_code == 200
    assert post(client, url + "/events", event).status_code == 200
    with patch("django_ergo.bots.messaging.dispatch"):
        request = {"event_id": "call-id-not-a-uuid", "text": "Research the choice"}
        first = post(client, url + "/delegations", request).json()
        second = post(client, url + "/delegations", request).json()
    assert first["message_id"] == second["message_id"]
    assert ThreadMessage.objects.count() == 1
    message = ThreadMessage.objects.get(pk=first["message_id"])
    assert "Choose blue" in message.text and "no deployment" in message.text
    message.status = "answered"
    message.reply_text = "Research complete"
    message.save()
    restored = client.get(url).json()
    assert restored["events"][0]["text"] == "Choose blue"
    assert restored["state"]["current_step"] == "research"
    assert restored["delegations"][0]["result"] == "Research complete"
    assert post(client, url + "/end", {}).json()["status"] == "ended"
    assert post(client, url + "/token", {}).status_code == 409
    assert post(client, url + "/delegations", {"event_id": "new", "text": "more"}).status_code == 409


def test_cooking_context_survives_cross_topic_delegation_and_reconnect(voice_setup):
    _, client, chat = voice_setup
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    url = f"{root}/{voice['id']}"
    facts = [
        "Creamy gochujang coconut chicken using boneless skinless thighs.",
        "Missing gochugaru, MSG, lemongrass and lime leaves.",
        "The 30-minute marinade is complete; ready to pan-fry.",
        "Let's discuss a different topic while that runs.",
    ]
    for index, text in enumerate(facts):
        assert (
            post(client, url + "/events", {"event_id": f"turn-{index}", "kind": "user", "text": text}).status_code
            == 200
        )
    realtime_voice.record_event(voice["id"], "generated", "assistant", "Ready", "unknown")
    realtime_voice.record_event(voice["id"], "interrupted", "assistant", "Start frying", "interrupted")
    realtime_voice.record_event(voice["id"], "unknown", "assistant", "Use a pan", "unknown")
    with patch("django_ergo.bots.messaging.dispatch"):
        request = {"event_id": "cook-request", "text": "Find a substitution, then return to the chicken."}
        first = post(client, url + "/delegations", request).json()
        assert post(client, url + "/delegations", request).json()["message_id"] == first["message_id"]
    message = ThreadMessage.objects.get(pk=first["message_id"])
    for fact in facts:
        assert fact in message.text
    message.status, message.reply_text = "answered", "Use available ingredients; the marinade is already complete."
    message.save()
    restored = client.get(url).json()
    assert facts[0] in restored["state"]["decisions"]
    assert facts[1] in restored["state"]["constraints"]
    assert restored["state"]["current_step"] == facts[2]
    assert restored["state"]["completed_task_ids"] == ["cook-request"]
    assert restored["id"] == voice["id"]
    assert [event["delivery"] for event in restored["events"][-3:]] == ["unknown", "interrupted", "unknown"]
    assert restored["delegations"][0]["result"] == message.reply_text
    assert post(client, url + "/delegations", request).json()["message_id"] == first["message_id"]
    assert ThreadMessage.objects.count() == 1


def test_reported_activity_time_is_distinct_from_uncertain_speech_time(voice_setup):
    _, client, chat = voice_setup
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    url = f"{root}/{voice['id']}"
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    flip = start + timedelta(minutes=3)
    client_time = start - timedelta(seconds=30)
    with patch("django.utils.timezone.now", return_value=start):
        response = post(
            client,
            url + "/events",
            {
                "event_id": "start",
                "kind": "user",
                "text": "I started frying the chicken.",
                "client_event_at": client_time.isoformat(),
            },
        )
    assert response.status_code == 200
    assert response.json()["received_at"] == start.isoformat()
    assert response.json()["processed_at"] == start.isoformat()
    assert response.json()["client_event_at"] == client_time.isoformat()
    with patch("django.utils.timezone.now", return_value=flip):
        assert (
            post(
                client, url + "/events", {"event_id": "flip", "kind": "user", "text": "I flipped the chicken."}
            ).status_code
            == 200
        )
    with patch("django.utils.timezone.now", return_value=flip + timedelta(seconds=45)):
        elapsed = client.get(url + "/time", {"activity_id": "flip"}).json()
        assert elapsed["elapsed_since_report_seconds"] == 45
        assert elapsed["activity"]["reported_at_utc"] == flip.isoformat()
        assert client.get(url + "/time", {"activity_id": "start"}).json()["elapsed_since_report_seconds"] == 225
        assert client.get(url + "/time", {"activity_id": "unknown"}).json()["elapsed_since_report_seconds"] is None
    restored = client.get(url).json()
    assert [a["event_id"] for a in restored["activities"]] == ["start", "flip"]
    assert (
        post(
            client,
            url + "/events",
            {
                "event_id": "start",
                "kind": "user",
                "text": "I started frying the chicken.",
                "client_event_at": client_time.isoformat(),
            },
        ).status_code
        == 200
    )
    assert len(client.get(url).json()["activities"]) == 2
    assert (
        post(
            client, url + "/events", {"event_id": "fake", "kind": "result", "text": "done", "delivery": "delivered"}
        ).status_code
        == 403
    )


def test_chat_scoped_session_list_includes_ended(voice_setup, django_user_model):
    _, client, chat = voice_setup
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    post(client, f"{root}/{voice['id']}/end", {})
    response = client.get(root, {"chat_id": str(chat.id)})
    assert response.status_code == 200
    assert response.json()[0]["id"] == voice["id"]
    assert response.json()[0]["status"] == "ended"
    assert response.json()[0]["created_at"]
    other = django_user_model.objects.create_user("other-list", "other-list@example.com", "pw")
    client.force_login(other)
    assert client.get(root, {"chat_id": str(chat.id)}).status_code == 404


def test_cooking_report_elapsed_survives_compaction_delegation_and_reconnect(voice_setup):
    _, client, chat = voice_setup
    root = "/api/realtime-voice/sessions"
    voice = post(client, root, {"chat_id": str(chat.id)}).json()
    url = f"{root}/{voice['id']}"
    facts = [
        "Creamy gochujang coconut chicken using boneless skinless thighs.",
        "Missing gochugaru, MSG, lemongrass and lime leaves.",
        "The 30-minute marinade is complete; ready to pan-fry.",
    ]
    for index, text in enumerate(facts):
        assert (
            post(client, url + "/events", {"event_id": f"fact-{index}", "kind": "user", "text": text}).status_code
            == 200
        )
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    flip = start + timedelta(minutes=3)
    with patch("django.utils.timezone.now", return_value=start):
        assert (
            post(
                client,
                url + "/events",
                {"event_id": "start-pan", "kind": "user", "text": "I started pan-frying the chicken."},
            ).status_code
            == 200
        )
    for index in range(105):
        assert (
            post(
                client,
                url + "/events",
                {"event_id": f"topic-{index}", "kind": "user", "text": "Let's discuss LiveKit."},
            ).status_code
            == 200
        )
    with patch("django.utils.timezone.now", return_value=flip):
        assert (
            post(
                client, url + "/events", {"event_id": "flip", "kind": "user", "text": "I flipped the chicken."}
            ).status_code
            == 200
        )
    with patch("django_ergo.bots.messaging.dispatch"):
        result = post(client, url + "/delegations", {"event_id": "research", "text": "Research LiveKit."}).json()
    prompt = ThreadMessage.objects.get(pk=result["message_id"]).text
    for fact in facts:
        assert fact in prompt
    assert "start-pan" in prompt and "flip" in prompt
    restored = client.get(url).json()
    assert restored["id"] == voice["id"]
    assert facts[0] in restored["state"]["decisions"]
    assert facts[1] in restored["state"]["constraints"]
    assert restored["state"]["current_step"] == facts[2]
    assert [activity["event_id"] for activity in restored["activities"]] == ["start-pan", "flip"]
    with patch("django.utils.timezone.now", return_value=flip + timedelta(seconds=45)):
        assert client.get(url + "/time", {"activity_id": "start-pan"}).json()["elapsed_since_report_seconds"] == 225
        assert client.get(url + "/time", {"activity_id": "flip"}).json()["elapsed_since_report_seconds"] == 45
