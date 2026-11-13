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
    "model": "gpt-6-luna",  # scripted engine only; never calls a provider
    "start_within_seconds": 30,
}
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FLIP = START + timedelta(minutes=3)


@pytest.fixture(autouse=True)
def held_messages(settings, monkeypatch):
    """The queue records the message; the test runs its turn, as a worker would."""
    from django_ergo.bots.runtime import Bot

    original = Bot.make_engine

    def scripted_engine(bot, *args, **kwargs):
        engine = original(bot, *args, **kwargs)
        if bot._engine_factory is not None:
            engine.model = "gpt-6-luna"
        return engine

    monkeypatch.setattr(Bot, "make_engine", scripted_engine)
    held = []
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "THREAD_MESSAGE_RUNNER": held.append}
    settings.REALTIME_VOICE_ENABLED = True
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    return held


@pytest.fixture
def cooking(cook, use_bots, settings):  # noqa: F811
    use_bots(say("Scripted fallback."))
    settings.REALTIME_VOICE_USERS = {cook.get_username()}
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
        # Not the current app: shared tasks in later tests would publish to this dead broker.
        app = Celery("voice-unavailable", broker=f"redis://127.0.0.1:{sock.getsockname()[1]}/0", set_as_current=False)
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
    settings.REALTIME_VOICE_DELEGATION_BUDGET = profile
    _, vid, _ = cooking
    client = use_bots(say("Nothing more to add."))

    message = run_delegation(vid)

    assert message.status == "answered"  # the profile is a budget the runner accepts
    # Non-orchestration chats do not offer bot/thread tools; the ceiling does not enable them.
    expected = sorted(set(profile["allowed_tools"]) - {"ergo_bot_list", "ergo_thread_list"})
    assert [t["name"] for t in client.calls[0]["tools"]] == [*expected, "send_reply"]


@pytest.mark.django_db(transaction=True)
def test_startup_context_is_the_populated_launch_thread_not_a_global_briefing(
    cooking,
    cook,  # noqa: F811
    use_bots,  # noqa: F811
    django_user_model,
):
    from asgiref.sync import async_to_sync
    from django_ergo.conversation.messages import add_message
    from django_ergo.conversation.models import ConversationAttachment, ConversationCompaction

    chat, vid, _ = cooking
    use_bots(say("No network."))
    chat.system_prompt = "Launch thread owning instructions."
    chat.metadata = {
        **chat.metadata,
        "custom_instructions": True,
        "voice_state": {"current_step": "Currently inspecting the pan.", "decisions": ["We chose coconut."]},
    }
    chat.save()
    async_to_sync(add_message)(
        chat, "user", [{"block_type": "text", "text": "Current task: finish the coconut chicken."}]
    )
    ConversationCompaction.objects.create(
        session=chat,
        mode="context_size",
        from_sequence=0,
        upto_sequence=0,
        summary="Durable summary: use boneless thighs.",
    )
    attachment = ConversationAttachment.objects.create(
        session=chat, filename="recipe.txt", kind="document", media_type="text/plain"
    )
    other_owner = django_user_model.objects.create_user("not-cook")
    for owner in (cook, other_owner):
        other_chat = ConversationSession.objects.create(
            user=owner, bot_name="kitchen", metadata={"bot_role": "thread", "title": "Unrelated private work"}
        )
        async_to_sync(add_message)(other_chat, "user", [{"block_type": "text", "text": "UNRELATED-THREAD-SECRET"}])
    # Superuser visibility must not turn the launch context into an admin/global overview.
    cook.is_superuser = True
    cook.save(update_fields=["is_superuser"])

    snapshot = realtime_voice.bridge_state(vid)
    briefing = snapshot["briefing"]
    text = briefing["context"]
    assert briefing["delegation_available"]
    assert "Restricted read-only mode" in briefing["restriction"]
    assert "gpt-6-luna" in briefing["restriction"]
    assert briefing["instructions"] == "Launch thread owning instructions."
    for expected in (
        "finish the coconut chicken",
        "Durable summary: use boneless thighs.",
        "Currently inspecting the pan.",
        "We chose coconut.",
        str(attachment.pk),
        str(chat.pk),
    ):
        assert expected in text
    assert "UNRELATED-THREAD-SECRET" not in text
    assert "Unrelated private work" not in text
    assert len(text) <= realtime_voice.BRIEFING_CONTEXT_CHARS
    assert "NOT lookup results" in text and "not known" in text


@pytest.mark.django_db(transaction=True)
def test_final_voice_messages_and_decisions_are_idempotent_durable_thread_history(cooking):
    from django_ergo.conversation.history import SessionSource

    chat, vid, other = cooking
    realtime_voice.record_event(vid, "choice", "user", "We will use coconut milk.")
    realtime_voice.record_event(vid, "choice", "user", "We will use coconut milk.")
    realtime_voice.record_event(vid, "speech", "assistant", "Use a gentle heat.")
    realtime_voice.record_event(vid, "speech", "assistant", "Use a gentle heat.", "interrupted", trusted_delivery=True)
    users = chat.messages.filter(provenance__voice_session_id=vid, provenance__voice_event_id="choice")
    assert users.count() == 1
    user = users.get()
    assert user.author["ref"] == str(chat.user_id)
    assert user.created_at == RealtimeVoiceSession.objects.get(pk=vid).events.get(event_id="choice").received_at
    speech = chat.messages.get(provenance__voice_session_id=vid, provenance__voice_event_id="speech")
    assert speech.role == "assistant" and speech.provenance["voice_delivery"] == "interrupted"
    assert speech.content_blocks.get().text == "[Generated voice speech; delivery interrupted] Use a gentle heat."
    chat.refresh_from_db()
    assert "We will use coconut milk." in chat.metadata["voice_state"]["decisions"]
    assert "Use a gentle heat." in "\n".join(row.text() for row in SessionSource(chat).messages())
    restored = realtime_voice.bridge_state(vid)
    assert restored["briefing"]["context"].count("We will use coconut milk.") >= 1
    assert not RealtimeVoiceSession.objects.get(pk=other).events.filter(event_id="choice").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("capable", "model", "reason"),
    [(False, "gpt-6-luna", "cannot enforce"), (True, "another-model", "not on the pinned")],
)
def test_unsupported_target_explains_mode_before_any_delegation(cooking, monkeypatch, capable, model, reason):
    from types import SimpleNamespace

    _, vid, _ = cooking
    bot = webhooks.get_registry().get("kitchen")
    monkeypatch.setattr(bot, "make_engine", lambda _: SimpleNamespace(supports_request_limits=capable, model=model))
    briefing = realtime_voice.bridge_state(vid)["briefing"]
    assert not briefing["delegation_available"]
    assert reason in briefing["restriction"]
    assert "dedicated budgeted API-engine profile" in briefing["restriction"]
    with pytest.raises(ValueError, match=reason):
        realtime_voice.delegate(vid, "unavailable", "Check the thread.")
    assert not RealtimeVoiceSession.objects.get(pk=vid).delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_budget_cannot_authorize_write_tools_or_unpinned_models(cooking, settings):
    _, vid, _ = cooking
    for config, reason in (
        ({**BUDGET, "allowed_tools": ["ergo_thread_forward"]}, "read-only ceiling"),
        ({**BUDGET, "model": ""}, "must pin gpt-6-luna"),
    ):
        settings.REALTIME_VOICE_DELEGATION_BUDGET = config
        assert not realtime_voice.bridge_state(vid)["briefing"]["delegation_available"]
        with pytest.raises(ValueError, match=reason):
            realtime_voice.delegate(vid, "not-authorized", "Forward this.")
    assert not RealtimeVoiceSession.objects.get(pk=vid).delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_reviewed_read_only_tools_remain_owner_scoped_and_writes_are_never_offered(
    cooking,
    use_bots,  # noqa: F811
    bot_folder,  # noqa: F811
    settings,
    django_user_model,
):
    from ergonaut.settings.voice_live_test import REALTIME_VOICE_DELEGATION_BUDGET

    chat, vid, _ = cooking
    config_path = bot_folder / "bot.yaml"
    config_path.write_text(config_path.read_text().replace("orchestration: false", "orchestration: true"))
    chat.metadata = {**chat.metadata, "always_skills": ["orchestration"]}
    chat.save()
    other_owner = django_user_model.objects.create_user("different-owner")
    ConversationSession.objects.create(
        user=other_owner, bot_name="kitchen", metadata={"bot_role": "thread", "title": "OTHER-OWNER-SECRET"}
    )
    sibling = ConversationSession.objects.create(
        user=chat.user, bot_name="kitchen", metadata={"bot_role": "thread", "title": "ON-DEMAND-THREAD-TITLE"}
    )
    from django_ergo.conversation.models import StructuredCall

    StructuredCall.objects.create(
        session=sibling,
        kind="chat_reply",
        status="awaiting_approval",
        metadata={
            "pending_approvals": [
                {"id": "not-an-approval", "name": "order", "input": {"private": "APPROVAL-ARGUMENTS-NOT-SHARED"}}
            ]
        },
    )
    client = use_bots(
        tool_call("ergo_bot_list", {}),
        tool_call("ergo_thread_list", {}),
        tool_call("ergo_worker_list", {"active_only": True}),
        say("Only authorized stored context was checked."),
    )
    settings.REALTIME_VOICE_DELEGATION_BUDGET = REALTIME_VOICE_DELEGATION_BUDGET
    original = dict(REALTIME_VOICE_DELEGATION_BUDGET)
    message = run_delegation(vid)
    assert message.status == "answered"
    assert "ON-DEMAND-THREAD-TITLE" not in str(client.calls[0])
    assert "ON-DEMAND-THREAD-TITLE" in str(client.calls[-1])
    assert "pending_approval_tools" in str(client.calls[-1])
    assert "APPROVAL-ARGUMENTS-NOT-SHARED" not in str(client.calls)
    call = chat.structured_calls.get()
    assert call.metadata["voice_session_id"] == vid
    assert call.metadata["voice_event_id"] == "frying-time"
    assert call.metadata["context_policy"] == "current_chat_only"
    assert settings.REALTIME_VOICE_DELEGATION_BUDGET == original
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        *REALTIME_VOICE_DELEGATION_BUDGET["allowed_tools"],
        "send_reply",
    }
    assert "OTHER-OWNER-SECRET" not in str(client.calls)
    assert all(options["max_retries"] == 0 for options in client.options)
    assert len(client.calls) == REALTIME_VOICE_DELEGATION_BUDGET["max_turns"]
    assert not any(
        tool["name"] in {"ergo_thread_send", "ergo_thread_forward", "ergo_worker_cancel"}
        for call in client.calls
        for tool in call["tools"]
    )


@pytest.mark.django_db(transaction=True)
def test_ordinary_non_voice_budgeted_turn_keeps_its_normal_orchestration_context(cooking, use_bots, bot_folder):  # noqa: F811
    from django_ergo.conversation.budget import attach

    chat, _, _ = cooking
    path = bot_folder / "bot.yaml"
    path.write_text(path.read_text().replace("orchestration: false", "orchestration: true"))
    chat.metadata = {**chat.metadata, "always_skills": ["orchestration"]}
    chat.save()
    ConversationSession.objects.create(
        user=chat.user, bot_name="kitchen", metadata={"bot_role": "thread", "title": "Ordinary overview marker"}
    )
    client = use_bots(say("Ordinary budgeted reply."))
    message = messaging.send(
        None, chat, "An ordinary request.", metadata=attach({}, realtime_voice.delegation_budget())
    )
    messaging.deliver(str(message.id), registry=webhooks.get_registry())
    message.refresh_from_db()
    assert message.status == "answered"
    assert "## Bots and threads" in str(client.calls[0])
    assert "Ordinary overview marker" in str(client.calls[0])
    assert "context_policy" not in chat.structured_calls.get().metadata


@pytest.mark.django_db(transaction=True)
def test_launch_context_is_bounded_without_running_paid_compaction(cooking):
    from asgiref.sync import async_to_sync
    from django_ergo.conversation.messages import add_message
    from django_ergo.conversation.models import ConversationCompaction

    chat, vid, _ = cooking
    chat.system_prompt = "Owning instructions. " * 1000
    chat.metadata = {**chat.metadata, "custom_instructions": True}
    chat.save()
    async_to_sync(add_message)(chat, "user", [{"block_type": "text", "text": "Recent task. " * 4000}])
    ConversationCompaction.objects.create(
        session=chat,
        mode="context_size",
        from_sequence=0,
        upto_sequence=0,
        summary="Saved summary. " * 2000,
    )
    briefing = realtime_voice.bridge_state(vid)["briefing"]
    assert len(briefing["instructions"]) == 8000
    assert len(briefing["context"]) <= realtime_voice.BRIEFING_CONTEXT_CHARS
    assert len(briefing["voice_event_ids"]) <= 100
    assert chat.compactions.count() == 1  # reads the durable summary; no new compaction/model call


@pytest.mark.django_db(transaction=True)
def test_polling_and_delegation_do_not_rebuild_launch_thread_context(cooking, monkeypatch):
    from unittest.mock import Mock

    from django_ergo.conversation.context import TextContextSource

    _, vid, _ = cooking
    bot = webhooks.get_registry().get("kitchen")
    original = bot.context_builder
    callback = Mock(return_value="Authorized launch-thread callback context.")

    def context_builder(*args, **kwargs):
        builder = original(*args, **kwargs)
        builder.add(TextContextSource("Local callback", callback))
        return builder

    monkeypatch.setattr(bot, "context_builder", context_builder)
    with patch.object(bot, "make_engine", wraps=bot.make_engine) as make_engine:
        startup = realtime_voice.bridge_state(vid, include_briefing=True)
        assert "Authorized launch-thread callback context." in startup["briefing"]["context"]
        assert callback.call_count == 1
        assert make_engine.call_count == 1
        for _ in range(5):
            snapshot = realtime_voice.bridge_state(vid, include_briefing=False)
            assert snapshot["briefing"]["context"] == ""
            assert snapshot["briefing"]["delegation_available"] is True
            realtime_voice.session_out(RealtimeVoiceSession.objects.get(pk=vid))
        assert callback.call_count == 1
        assert make_engine.call_count == 1
        realtime_voice.delegate(vid, "polling-regression", "Read this thread's history.")
        assert callback.call_count == 1
        assert make_engine.call_count == 2  # required delegation preflight, not a briefing rebuild
        resumed = realtime_voice.bridge_state(vid, include_briefing=True)
        assert "Authorized launch-thread callback context." in resumed["briefing"]["context"]
        assert callback.call_count == 2
        assert resumed["briefing"]["delegation_available"] is False  # one request already used


@pytest.mark.django_db(transaction=True)
def test_web_without_budget_uses_bridge_validated_status_not_web_process_settings(cooking, settings):
    _, vid, _ = cooking
    settings.REALTIME_VOICE_DELEGATION_BUDGET = None
    before = realtime_voice.session_out(RealtimeVoiceSession.objects.get(pk=vid))["briefing"]
    assert before["delegation_available"] is None
    assert "Bridge has not validated delegation for this session" in before["restriction"]
    assert "no enforced request budget" not in before["restriction"]

    # The bridge process has its reviewed profile; only it validates and publishes capability.
    settings.REALTIME_VOICE_DELEGATION_BUDGET = BUDGET
    bridge = realtime_voice.bridge_state(vid, include_briefing=True)["briefing"]
    assert bridge["delegation_available"] is True
    settings.REALTIME_VOICE_DELEGATION_BUDGET = None
    after = realtime_voice.session_out(RealtimeVoiceSession.objects.get(pk=vid))["briefing"]
    assert after["delegation_available"] is True
    assert after["restriction"] == bridge["restriction"]
    assert after["context"] == ""
    assert "instructions" not in after
