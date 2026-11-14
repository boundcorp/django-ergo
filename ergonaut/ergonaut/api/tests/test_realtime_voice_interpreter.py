"""Offline native-interpreter behavior through durable tasks and the pinned SDK parser.

No SDK session constructor, sockets, generated provider responses, or mocked task results.
The scripted bot transport is the same one used by the normal-thread execution tests.
"""

# ruff: noqa: F811 -- imported pytest fixtures are intentional.

import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import close_old_connections
from django.utils import timezone
from django_ergo.bots import messaging, orchestrator, webhooks
from django_ergo.conversation.models import ConversationSession, ThreadMessage

pytest.importorskip("livekit.plugins.openai.realtime.gpt_live_model")

from ergonaut import realtime_voice_bridge as bridge
from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice_bridge import (  # noqa: F401
    bind,
    drain,
    offline_key,
    offline_session,
    settle,
)
from ergonaut.api.tests.test_realtime_voice_budget import bot_folder, cook, held_messages, use_bots  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_execution import deliver, normal_voice, post  # noqa: F401
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.realtime_voice_interpreter import process_native_event


def arguments(
    intent_id="intent-A", kind="new_request", target=None, text="Check the disposable task code.", clarification=""
):
    return {
        "kind": kind,
        "intent_id": intent_id,
        "target_intent_id": target,
        "request_text": text,
        "clarification": clarification,
    }


def native_events(args=None, *, identity="A", delegation_id=None):
    """The actual GPT-Live response.event envelope, not a stand-in function parser."""
    delegation_id = delegation_id or f"delegation-{identity}"

    def envelope(event, ordinal):
        return {
            "type": "response.event",
            "event_id": f"wire-{identity}-{ordinal}",
            "offset_ms": 1000 + ordinal,
            "delegation_id": delegation_id,
            "event": event,
        }

    return [
        {
            "type": "session.delegation.created",
            "event_id": f"wire-{identity}-0",
            "offset_ms": 1000,
            "delegation": {
                "id": delegation_id,
                "type": "delegation",
                "target": "responses",
                "response_id": f"response-{identity}",
            },
        },
        envelope({"type": "response.created", "response": {"id": f"response-{identity}"}}, 1),
        envelope(
            {
                "type": "response.output_item.done",
                "item": {
                    "id": f"item-{identity}",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": f"call-{identity}",
                    "name": "ergo_voice_intent",
                    "arguments": json.dumps(arguments() if args is None else args, ensure_ascii=False),
                },
            },
            2,
        ),
        envelope({"type": "response.completed", "response": {"id": f"response-{identity}", "status": "completed"}}, 3),
    ]


def consume(voice, events, *, claim_id=None):
    result = None
    for event in events:
        result = process_native_event(voice.pk, event, claim_id=claim_id)
    return result


def receipt(result, call_id="call-A"):
    return result["receipts"][call_id]


def tasks(voice):
    return list(voice.delegations.select_related("message").order_by("created_at", "id"))


def native_sdk(assistant):
    sdk = offline_session(assistant)
    sdk._backend_running_responses = {}
    sdk._backend_open_calls = set()
    sdk._backend_response_pending = False
    return sdk


def wire(assistant, sdk, event):
    # The production receive loop emits the raw callback BEFORE SDK parsing.
    # _handle_event itself is the real installed 1.8.11 parser; it emits FunctionCall.
    assistant.on_protocol_event(copy.deepcopy(event))
    sdk._handle_event(copy.deepcopy(event))


@pytest.fixture
def interpreter_voice(normal_voice, settings):
    chat, target, voice = normal_voice
    policy = settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS[str(chat.pk)]
    policy.update(
        max_requests=3,
        followup_costs_approved=True,
        followup_approval_reference="disposable-native-followups",
    )
    from ergonaut.realtime_voice_execution import initial_state

    voice.state = {**voice.state, **initial_state(chat, chat.user)}
    voice.save(update_fields=["state"])
    return chat, target, voice


@pytest.mark.django_db(transaction=True)
def test_completed_native_args_not_late_display_fragments_start_the_normal_task(
    interpreter_voice, held_messages, use_bots
):
    chat, _, voice = interpreter_voice
    scripted = use_bots(say("Task zero completed."), say("The disposable task returned cobalt-spoon."))
    actual_calls = []

    async def scenario():
        assistant = bridge.Assistant(str(voice.pk), await sync_to_async(realtime_voice.bridge_state)(voice.pk), None)
        sdk = native_sdk(assistant)
        sdk.on("function_call", actual_calls.append)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            first = native_events(arguments("intent-zero", text="Start offline task zero."), identity="zero")
            for event in first:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            assert len(held_messages) == 1
            await sync_to_async(messaging.deliver)(held_messages[0], registry=webhooks.get_registry())
            await assistant.deliver_results(await sync_to_async(realtime_voice.bridge_state)(voice.pk))
            drain(sdk)
            wire(
                assistant,
                sdk,
                {
                    "type": "session.input_transcript.delta",
                    "delta": "STALE EARLIER TASK. ",
                    "start_ms": 0,
                    "end_ms": 100,
                },
            )
            sdk._end_speech("user")
            wire(
                assistant,
                sdk,
                {
                    "type": "session.input_transcript.delta",
                    "delta": "An unrelated display fragment.",
                    "start_ms": 2500,
                    "end_ms": 3000,
                },
            )
            sdk._end_speech("user")
            events = native_events(arguments(text="Look up the current disposable task code, not the stale request."))
            for event in events[:-1]:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            assert len(held_messages) == 1  # SDK FunctionCall is not response-completed admission.
            assert len(actual_calls) == 2
            tool_result = asyncio.create_task(
                assistant.ergo_voice_intent(
                    SimpleNamespace(function_call=actual_calls[-1]),
                    **json.loads(actual_calls[-1].arguments),
                )
            )
            await asyncio.sleep(0)
            assert not tool_result.done()
            wire(assistant, sdk, events[-1])
            await assistant.flush_native_events()
            accepted = json.loads(await tool_result)
            assert accepted["status"] == "submitted"
            assert accepted["intent_id"] == "intent-A"
            await settle(assistant)
            assert len(held_messages) == 2
            await sync_to_async(messaging.deliver)(held_messages[-1], registry=webhooks.get_registry())
            await assistant.deliver_results(await sync_to_async(realtime_voice.bridge_state)(voice.pk))
            outgoing = drain(sdk)
            assert any("cobalt-spoon" in row.get("content", "") for row in outgoing)

    async_to_sync(scenario)()
    _, task = tasks(voice)
    assert task.text == "Look up the current disposable task code, not the stale request."
    assert task.message.recipient_session_id == chat.pk
    assert task.message.status == "answered" and "cobalt-spoon" in task.message.reply_text
    assert len(scripted.calls) == 2
    assert json.loads(actual_calls[-1].arguments)["request_text"] == task.text


@pytest.mark.django_db(transaction=True)
def test_raw_completion_is_durable_before_any_task_dispatch(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    events = native_events()
    original_send = messaging.send

    def inspect_then_send(*args, **kwargs):
        saved = RealtimeVoiceSession.objects.get(pk=voice.pk).state["_voice_native"]["raw"]
        assert any(row["payload"] == events[-1] for row in saved)
        return original_send(*args, **kwargs)

    with patch.object(messaging, "send", side_effect=inspect_then_send):
        result = consume(voice, events)
    assert receipt(result)["status"] == "submitted"
    assert len(held_messages) == 1
    voice.refresh_from_db()
    assert [row["payload"] for row in voice.state["_voice_native"]["raw"]] == events


@pytest.mark.django_db(transaction=True)
def test_exact_wire_replay_and_restarted_response_return_the_original_durable_task(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    first = consume(voice, native_events())
    first_message = receipt(first)["message_id"]
    close_old_connections()
    restored = RealtimeVoiceSession.objects.get(pk=voice.pk)
    assert receipt(consume(restored, native_events()))["message_id"] == first_message
    # A new parser/response has no SDK-local call dedup history.
    restarted = consume(restored, native_events(identity="restart", delegation_id="delegation-A")[1:])
    assert receipt(restarted, "call-restart")["message_id"] == first_message
    assert len(held_messages) == 1 and len(tasks(voice)) == 1


@pytest.mark.django_db(transaction=True)
def test_fresh_sdk_reconnect_cannot_replay_a_completed_intent(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice

    async def run_one(events):
        assistant = bridge.Assistant(str(voice.pk), await sync_to_async(realtime_voice.bridge_state)(voice.pk), None)
        sdk = native_sdk(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            for event in events:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            await settle(assistant)

    async_to_sync(run_one)(native_events())
    async_to_sync(run_one)(native_events())
    async_to_sync(run_one)(native_events(identity="restart", delegation_id="delegation-A")[1:])
    assert len(held_messages) == 1 and len(tasks(voice)) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("reuse", ["wire_item", "intent_id"])
def test_conflicting_arguments_never_replace_or_duplicate_accepted_work(interpreter_voice, held_messages, reuse):
    _, _, voice = interpreter_voice
    original = arguments(text="Only the original authorized task.")
    consume(voice, native_events(original))
    conflicting = arguments(text="Conflicting replacement must not run.")
    events = native_events(
        conflicting, identity="A" if reuse == "wire_item" else "conflict", delegation_id="delegation-A"
    )
    consume(voice, events if reuse == "wire_item" else events[1:])
    [task] = tasks(voice)
    assert task.text == original["request_text"]
    assert len(held_messages) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "fault",
    [
        "orphan",
        "missing_delegation",
        "client_delegation",
        "missing_created_id",
        "partial_item",
        "missing_item_id",
        "missing_call_id",
        "wrong_tool",
        "missing_arguments",
        "invalid_json",
        "extra_argument",
        "invalid_kind",
        "missing_completion",
        "failed",
        "incomplete",
        "mismatched_completion",
    ],
)
def test_incomplete_or_unbound_native_lifecycle_cannot_admit_work(interpreter_voice, held_messages, fault):
    _, _, voice = interpreter_voice
    events = native_events()
    item = events[2]["event"]["item"]
    if fault == "orphan":
        events.pop(1)
    elif fault == "missing_delegation":
        events.pop(0)
    elif fault == "client_delegation":
        events[0]["delegation"]["target"] = "client"
    elif fault == "missing_created_id":
        events[1]["event"]["response"] = {}
    elif fault == "partial_item":
        item["status"] = "in_progress"
    elif fault == "missing_item_id":
        item.pop("id")
    elif fault == "missing_call_id":
        item.pop("call_id")
    elif fault == "wrong_tool":
        item["name"] = "ergo_thread_send"
    elif fault == "missing_arguments":
        item.pop("arguments")
    elif fault == "invalid_json":
        item["arguments"] = '{"kind":'
    elif fault == "extra_argument":
        item["arguments"] = json.dumps({**arguments(), "approved": True})
    elif fault == "invalid_kind":
        item["arguments"] = json.dumps({**arguments(), "kind": "approve"})
    elif fault == "missing_completion":
        events.pop()
    elif fault in ("failed", "incomplete"):
        events[-1]["event"]["type"] = f"response.{fault}"
        events[-1]["event_id"] = f"wire-A-{fault}"
        # A stray later completion must not revive a failed response.
        events.append(copy.deepcopy(native_events()[-1]))
    elif fault == "mismatched_completion":
        events[-1]["event"]["response"]["id"] = "not-the-created-response"
    consume(voice, events)
    assert held_messages == []
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_conflicting_item_before_completion_invalidates_instead_of_selecting_a_payload(
    interpreter_voice, held_messages
):
    _, _, voice = interpreter_voice
    delegation, created, item, completed = native_events()
    conflict = copy.deepcopy(item)
    conflict["event_id"] = "wire-A-conflicting-item"
    conflict["event"]["item"]["arguments"] = json.dumps(arguments(text="A conflicting payload."))
    consume(voice, [delegation, created, item, conflict, completed])
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_display_and_client_delegation_are_never_native_task_admission(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    consume(
        voice,
        [
            {"type": "session.input_transcript.delta", "delta": "Please run a task.", "start_ms": 0, "end_ms": 100},
            {"type": "session.delegation.created", "delegation": {"id": "legacy-client", "target": "client"}},
        ],
    )
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_clarification_then_explicit_new_request_has_one_authoritative_task(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    ask = arguments("unclear-A", "clarify", text="Check it.", clarification="Which disposable task should be checked?")
    result = consume(voice, native_events(ask))
    assert receipt(result)["status"] == "clarification"
    assert receipt(result)["clarification"]
    assert held_messages == []
    intent = next(row for row in result["context"]["intents"] if row["intent_id"] == "unclear-A")
    assert intent["status"] == "awaiting_clarification"
    result = consume(voice, native_events(arguments("clear-B", text="Check the cobalt-spoon task."), identity="B"))
    assert receipt(result, "call-B")["status"] == "submitted"
    assert len(held_messages) == 1
    assert tasks(voice)[0].text == "Check the cobalt-spoon task."


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["status", "continue", "correct", "cancel"])
def test_non_new_intents_without_authoritative_targets_only_clarify(interpreter_voice, held_messages, kind):
    _, _, voice = interpreter_voice
    result = consume(voice, native_events(arguments("followup", kind, "unknown-intent", text="Do not start new work.")))
    assert receipt(result)["status"] == "clarification"
    assert receipt(result)["clarification"]
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["status", "continue", "correct", "cancel"])
def test_existing_intent_followups_never_launch_or_rewrite_queued_work(interpreter_voice, held_messages, kind):
    _, _, voice = interpreter_voice
    consume(voice, native_events(arguments(text="Preserve this queued task.")))
    [before] = tasks(voice)
    original_text = before.message.text
    result = consume(
        voice, native_events(arguments("followup", kind, "intent-A", text="Replace or repeat the task."), identity="B")
    )
    observed = receipt(result, "call-B")
    assert observed["status"] == ("rejected" if kind in ("correct", "cancel") else "observed")
    if kind in ("correct", "cancel"):
        assert observed["clarification"]
    [after] = tasks(voice)
    assert after.message_id == before.message_id
    assert after.text == "Preserve this queued task." and after.message.text == original_text
    assert after.message.status == "queued"
    assert len(held_messages) == 1
    authoritative = next(row for row in result["context"]["intents"] if row["intent_id"] == "intent-A")
    assert authoritative["status"] == "queued"


@pytest.mark.django_db(transaction=True)
def test_more_than_three_intentional_asks_and_multiple_queued_jobs_are_independent(
    interpreter_voice, held_messages, use_bots
):
    _, _, voice = interpreter_voice
    use_bots(*(say(f"Synthetic task {index} completed.") for index in range(5)))
    for index in range(4):
        result = consume(
            voice,
            native_events(arguments(f"intent-{index}", text=f"Run intentional task {index}."), identity=str(index)),
        )
        assert receipt(result, f"call-{index}")["status"] == "submitted"
    assert len(held_messages) == 4
    assert all(row.message.status == "queued" for row in tasks(voice))
    overflow = consume(
        voice,
        native_events(arguments("overflow", text="This would exceed concurrent task capacity."), identity="overflow"),
    )
    assert receipt(overflow, "call-overflow")["status"] == "rejected"
    assert len(held_messages) == 4
    deliver(tasks(voice)[0].message)
    result = consume(voice, native_events(arguments("intent-4", text="Run intentional task four."), identity="4"))
    assert receipt(result, "call-4")["status"] == "submitted"
    # The normal runner may wake already queued roots again; durable task IDs,
    # not broker wakeup count, establish that no new work was duplicated.
    assert len(set(map(str, held_messages))) == 5
    assert len({row.message_id for row in tasks(voice)}) == 5
    for task in tasks(voice)[1:]:
        deliver(task.message)
    assert all(task.message.status == "answered" for task in tasks(voice))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "boundary",
    ["revoked", "owner_mismatch", "bot_mismatch", "kill_switch", "allowlist", "expired", "ended", "stale_claim"],
)
def test_authorization_and_session_boundaries_fail_closed_at_completion(
    interpreter_voice, held_messages, settings, boundary
):
    chat, _, voice = interpreter_voice
    assert realtime_voice.claim_bridge(voice.pk, "current-claim")
    events = native_events()
    consume(voice, events[:-1], claim_id="current-claim")
    policy = settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS[str(chat.pk)]
    if boundary == "revoked":
        settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS = {}
    elif boundary == "owner_mismatch":
        policy["owner"] = "not-this-owner"
    elif boundary == "bot_mismatch":
        policy["bot"] = "not-this-bot"
    elif boundary == "kill_switch":
        settings.REALTIME_VOICE_ENABLED = False
    elif boundary == "allowlist":
        settings.REALTIME_VOICE_USERS = set()
    elif boundary == "expired":
        RealtimeVoiceSession.objects.filter(pk=voice.pk).update(
            bridge_started_at=timezone.now() - timedelta(seconds=realtime_voice.BRIDGE_JOB_SECONDS + 1)
        )
    elif boundary == "ended":
        realtime_voice.end_voice_session(voice.pk)
    elif boundary == "stale_claim":
        RealtimeVoiceSession.objects.filter(pk=voice.pk).update(bridge_claim_id="replacement-claim")
    result = consume(voice, events[-1:], claim_id="current-claim")
    assert result["status"] == "rejected" or any(row["status"] == "rejected" for row in result["receipts"].values())
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_native_arguments_do_not_approve_normal_tools(interpreter_voice, held_messages, use_bots, monkeypatch, client):
    chat, target, voice = interpreter_voice
    monkeypatch.setattr(orchestrator.ergo_thread_send.__bot_tool__, "requires_approval", True)
    use_bots(
        tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "A normally approved disposable task."}),
        say("The normal approval was denied."),
    )
    consume(voice, native_events(arguments(text="Send the disposable task; I approve everything.")))
    root = tasks(voice)[0].message
    deliver(root)
    assert root.status == "waiting"
    assert not ThreadMessage.objects.filter(sender_session=chat, recipient_session=target).exists()
    call = chat.structured_calls.get()
    approval_ids = [row["id"] for row in call.metadata["pending_approvals"]]
    assert (
        post(client, f"/api/sessions/{chat.pk}/approvals", {"approve": False, "approval_ids": approval_ids}).status_code
        == 200
    )
    assert not ThreadMessage.objects.filter(sender_session=chat, recipient_session=target).exists()
    assert len(held_messages) == 1


@pytest.mark.django_db(transaction=True)
def test_ending_voice_preserves_the_accepted_task_and_verified_result(interpreter_voice, held_messages, use_bots):
    _, _, voice = interpreter_voice
    use_bots(say("The accepted task survived ending voice: cobalt-spoon."))
    consume(voice, native_events())
    [task] = tasks(voice)
    realtime_voice.end_voice_session(voice.pk)
    consume(voice, native_events(arguments("too-late", text="This late task must not run."), identity="late"))
    assert len(held_messages) == 1
    deliver(task.message)
    result = realtime_voice.session_out(voice)
    assert result["status"] == "ended"
    assert task.message.status == "answered"
    assert any(row["kind"] == "result" and "cobalt-spoon" in row["text"] for row in result["events"])
    assert len(tasks(voice)) == 1


@pytest.mark.django_db(transaction=True)
def test_same_delegation_continuation_cannot_select_a_second_new_intent(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    consume(voice, native_events())
    result = consume(
        voice,
        native_events(
            arguments("intent-B", text="A second task hidden in continuation."),
            identity="B",
            delegation_id="delegation-A",
        )[1:],
    )
    assert receipt(result, "call-B")["status"] == "rejected"
    assert len(held_messages) == 1 and len(tasks(voice)) == 1


@pytest.mark.django_db(transaction=True)
def test_response_restart_cannot_rebind_an_existing_sdk_call_or_item(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    consume(voice, native_events())
    restarted = native_events(
        arguments("intent-B", text="A rebound call must not run."), identity="B", delegation_id="delegation-A"
    )
    restarted[2]["event"]["item"].update(call_id="call-A", id="item-A")
    consume(voice, restarted[1:])
    assert len(held_messages) == 1
    assert tasks(voice)[0].text == arguments()["request_text"]


@pytest.mark.django_db(transaction=True)
def test_authoritative_context_is_bounded_and_reflects_real_worker_status(interpreter_voice, held_messages, use_bots):
    _, _, voice = interpreter_voice
    use_bots(say("The durable target completed cobalt-spoon."))
    for index in range(16):
        result = consume(
            voice,
            native_events(
                arguments(
                    f"unclear-{index}", "clarify", text=f"Ambiguous synthetic ask {index}.", clarification="Which task?"
                ),
                identity=str(index),
            ),
        )
    assert held_messages == []
    context = result["context"]["intents"]
    assert 0 < len(context) < 16
    assert context[-1]["intent_id"] == "unclear-15"
    assert context[-1]["request_text"] == "Ambiguous synthetic ask 15."
    assert all(row["status"] == "awaiting_clarification" for row in context)
    consume(voice, native_events())
    deliver(tasks(voice)[0].message)
    observed = consume(voice, native_events(arguments("observe", "status", "intent-A"), identity="observe"))
    target = next(row for row in observed["context"]["intents"] if row["intent_id"] == "intent-A")
    assert target["status"] == "answered" and "cobalt-spoon" in target["result"]
    assert len(held_messages) == 1


@pytest.mark.django_db(transaction=True)
def test_native_entry_preserves_private_thread_access_control(
    interpreter_voice, held_messages, use_bots, django_user_model
):
    chat, _, voice = interpreter_voice
    outsider = django_user_model.objects.create_user("native-private-owner")
    private = ConversationSession.objects.create(
        user=outsider,
        bot_name="kitchen",
        metadata={"bot_role": "thread", "title": "PRIVATE-NATIVE-THREAD"},
    )
    use_bots(
        tool_call("ergo_thread_send", {"thread": str(private.pk), "message": "Must remain unauthorized."}),
        say("The private target was not accessed."),
    )
    consume(voice, native_events(arguments(text=f"Send a task to thread {private.pk}.")))
    root = tasks(voice)[0].message
    deliver(root)
    assert root.recipient_session_id == chat.pk and root.status == "answered"
    assert not ThreadMessage.objects.filter(recipient_session=private).exists()
    assert not private.structured_calls.exists()
    assert len(held_messages) == 1


@pytest.mark.django_db(transaction=True)
def test_single_request_cost_grant_is_not_widened_by_native_interpretation(normal_voice, held_messages, use_bots):
    _, _, voice = normal_voice
    use_bots(say("The one approved request completed."))
    first = consume(voice, native_events())
    assert receipt(first)["status"] == "submitted"
    deliver(tasks(voice)[0].message)
    second = consume(voice, native_events(arguments("intent-B", text="Unapproved follow-up costs."), identity="B"))
    assert receipt(second, "call-B")["status"] == "rejected"
    assert len(held_messages) == 1 and len(tasks(voice)) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["status", "clarify"])
def test_observation_or_clarification_delegation_cannot_become_a_new_request_on_continuation(
    interpreter_voice, held_messages, kind
):
    _, _, voice = interpreter_voice
    first = arguments("first-semantic-intent", kind, text="Which task is this?", clarification="Which task?")
    consume(voice, native_events(first))
    assert held_messages == []
    continuation = native_events(
        arguments("hidden-new", text="A new task hidden in a semantic continuation."),
        identity="continuation",
        delegation_id="delegation-A",
    )[1:]
    result = consume(voice, continuation)
    assert receipt(result, "call-continuation")["status"] == "rejected"
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("second_kind", ["correct", "cancel", "new_request"])
def test_multiple_semantic_calls_reject_the_whole_response_before_any_task_dispatch(
    interpreter_voice, held_messages, second_kind
):
    _, _, voice = interpreter_voice
    events = native_events()
    second = native_events(
        arguments(
            "second-intent",
            second_kind,
            None if second_kind == "new_request" else "intent-A",
            text="Revise, cancel, or replace the first interpretation.",
        ),
        identity="second-call",
        delegation_id="delegation-A",
    )[2]
    result = consume(voice, [*events[:-1], second, events[-1]])
    assert len(result["receipts"]) == 2
    assert all(row["status"] == "rejected" for row in result["receipts"].values())
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_target_evicted_from_bounded_interpreter_context_requires_clarification(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    for index in range(16):
        result = consume(
            voice,
            native_events(
                arguments(
                    f"unclear-{index}", "clarify", text=f"Ambiguous request {index}.", clarification="Which task?"
                ),
                identity=str(index),
            ),
        )
    assert "unclear-0" not in {row["intent_id"] for row in result["context"]["intents"]}
    result = consume(
        voice,
        native_events(arguments("stale-reference", "status", "unclear-0"), identity="stale-reference"),
    )
    assert receipt(result, "call-stale-reference")["status"] == "clarification"
    assert receipt(result, "call-stale-reference")["clarification"]
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_initial_response_must_match_the_observed_delegation_response_identity(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    events = native_events()
    events[0]["delegation"]["response_id"] = "a-different-initial-response"
    consume(voice, events)
    assert held_messages == [] and not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_response_restart_before_prior_terminal_cannot_admit_either_interpretation(interpreter_voice, held_messages):
    _, _, voice = interpreter_voice
    original = native_events()
    consume(voice, original[:-1])
    restarted = native_events(
        arguments("restarted-intent", text="An interpretation before prior completion."),
        identity="restart",
        delegation_id="delegation-A",
    )[1:]
    consume(voice, restarted)
    consume(voice, original[-1:])
    assert held_messages == [] and not voice.delegations.exists()
