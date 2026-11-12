"""The cooking/time scenario with a real, budgeted delegation (no hosted audio, no provider call):
the request runs through the queue and turn runner under its budget, and its truthful outcome returns
to the same voice session through the durable outbox."""

import socket
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from celery import Celery
from django_ergo.bots import messaging, webhooks
from django_ergo.conversation.models import ConversationSession

from ergonaut.api import realtime_voice
from ergonaut.api.realtime_voice_state import current_time
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.apps.bots.tests.test_api import bot_folder, cook, use_bots  # noqa: F401
from ergonaut.realtime_voice_outbox import claim_announcement, completion_event

BUDGET = {
    "max_turns": 3,
    "max_input_tokens": 40_000,
    "max_output_tokens": 1_500,
    "deadline_seconds": 45,
    "allowed_tools": ["ergo_chat_history_sources"],
    "model": "claude-test",  # the engine the fake client stands in for
    "start_within_seconds": 30,
}
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FLIP = START + timedelta(minutes=3)


@pytest.fixture(autouse=True)
def held_messages(settings):
    """The queue records the message; the test runs its turn, as a worker would."""
    held = []
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "THREAD_MESSAGE_RUNNER": held.append}
    settings.REALTIME_VOICE_ENABLED = True
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    return held


@pytest.fixture
def cooking(cook):  # noqa: F811
    chat = ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "main"})
    other = RealtimeVoiceSession.objects.create(user=cook, chat=chat)
    voice = RealtimeVoiceSession.objects.create(user=cook, chat=chat)
    vid = str(voice.id)
    realtime_voice.record_event(vid, "fact-0", "user", "I'm going with creamy gochujang coconut chicken.")
    with patch("django.utils.timezone.now", return_value=START):
        realtime_voice.record_event(vid, "start-pan", "user", "I started pan-frying the chicken.")
    with patch("django.utils.timezone.now", return_value=FLIP):
        realtime_voice.record_event(vid, "flip", "user", "I just flipped the chicken.")
    return chat, vid, str(other.id)


def run_delegation(vid):
    message = realtime_voice.delegate(vid, "frying-time", "How long ago did I start frying, and when did I flip?")
    messaging.deliver(str(message.id), registry=webhooks.get_registry())
    message.refresh_from_db()
    return message


def outcome(vid):
    [task] = realtime_voice.bridge_state(vid)["delegations"]
    return task


@pytest.mark.django_db(transaction=True)
def test_a_budgeted_delegation_returns_its_answer_to_the_same_voice_session(cooking, use_bots):  # noqa: F811
    chat, vid, other = cooking
    client = use_bots(
        tool_call("ergo_chat_history_sources", {}),
        say("You started frying at 12:00:00 UTC and flipped at 12:03:00."),
    )

    message = run_delegation(vid)

    # The delegated turn saw the session's context, and only its allowed read-only tool.
    first = client.calls[0]
    prompt = " ".join(b.get("text", "") for m in first["messages"] for b in m["content"])
    assert "start-pan" in prompt and "I just flipped the chicken." in prompt
    assert "How long ago did I start frying" in prompt
    assert [t["name"] for t in first["tools"]] == ["ergo_chat_history_sources", "send_reply"]
    assert all(options["max_retries"] == 0 for options in client.options)
    call = chat.structured_calls.get()
    assert call.metadata["budget"]["used"]["model_turns"] == 2

    # The result is the thread message's reply; the outbox records it once, in this voice session only.
    task = outcome(vid)
    assert (task["status"], task["result"]) == ("answered", message.reply_text)
    event = completion_event(vid, task)
    assert event.session_id == RealtimeVoiceSession.objects.get(pk=vid).pk
    assert event.text == "You started frying at 12:00:00 UTC and flipped at 12:03:00."
    assert claim_announcement(event) and not claim_announcement(completion_event(vid, task))
    assert not RealtimeVoiceSession.objects.get(pk=other).events.exists()

    # Elapsed time is the server's, since the user's report, not the model's guess.
    with patch("django.utils.timezone.now", return_value=START + timedelta(minutes=4)):
        assert current_time(vid, "start-pan")["elapsed_since_report_seconds"] == 240
        assert current_time(vid, "flip")["elapsed_since_report_seconds"] == 60


@pytest.mark.django_db(transaction=True)
def test_a_delegation_that_hits_its_budget_reports_the_real_reason_to_the_same_session(cooking, use_bots):  # noqa: F811
    chat, vid, other = cooking
    client = use_bots(*[tool_call("ergo_chat_history_sources", {}) for _ in range(4)])

    message = run_delegation(vid)

    assert message.status == "failed"
    assert len(client.calls) == 3  # BUDGET["max_turns"]: the fourth request was never sent
    task = outcome(vid)
    assert task["status"] == "failed" and "max_turns" in task["error"]
    event = completion_event(vid, task)
    assert event.text.startswith("The background request failed: Stopped by the request budget (max_turns)")
    assert "no final answer" in event.text
    assert event.session_id == RealtimeVoiceSession.objects.get(pk=vid).pk
    assert not RealtimeVoiceSession.objects.get(pk=other).events.exists()


@pytest.mark.django_db(transaction=True)
def test_a_failure_that_is_not_the_librarys_own_text_is_not_read_to_the_user(cooking):
    _, vid, _ = cooking
    message = realtime_voice.delegate(vid, "frying-time", "How long?")
    messaging.fail(message, "Traceback: connection to 10.0.0.5 refused (key sk-live-123)")

    event = completion_event(vid, outcome(vid))

    assert event.text == "The background request failed."


@pytest.mark.django_db(transaction=True)
def test_unavailable_broker_fails_delegation_and_reports_it_in_the_same_session(cooking, settings):
    _, vid, other = cooking
    # Keep the port reserved without listening so the broker connection is refused.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        app = Celery("voice-unavailable", broker=f"redis://127.0.0.1:{sock.getsockname()[1]}/0")
        settings.DJANGO_ERGO = {
            **settings.DJANGO_ERGO,
            "THREAD_MESSAGE_RUNNER": lambda message_id: app.send_task("test.deliver", args=[message_id], retry=False),
        }
        try:
            message = realtime_voice.delegate(vid, "unavailable", "Check the background request")
        finally:
            app.close()
    assert message.status == "failed"
    assert message.error == "Not run: the background request could not be dispatched."
    task = outcome(vid)
    assert task["status"] == "failed"
    event = completion_event(vid, task)
    assert event.text == "The background request failed: Not run: the background request could not be dispatched."
    assert claim_announcement(event)
    assert not claim_announcement(event)
    assert not RealtimeVoiceSession.objects.get(pk=other).events.exists()


@pytest.mark.django_db(transaction=True)
def test_the_priced_live_test_profile_is_valid_and_its_tools_are_really_offered(cooking, use_bots, settings):  # noqa: F811
    from ergonaut.settings import voice_live_test

    profile = voice_live_test.REALTIME_VOICE_DELEGATION_BUDGET
    # The fake engine stands in for gpt-6-luna; everything else is the profile the docs price.
    settings.REALTIME_VOICE_DELEGATION_BUDGET = {**profile, "model": "claude-test"}
    _, vid, _ = cooking
    client = use_bots(say("Nothing more to add."))

    message = run_delegation(vid)

    assert message.status == "answered"  # the profile is a budget the runner accepts
    assert [t["name"] for t in client.calls[0]["tools"]] == [*sorted(profile["allowed_tools"]), "send_reply"]
