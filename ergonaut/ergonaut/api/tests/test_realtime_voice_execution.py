"""Default-off normal-thread bridge: real tools/queue/approvals, scripted provider only."""

# ruff: noqa: F811 -- imported pytest fixtures are intentional.

import json
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django_ergo.bots import messaging, orchestrator, webhooks
from django_ergo.conversation.messages import add_message
from django_ergo.conversation.models import ConversationSession, ThreadMessage

from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice_budget import (  # noqa: F401
    bot_folder,
    cook,
    held_messages,
    use_bots,
)
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.realtime_voice_outbox import claim_announcement, thread_update_event


@pytest.fixture
def normal_voice(cook, bot_folder, use_bots, settings, monkeypatch):  # noqa: F811
    config = bot_folder / "bot.yaml"
    config.write_text(config.read_text().replace("orchestration: false", "orchestration: true"))
    use_bots(say("Not called during authorization."))
    settings.REALTIME_VOICE_USERS = {cook.get_username()}
    chat = ConversationSession.objects.create(
        user=cook,
        bot_name="kitchen",
        metadata={
            "bot_role": "main",
            "always_skills": ["orchestration"],
        },
    )
    target = ConversationSession.objects.create(
        user=cook,
        bot_name="kitchen",
        metadata={
            "bot_role": "thread",
            "title": "Existing disposable worker thread",
        },
    )
    async_to_sync(add_message)(chat, "user", [{"block_type": "text", "text": "Current task code is cobalt-spoon."}])
    settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS = {
        str(chat.pk): {
            "owner": cook.get_username(),
            "bot": "kitchen",
            "normal_task_costs_approved": True,
            "approval_reference": "disposable-offline-policy-only",
        }
    }
    result = realtime_voice.create_session(SimpleNamespace(auth=cook), realtime_voice.CreateIn(chat_id=str(chat.pk)))
    voice = RealtimeVoiceSession.objects.get(pk=result["id"])
    # No API-only limits capability: do not substitute a real CLI/provider for the scripted transport.
    from django_ergo.bots.runtime import Bot

    original = Bot.make_engine

    def unsupported_limits(bot, *args, **kwargs):
        engine = original(bot, *args, **kwargs)
        engine.supports_request_limits = False
        return engine

    monkeypatch.setattr(Bot, "make_engine", unsupported_limits)
    return chat, target, voice


def deliver(message):
    messaging.deliver(str(message.pk), registry=webhooks.get_registry())
    message.refresh_from_db()


def post(client, url, data):
    return client.post(url, json.dumps(data), content_type="application/json")


@pytest.mark.django_db(transaction=True)
def test_normal_tools_dispatch_once_while_voice_continues_and_results_restore(
    normal_voice, use_bots, django_user_model
):  # noqa: F811
    chat, target, voice = normal_voice
    outsider = django_user_model.objects.create_user("outside-voice-owner")
    private = ConversationSession.objects.create(
        user=outsider,
        bot_name="kitchen",
        metadata={
            "bot_role": "thread",
            "title": "PRIVATE-OTHER-OWNER",
        },
    )
    write = tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "Check the disposable task code."})
    conflict = tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "Different unauthorized replay."})
    conflict.content[0].id = write.content[0].id
    scripted = use_bots(
        tool_call("ergo_thread_list", {}),
        tool_call("ergo_thread_send", {"thread": str(private.pk), "message": "Must be refused."}),
        write,
        write,  # repeated provider invocation, not a second authorized external write
        conflict,
        tool_call("ergo_worker_list", {"active_only": True}),
        say("Submitted to the existing thread; awaiting its result."),
        say("The disposable worker completed cobalt-spoon."),
        say("The existing thread returned its result."),
    )
    briefing = realtime_voice.startup_briefing(voice)
    assert briefing["execution_mode"] == "normal_thread" and briefing["delegation_available"]
    root = realtime_voice.delegate(voice.pk, "voice-request", "Ask the existing thread for its task code.")
    resumed = realtime_voice.startup_briefing(RealtimeVoiceSession.objects.get(pk=voice.pk))
    assert resumed["execution_mode"] == "normal_thread" and not resumed["delegation_available"]
    assert root.recipient_session_id == chat.pk and root.sender_session_id is None
    assert root.metadata["message_author"]["ref"] == str(chat.user_id)
    assert "turn_budget" not in root.metadata
    assert root.metadata["tool_execution_policy"] == "at_most_once"
    deliver(root)
    assert root.status == "answered"
    assert (
        realtime_voice.delegate(voice.pk, "voice-request", "Ask the existing thread for its task code.").pk == root.pk
    )
    child = ThreadMessage.objects.get(sender_session=chat, recipient_session=target)
    assert not ThreadMessage.objects.filter(recipient_session=private).exists()
    assert child.metadata["voice_event_id"] == "voice-request"
    assert child.metadata["parent_thread_message"] == str(root.pk)
    assert "already used with different arguments" in str(scripted.calls)
    assert "cobalt-spoon" in str(scripted.calls[0])
    assert "PRIVATE-OTHER-OWNER" not in str(scripted.calls)
    assert all(options["max_retries"] == 0 for options in scripted.options)
    # A real finalized conversational turn can be persisted while durable task work stays queued.
    realtime_voice.record_event(voice.pk, "still-talking", "user", "Keep talking while that task runs.")
    pending = realtime_voice.bridge_state(voice.pk, include_briefing=False)
    assert pending["status"] == "active" and pending["thread_updates"][0]["status"] == "queued"
    assert child.pk == ThreadMessage.objects.get(sender_session=chat, recipient_session=target).pk
    # End voice is not task cancellation. The normal child turn and normal reply still execute.
    realtime_voice.end_voice_session(voice.pk)
    deliver(child)
    incoming = ThreadMessage.objects.get(in_reply_to=child)
    assert incoming.metadata["voice_session_id"] == str(voice.pk)
    deliver(incoming)
    restored = realtime_voice.bridge_state(voice.pk, include_briefing=False)
    assert restored["status"] == "ended"
    completed = next(row for row in restored["thread_updates"] if row["update_id"].startswith(f"thread:{child.pk}:"))
    assert completed["status"] == "answered" and "cobalt-spoon" in completed["result"]
    observed = realtime_voice.session_out(voice)
    assert any("cobalt-spoon" in event["text"] and event["kind"] == "result" for event in observed["events"])
    assert len(scripted.calls) == 9  # polling/reconnect did not invoke a model or redispatch a write
    receipt = thread_update_event(voice.pk, completed)
    assert claim_announcement(receipt) and not claim_announcement(thread_update_event(voice.pk, completed))
    with pytest.raises(ValueError, match="one background request"):
        realtime_voice.delegate(voice.pk, "second-request", "Another task")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("decision", ["approve", "deny", "uncertain"])
@pytest.mark.parametrize("unlimited", [False, True])
def test_real_thread_send_preserves_normal_owner_approval_and_uncertain_write_claim(
    normal_voice,
    use_bots,
    client,
    monkeypatch,
    settings,
    unlimited,
    decision,  # noqa: F811
):
    chat, target, voice = normal_voice
    if unlimited:
        from ergonaut.realtime_voice_execution import initial_state

        settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS[str(chat.pk)].update(
            max_requests=3,
            followup_costs_approved=True,
            followup_approval_reference="offline-three",
            unlimited_intentional_requests=True,
            unlimited_approval_reference="offline-unlimited",
        )
        voice.state = {**voice.state, **initial_state(chat, chat.user)}
        voice.save(update_fields=["state"])
    # Apply the normal toolkit's approval policy to the real registered orchestration handler.
    monkeypatch.setattr(orchestrator.ergo_thread_send.__bot_tool__, "requires_approval", True)
    write = tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "Owner-approved disposable action."})
    use_bots(write, say("The normal approval decision was applied."))
    root = realtime_voice.delegate(voice.pk, "approval-request", "Send the disposable task after normal approval.")
    deliver(root)
    assert root.status == "waiting" and not ThreadMessage.objects.filter(sender_session=chat).exists()
    waiting = realtime_voice.bridge_state(voice.pk, include_briefing=False)["delegations"][0]
    event = thread_update_event(voice.pk, waiting)
    assert event.kind == "task_status" and "voice has not approved" in event.text
    call = chat.structured_calls.get()
    ids = [item["id"] for item in call.metadata["pending_approvals"]]
    endpoint = f"/api/sessions/{chat.pk}/approvals"
    assert post(client, endpoint, {"approve": True, "approval_ids": ["stale"]}).status_code == 409
    if decision == "uncertain":
        import hashlib

        signature = hashlib.sha256(
            json.dumps(
                [
                    "ergo_thread_send",
                    write.content[0].input,
                ],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        call.metadata["tool_invocations"] = {ids[0]: {"signature": signature, "status": "claimed"}}
        call.save(update_fields=["metadata"])
    response = post(client, endpoint, {"approve": decision != "deny", "approval_ids": ids})
    assert response.status_code == 200
    root.refresh_from_db()
    assert root.status == "answered"
    assert ThreadMessage.objects.filter(sender_session=chat, recipient_session=target).count() == (
        decision == "approve"
    )
    assert post(client, endpoint, {"approve": True, "approval_ids": ids}).status_code == 409
    voice.refresh_from_db()
    assert voice.status == "active"  # neither approval nor its result ends the conversation


@pytest.mark.django_db(transaction=True)
def test_exact_thread_grant_is_default_off_revocable_and_not_inherited(normal_voice, settings):
    chat, _, voice = normal_voice
    settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS = {}
    with pytest.raises(ValueError, match="revoked"):
        realtime_voice.delegate(voice.pk, "not-authorized", "Do not run.")
    assert not voice.delegations.exists()
    # Copying saved conversational state cannot mint a grant under a disabled policy.
    chat.metadata = {
        **chat.metadata,
        "voice_state": {"normal_thread_authorization": voice.state["normal_thread_authorization"]},
    }
    chat.save(update_fields=["metadata"])
    new = realtime_voice.create_session(SimpleNamespace(auth=chat.user), realtime_voice.CreateIn(chat_id=str(chat.pk)))
    assert "normal_thread_authorization" not in new["state"]


@pytest.mark.django_db(transaction=True)
def test_normal_task_transient_failure_is_not_retried_or_reported_answered(normal_voice, use_bots):  # noqa: F811
    from django_ergo.conversation.engine import automatic_request_retries

    _, target, voice = normal_voice
    client = use_bots(say("Must not run after the first failed request."))
    attempts = []

    class APIConnectionError(Exception):
        pass

    async def unavailable(**kwargs):
        attempts.append(kwargs)
        raise APIConnectionError("Disposable transport failure")

    client.create = unavailable
    root = realtime_voice.delegate(voice.pk, "no-retry", "Run the disposable task.")
    deliver(root)
    assert len(attempts) == 1
    assert root.status == "failed" and not root.reply_text
    assert not ThreadMessage.objects.filter(recipient_session=target).exists()
    task = realtime_voice.bridge_state(voice.pk, include_briefing=False)["delegations"][0]
    assert thread_update_event(voice.pk, task).text == "The background request failed."
    assert automatic_request_retries.get() is True  # the next unrelated turn keeps its existing policy


@pytest.mark.django_db(transaction=True)
def test_concurrent_normal_delegation_claims_one_durable_request(normal_voice):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    chat, _, voice = normal_voice
    barrier = Barrier(2)

    def submit(_):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return realtime_voice.delegate(voice.pk, "same-provider-id", "One disposable task.").pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(submit, range(2))
    assert first == second
    assert ThreadMessage.objects.filter(recipient_session=chat).count() == 1
    assert voice.delegations.get().message_id == first


@pytest.mark.django_db(transaction=True)
def test_authorized_normal_request_cannot_start_after_original_voice_deadline(normal_voice):
    from datetime import timedelta

    from django.utils import timezone

    _, _, voice = normal_voice
    voice.bridge_started_at = timezone.now() - timedelta(seconds=realtime_voice.BRIDGE_JOB_SECONDS + 1)
    voice.save(update_fields=["bridge_started_at"])
    with pytest.raises(ValueError, match="deadline expired"):
        realtime_voice.delegate(voice.pk, "expired-normal-request", "Do not run after the voice cap.")
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_startup_has_bounded_exact_thread_work_and_current_call_observations(normal_voice):
    from django_ergo.conversation.models import Worker

    chat, target, voice = normal_voice
    Worker.objects.create(
        session=chat,
        bot_name=chat.bot_name,
        title="Current cobalt-spoon release",
        function="offline:worker",
        status="running",
        progress="Reviewing synthetic result",
    )
    Worker.objects.create(
        session=target,
        bot_name=target.bot_name,
        title="UNRELATED PRIVATE WORK",
        function="offline:worker",
        status="running",
    )
    realtime_voice.record_event(
        voice.pk,
        "bridge-input:transcribed:offline",
        "bridge_input",
        "Nonempty input transcription observed from the voice provider.",
    )
    before = chat.messages.count()
    snapshot = realtime_voice.startup_briefing(voice)
    assert "Current cobalt-spoon release" in snapshot["context"]
    assert "Reviewing synthetic result" in snapshot["context"]
    assert "UNRELATED PRIVATE WORK" not in snapshot["context"]
    assert snapshot["observation"]["current_session"]["input_transcribed"] is True
    assert snapshot["observation"]["current_session"]["physical_hearing"] == "unknown"
    assert snapshot["observation"]["observed_at"]
    assert chat.messages.count() == before


@pytest.mark.django_db(transaction=True)
def test_reviewed_followups_are_finite_sequential_idempotent_and_do_not_widen_existing_sessions(
    normal_voice, settings, held_messages
):
    from ergonaut.realtime_voice_execution import request_capacity

    chat, _, original = normal_voice
    policy = settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS[str(chat.pk)]
    policy["max_requests"] = 3
    with pytest.raises(ValueError, match="cost approval"):
        realtime_voice.create_session(SimpleNamespace(auth=chat.user), realtime_voice.CreateIn(chat_id=str(chat.pk)))
    policy.update(followup_costs_approved=True, followup_approval_reference="offline-explicit-three")
    with pytest.raises(ValueError, match="revoked"):
        realtime_voice.delegate(original.pk, "must-not-widen", "Old logical sessions cannot inherit increased costs.")
    fresh = realtime_voice.create_session(
        SimpleNamespace(auth=chat.user), realtime_voice.CreateIn(chat_id=str(chat.pk))
    )
    voice = RealtimeVoiceSession.objects.get(pk=fresh["id"])
    assert realtime_voice.claim_bridge(voice.pk, "a" * 32)
    voice.refresh_from_db()
    deadline_origin = voice.bridge_started_at
    for index in range(3):
        event_id = f"explicit-{index}"
        assert realtime_voice.begin_pending(voice.pk, event_id, {})
        receipt = realtime_voice.resolve_pending(voice.pk, event_id, f"Explicit user follow-up {index}")
        message = ThreadMessage.objects.get(pk=receipt["message_id"])
        assert receipt["status"] == "queued"
        assert realtime_voice.delegate(voice.pk, event_id, f"Explicit user follow-up {index}").pk == message.pk
        assert not realtime_voice.begin_pending(voice.pk, f"concurrent-{index}", {})
        with pytest.raises(ValueError):
            realtime_voice.delegate(voice.pk, f"bypass-{index}", "Must not execute concurrently.")
        for _ in range(2):
            realtime_voice.session_out(voice)  # Reading status never dispatches or consumes execution.
        assert len(held_messages) == index + 1
        message.status, message.reply_text = "answered", f"Verified result {index}"
        message.save(update_fields=["status", "reply_text", "updated_at"])
    voice.refresh_from_db()
    assert request_capacity(voice)["remaining"] == 0
    assert voice.bridge_started_at == deadline_origin
    assert not realtime_voice.begin_pending(voice.pk, "fourth", {})
    with pytest.raises(ValueError, match="cap reached"):
        realtime_voice.delegate(voice.pk, "fourth", "Must not exceed the reviewed finite cap.")
    assert len(held_messages) == 3


@pytest.mark.django_db(transaction=True)
def test_expired_collection_consumes_slot_and_cannot_replay_as_execution(normal_voice):
    _, _, voice = normal_voice
    assert realtime_voice.begin_pending(voice.pk, "expired-attempt", {})
    realtime_voice.close_pending(voice.pk, "expired-attempt", "expired")
    with pytest.raises(ValueError, match="consumed"):
        realtime_voice.delegate(voice.pk, "expired-attempt", "Do not replay an expired attempt.")
    assert not realtime_voice.begin_pending(voice.pk, "new-attempt", {})
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_unlimited_policy_upgrades_only_matching_existing_three_request_grants(normal_voice, settings):
    from ergonaut.realtime_voice_execution import initial_state, request_capacity

    chat, _, old_one = normal_voice
    policy = settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS[str(chat.pk)]
    policy.update(max_requests=3, followup_costs_approved=True, followup_approval_reference="offline-three")
    voice = RealtimeVoiceSession.objects.create(user=chat.user, chat=chat, state=initial_state(chat, chat.user))
    policy["unlimited_intentional_requests"] = True
    assert request_capacity(voice)["limit"] == 0
    policy["unlimited_approval_reference"] = "offline-unlimited"
    assert request_capacity(voice)["limit"] is None
    assert request_capacity(old_one)["limit"] == 0
    policy["owner"] = "someone-else"
    assert request_capacity(voice)["limit"] == 0
