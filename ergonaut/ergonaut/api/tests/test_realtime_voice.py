import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
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
    settings.REALTIME_VOICE_USERS = {user.get_username()}
    skills = SimpleNamespace(
        active=["history"],
        tool_names=lambda _: ["ergo_chat_history_search"],
        requires_approval=lambda _: False,
    )
    bot = SimpleNamespace(
        make_engine=lambda _: SimpleNamespace(supports_request_limits=True, model="gpt-6-luna"),
        skillset=lambda _: skills,
    )
    monkeypatch.setattr(realtime_voice, "get_bot", lambda *args: bot)
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


def test_only_allowlisted_users_get_voice_or_a_token(voice_setup, settings):
    _, client, chat = voice_setup
    settings.LIVEKIT_URL = "wss://example.invalid"
    settings.LIVEKIT_API_KEY = "test-key"
    settings.LIVEKIT_API_SECRET = "test-secret"
    voice, url = create(client, chat)
    assert client.get("/api/realtime-voice/config").json()["enabled"]
    # Off the allowlist, even the owner of the chat and the voice session is refused.
    settings.REALTIME_VOICE_USERS = {"someone-else"}
    assert not client.get("/api/realtime-voice/config").json()["enabled"]
    assert post(client, url + "/token", {}).status_code == 404
    assert post(client, "/api/realtime-voice/sessions", {"chat_id": str(chat.id)}).status_code == 404
    assert not realtime_voice.claim_bridge(voice["id"])
    settings.REALTIME_VOICE_USERS = set()
    assert not client.get("/api/realtime-voice/config").json()["enabled"]


def test_a_voice_session_gets_one_bridge_job(voice_setup, settings):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    settings.REALTIME_VOICE_ENABLED = False
    assert not realtime_voice.claim_bridge(voice["id"])  # the kill switch
    settings.REALTIME_VOICE_ENABLED = True
    assert realtime_voice.claim_bridge(voice["id"])
    assert not realtime_voice.claim_bridge(voice["id"])  # a second dispatch for the same session
    ended, _ = create(client, chat)
    post(client, f"/api/realtime-voice/sessions/{ended['id']}/end", {})
    assert not realtime_voice.claim_bridge(ended["id"])


def test_a_claimed_session_gets_no_new_token_and_a_dead_bridge_job_ends_it(voice_setup, settings):
    _, client, chat = voice_setup
    settings.LIVEKIT_URL = "wss://example.invalid"
    settings.LIVEKIT_API_KEY = "test-key"
    settings.LIVEKIT_API_SECRET = "test-secret"
    voice, url = create(client, chat)
    assert realtime_voice.claim_bridge(voice["id"])
    # The job is (or may still be) running: no second room, but the session stays.
    assert post(client, url + "/token", {}).status_code == 409
    assert client.get(url).json()["status"] == "active"
    # A bridge that died without ending it: once the job is surely over, the session ends,
    # so the next Start creates a new one instead of reusing it.
    realtime_voice.RealtimeVoiceSession.objects.filter(pk=voice["id"]).update(
        bridge_started_at=datetime.now(UTC) - timedelta(seconds=realtime_voice.BRIDGE_JOB_SECONDS + 31)
    )
    assert post(client, url + "/token", {}).status_code == 409
    assert client.get(url).json()["status"] == "ended"
    fresh, fresh_url = create(client, chat)
    assert fresh["id"] != voice["id"]
    assert post(client, fresh_url + "/token", {}).status_code == 200


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


def test_standby_is_flagged_owner_scoped_and_requires_confirmed_close(voice_setup, settings, django_user_model):
    user, client, chat = voice_setup
    voice, url = create(client, chat)
    assert not client.get("/api/realtime-voice/config").json()["standby_enabled"]
    assert post(client, url + "/standby", {}).status_code == 404
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    assert client.get("/api/realtime-voice/config").json()["standby_enabled"]
    assert post(client, url + "/standby", {}).status_code == 409  # no bridge
    assert realtime_voice.claim_bridge(voice["id"], "a" * 32)
    assert post(client, url + "/standby", {}).json()["standby_state"] == "requested"
    assert post(client, url + "/resume", {}).status_code == 409
    assert realtime_voice.begin_standby_close(voice["id"], "a" * 32)
    assert client.get(url).json()["standby_state"] == "closing"
    assert post(client, url + "/resume", {}).status_code == 409
    other = django_user_model.objects.create_user("standby-other", password="pw")
    settings.REALTIME_VOICE_USERS.add(other.get_username())
    client.force_login(other)
    assert post(client, url + "/resume", {}).status_code == 404
    assert post(client, url + "/standby", {}).status_code == 404
    client.force_login(user)
    assert realtime_voice.finish_standby_close(voice["id"], "a" * 32)
    realtime_voice.end_bridge_session(voice["id"], "a" * 32)  # late/cancelled cleanup after publication
    assert client.get(url).json()["status"] == "active"
    assert client.get(url).json()["standby_state"] == "standby"
    assert post(client, url + "/resume", {}).json()["standby_state"] == "resuming"
    assert post(client, url + "/resume", {}).status_code == 409
    assert realtime_voice.claim_bridge(voice["id"], "b" * 32)
    assert not realtime_voice.claim_bridge(voice["id"], "c" * 32)
    realtime_voice.end_bridge_session(voice["id"], "a" * 32)
    assert client.get(url).json()["status"] == "active"
    realtime_voice.end_bridge_session(voice["id"], "b" * 32)
    assert client.get(url).json()["status"] == "ended"


def test_end_and_expiry_beat_standby_cleanup_and_preserve_late_result(voice_setup, settings):
    from ergonaut.realtime_voice_outbox import claim_announcement, completion_event

    _, client, chat = voice_setup
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    voice, url = create(client, chat)
    vid = voice["id"]
    assert realtime_voice.claim_bridge(vid, "a" * 32)
    original = realtime_voice.RealtimeVoiceSession.objects.get(pk=vid).bridge_started_at
    with patch("django_ergo.bots.messaging.dispatch"):
        message = realtime_voice.delegate(vid, "task", "Research")
    assert post(client, url + "/standby", {}).status_code == 200
    assert realtime_voice.begin_standby_close(vid, "a" * 32)
    assert realtime_voice.finish_standby_close(vid, "a" * 32)
    message.status, message.reply_text = "answered", "Late result"
    message.save()
    task = realtime_voice.bridge_state(vid)["delegations"][0]
    event = completion_event(vid, task)
    assert claim_announcement(event)
    assert post(client, url + "/resume", {}).status_code == 200
    assert realtime_voice.claim_bridge(vid, "b" * 32)
    assert realtime_voice.RealtimeVoiceSession.objects.get(pk=vid).bridge_started_at == original
    assert not claim_announcement(completion_event(vid, task))
    with pytest.raises(ValueError, match="one background request"):
        realtime_voice.delegate(vid, "another", "Again")
    assert post(client, url + "/standby", {}).status_code == 200
    assert realtime_voice.begin_standby_close(vid, "b" * 32)
    assert post(client, url + "/end", {}).json()["status"] == "ended"
    assert not realtime_voice.finish_standby_close(vid, "b" * 32)
    assert post(client, url + "/resume", {}).status_code == 409
    assert client.get(url).json()["delegations"][0]["result"] == "Late result"

    fresh, fresh_url = create(client, chat)
    assert realtime_voice.claim_bridge(fresh["id"], "d" * 32)
    assert post(client, fresh_url + "/standby", {}).status_code == 200
    assert realtime_voice.begin_standby_close(fresh["id"], "d" * 32)
    assert realtime_voice.finish_standby_close(fresh["id"], "d" * 32)
    realtime_voice.RealtimeVoiceSession.objects.filter(pk=fresh["id"]).update(
        bridge_started_at=datetime.now(UTC) - timedelta(seconds=90)
    )
    assert client.get(fresh_url).json()["status"] == "ended"
    assert post(client, fresh_url + "/resume", {}).status_code == 409


@pytest.mark.django_db(transaction=True)
def test_concurrent_resume_and_bridge_claim_are_one_shot(voice_setup, settings):
    from types import SimpleNamespace

    from django.db import close_old_connections
    from ninja.errors import HttpError

    user, client, chat = voice_setup
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    voice, url = create(client, chat)
    vid = voice["id"]
    assert realtime_voice.claim_bridge(vid, "a" * 32)
    assert post(client, url + "/standby", {}).status_code == 200
    assert realtime_voice.begin_standby_close(vid, "a" * 32)
    assert realtime_voice.finish_standby_close(vid, "a" * 32)
    barrier = Barrier(2)

    def resume(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                realtime_voice.resume_session(SimpleNamespace(auth=user), vid)
                return True
            except HttpError as exc:
                assert exc.status_code == 409
                return False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(resume, range(2))) == [False, True]
    barrier = Barrier(2)

    def claim(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return realtime_voice.claim_bridge(vid, str(index) * 32)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (1, 2))) == [False, True]


def test_voice_decisions_seed_the_next_interface_to_the_same_thread(voice_setup):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    realtime_voice.record_event(voice["id"], "decision", "user", "We will use coconut milk.")
    assert post(client, url + "/end", {}).status_code == 200
    fresh, _ = create(client, chat)
    assert fresh["id"] != voice["id"]
    assert fresh["state"]["decisions"] == ["We will use coconut milk."]
    assert "We will use coconut milk." in realtime_voice.bridge_state(fresh["id"])["briefing"]["context"]
    assert chat.messages.filter(provenance__voice_event_id="decision").count() == 1


def draft(turn_id, role="user", text="Partial transcript"):
    return {
        "turn_id": turn_id,
        "role": role,
        "text": text,
        "is_final": False,
        "delivery": "unknown",
        "created_at": datetime.now(UTC).isoformat(),
    }


def test_progressive_transcript_is_owner_scoped_bounded_and_finalized_only(voice_setup, django_user_model):
    user, client, chat = voice_setup
    voice, url = create(client, chat)
    vid = voice["id"]
    assert voice["transcript"] == []
    assert voice["runtime"] == realtime_voice.UNKNOWN_RUNTIME
    assert realtime_voice.claim_bridge(vid, "claim")
    drafts = [draft(f"turn-{i}", text="x" * 5000) for i in range(8)]
    runtime = {"user_state": "listening", "agent_state": "thinking", "observed_at": datetime.now(UTC).isoformat()}
    assert realtime_voice.publish_live_state(vid, "claim", drafts, runtime)
    current = client.get(url).json()
    assert len(current["transcript"]) == 4
    assert all(len(turn["text"]) == 4000 and not turn["is_final"] for turn in current["transcript"])
    assert current["events"] == []
    assert current["runtime"] == runtime
    assert realtime_voice.LIVE_STATE_KEY not in current["state"]
    assert not chat.messages.exists()
    realtime_voice.record_event(vid, "turn-7", "user", "Final transcript", bridge_claim_id="claim")
    # A stale coalesced publication cannot recreate a draft after the final.
    assert realtime_voice.publish_live_state(vid, "claim", drafts, runtime)
    current = client.get(url).json()
    [final] = [turn for turn in current["transcript"] if turn["turn_id"] == "turn-7"]
    assert final["is_final"] and final["event_id"] == "turn-7" and final["text"] == "Final transcript"
    assert datetime.fromisoformat(final["created_at"]).tzinfo
    assert chat.messages.count() == 1
    assert len(current["events"]) == 1
    other = django_user_model.objects.create_user("progressive-other", password="pw")
    client.force_login(other)
    assert client.get(url).status_code == 404
    assert client.get(url + "/events").status_code == 404
    client.force_login(user)
    for path in ("transcript", "runtime"):
        assert post(client, f"{url}/{path}", {"text": "forged", "agent_state": "speaking"}).status_code == 404


def test_browser_playback_reports_are_owner_scoped_bounded_and_never_history(voice_setup, django_user_model):
    user, client, chat = voice_setup
    voice, url = create(client, chat)
    report = url + "/client-audio"
    blocked = {"event_id": "r-1", "status": "playback_blocked", "detail": "NotAllowedError"}
    assert post(client, report, blocked).json() == {"recorded": True}
    assert post(client, report, blocked).json() == {"recorded": True}  # a retried report is idempotent
    assert post(client, report, {"event_id": "r-1", "status": "playback_started"}).status_code == 409
    for invalid in (
        {"event_id": "r-2", "status": "delivered"},  # cannot claim assistant delivery
        {"event_id": "r 2!", "status": "playback_started"},
        {"event_id": "r-2", "status": "playback_failed", "detail": "<speaker name>"},
    ):
        assert post(client, report, invalid).status_code == 400
    current = client.get(url).json()
    assert [(e["kind"], e["text"]) for e in current["events"]] == [
        ("client_audio", "Browser blocked assistant audio playback. (NotAllowedError)")
    ]
    assert current["transcript"] == [] and not chat.messages.exists()
    for index in range(realtime_voice.MAX_CLIENT_AUDIO_EVENTS - 1):
        assert post(client, report, {"event_id": f"n-{index}", "status": "track_subscribed"}).json()["recorded"]
    assert post(client, report, {"event_id": "over", "status": "turn_unplayed"}).json() == {"recorded": False}
    assert post(client, report, blocked).json() == {"recorded": True}  # an existing report is still acknowledged
    other = django_user_model.objects.create_user("playback-other", password="pw")
    client.force_login(other)
    assert post(client, report, {"event_id": "x", "status": "playback_started"}).status_code == 404
    client.force_login(user)
    post(client, url + "/end", {})
    assert post(client, report, {"event_id": "late", "status": "playback_started"}).status_code == 409


def test_live_state_and_final_callbacks_are_claim_scoped_across_standby_stop_and_deadline(voice_setup, settings):
    _, client, chat = voice_setup
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    voice, url = create(client, chat)
    vid = voice["id"]
    runtime = {"user_state": "listening", "agent_state": "speaking", "observed_at": datetime.now(UTC).isoformat()}
    assert realtime_voice.claim_bridge(vid, "first")
    assert realtime_voice.publish_live_state(vid, "first", [draft("draft")], runtime)
    assert post(client, url + "/standby", {}).status_code == 200
    assert realtime_voice.begin_standby_close(vid, "first")
    assert client.get(url).json()["runtime"] == realtime_voice.UNKNOWN_RUNTIME
    assert not realtime_voice.publish_live_state(vid, "first", [draft("late")], runtime)
    assert realtime_voice.finish_standby_close(vid, "first")
    assert client.get(url).json()["transcript"] == []
    assert post(client, url + "/resume", {}).json()["runtime"] == realtime_voice.UNKNOWN_RUNTIME
    assert realtime_voice.claim_bridge(vid, "second")
    assert not realtime_voice.publish_live_state(vid, "first", [draft("late")], runtime)
    assert realtime_voice.record_event(vid, "stale-final", "assistant", "Old callback", bridge_claim_id="first") is None
    assert realtime_voice.publish_live_state(vid, "second", [draft("new")], runtime)
    accepted_at = datetime.now(UTC)
    assert post(client, url + "/end", {}).json()["runtime"] == realtime_voice.UNKNOWN_RUNTIME
    # A final received before Stop is drained once, but later callbacks are rejected.
    assert realtime_voice.record_event(
        vid, "accepted", "assistant", "Accepted before stop", bridge_claim_id="second", received_at=accepted_at
    )
    assert realtime_voice.record_event(vid, "after", "assistant", "Too late", bridge_claim_id="second") is None
    assert not realtime_voice.publish_live_state(vid, "second", [draft("after")], runtime)
    ended = client.get(url).json()
    assert ended["runtime"] == realtime_voice.UNKNOWN_RUNTIME
    assert [(turn["turn_id"], turn["is_final"]) for turn in ended["transcript"]] == [("accepted", True)]
    assert chat.messages.count() == 1
    fresh, fresh_url = create(client, chat)
    assert fresh["transcript"] == [] and fresh["runtime"] == realtime_voice.UNKNOWN_RUNTIME
    assert realtime_voice.claim_bridge(fresh["id"], "deadline")
    realtime_voice.RealtimeVoiceSession.objects.filter(pk=fresh["id"]).update(
        bridge_started_at=datetime.now(UTC) - timedelta(seconds=realtime_voice.BRIDGE_JOB_SECONDS)
    )
    assert not realtime_voice.publish_live_state(fresh["id"], "deadline", [draft("expired")], runtime)
    assert (
        realtime_voice.record_event(fresh["id"], "expired", "assistant", "Expired", bridge_claim_id="deadline") is None
    )
    assert client.get(fresh_url).json()["runtime"] == realtime_voice.UNKNOWN_RUNTIME


def test_transcript_keeps_only_last_hundred_final_events_and_never_duplicates(voice_setup):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    vid = voice["id"]
    for index in range(105):
        realtime_voice.record_event(vid, str(index), "assistant", f"Final {index}", "interrupted")
    realtime_voice.record_event(vid, "104", "assistant", "Final 104", "interrupted")
    current = client.get(url).json()
    assert len(current["events"]) == len(current["transcript"]) == 100
    assert len({turn["turn_id"] for turn in current["transcript"]}) == 100
    assert all(turn["is_final"] and turn["delivery"] == "interrupted" for turn in current["transcript"])
    assert chat.messages.count() == 105


@pytest.mark.parametrize("restriction", ["kill_switch", "allowlist"])
def test_live_publisher_and_late_finals_obey_voice_access_switches(voice_setup, settings, restriction):
    _, client, chat = voice_setup
    voice, url = create(client, chat)
    vid = voice["id"]
    assert realtime_voice.claim_bridge(vid, "restricted")
    if restriction == "kill_switch":
        settings.REALTIME_VOICE_ENABLED = False
    else:
        settings.REALTIME_VOICE_USERS = set()
    assert not realtime_voice.publish_live_state(
        vid, "restricted", [draft("forbidden")], dict(realtime_voice.UNKNOWN_RUNTIME)
    )
    assert realtime_voice.record_event(vid, "forbidden", "user", "Forbidden", bridge_claim_id="restricted") is None
    assert client.get(url).status_code == 404
    assert not chat.messages.exists()
    assert not realtime_voice.RealtimeVoiceSession.objects.get(pk=vid).events.exists()
