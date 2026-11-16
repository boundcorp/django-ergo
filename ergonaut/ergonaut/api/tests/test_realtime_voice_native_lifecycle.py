"""Native bridge lifecycle regressions found by independent offline review."""

# ruff: noqa: F811 -- imported pytest fixtures are intentional.

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django_ergo.bots import messaging, orchestrator, webhooks
from django_ergo.conversation.models import ThreadMessage

pytest.importorskip("livekit.plugins.openai.realtime.gpt_live_model")
from livekit.agents import llm

from ergonaut import realtime_voice_bridge as bridge
from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice_budget import cooking  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_interpreter import (  # noqa: F401
    arguments,
    bind,
    bot_folder,
    consume,
    cook,
    drain,
    held_messages,
    interpreter_voice,
    native_events,
    native_sdk,
    normal_voice,
    offline_key,
    settle,
    use_bots,
    wire,
)
from ergonaut.apps.bots.tests.fakes import say, tool_call


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("multiple_calls", [False, True], ids=["admitted", "multicall-rejected"])
def test_standby_waits_for_native_interpretation_and_its_tool_continuation(
    interpreter_voice, held_messages, settings, multiple_calls
):
    chat, _, voice = interpreter_voice
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    assert realtime_voice.claim_bridge(voice.pk, "native-standby")

    async def scenario():
        assistant = bridge.Assistant(
            str(voice.pk),
            await sync_to_async(realtime_voice.bridge_state)(voice.pk),
            None,
            claim_id="native-standby",
        )
        sdk = native_sdk(assistant)
        sdk._session_start_sent = True
        calls = []
        sdk.on("function_call", calls.append)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            assistant.on_user_state(SimpleNamespace(new_state="speaking"))
            wire(
                assistant,
                sdk,
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Run the disposable task.",
                    "start_ms": 0,
                    "end_ms": 100,
                },
            )
            sdk._end_speech("user")
            assistant.on_user_state(SimpleNamespace(new_state="listening"))
            assistant.speech_ended_at -= assistant.grace + 1
            assistant.transcript_changed_at -= 1
            events = native_events()
            for event in events[:2]:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            await asyncio.sleep(0)
            assert not assistant.native_jobs
            await sync_to_async(realtime_voice.request_standby)(SimpleNamespace(auth=chat.user), str(voice.pk))
            snapshot = await sync_to_async(realtime_voice.bridge_state)(voice.pk)
            assert snapshot["standby_state"] == "requested"
            assert not assistant.safe_to_standby(snapshot)
            assert held_messages == []
            wire(assistant, sdk, events[2])
            if multiple_calls:
                second = native_events(arguments("intent-B"), identity="B", delegation_id="delegation-A")[2]
                wire(assistant, sdk, second)
            wire(assistant, sdk, events[-1])
            await assistant.flush_native_events()
            await asyncio.sleep(0)
            assert not assistant.native_jobs
            assert not assistant.safe_to_standby(snapshot)
            outputs = []
            for call in calls:
                reply = await assistant.ergo_voice_intent(
                    SimpleNamespace(function_call=call), **json.loads(call.arguments)
                )
                assert json.loads(reply)["status"] == ("rejected" if multiple_calls else "submitted")
                outputs.append(
                    llm.FunctionCallOutput(
                        call_id=call.call_id,
                        name="ergo_voice_intent",
                        output=reply,
                        is_error=False,
                    )
                )
            await sdk._append_items(outputs)
            assert any(event["type"] == "response.create" for event in drain(sdk))
            assert not assistant.safe_to_standby(snapshot)
            continuation = native_events(identity="continuation", delegation_id="delegation-A")
            wire(assistant, sdk, continuation[1])
            wire(assistant, sdk, continuation[-1])
            await assistant.flush_native_events()
            await asyncio.sleep(0)
            assert assistant.safe_to_standby(snapshot)
            assert len(held_messages) == (0 if multiple_calls else 1)
            await settle(assistant)

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("restart", [True, False], ids=["new-response", "repeated-response"])
def test_conflicting_sdk_call_never_receives_the_old_submitted_receipt(interpreter_voice, held_messages, restart):
    _, _, voice = interpreter_voice

    async def scenario():
        assistant = bridge.Assistant(str(voice.pk), await sync_to_async(realtime_voice.bridge_state)(voice.pk), None)
        sdk = native_sdk(assistant)
        calls = []
        sdk.on("function_call", calls.append)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            original = native_events()
            for event in original:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            first = await assistant.ergo_voice_intent(SimpleNamespace(function_call=calls[-1]), **arguments())
            assert json.loads(first)["status"] == "submitted"
            if restart:
                conflict = native_events(identity="restart", delegation_id="delegation-A")[1:]
            else:
                conflict = copy.deepcopy(original[1:])
            changed = arguments(text="A conflicting request must not receive prior acceptance.")
            conflict[1]["event"]["item"]["call_id"] = "call-A"
            conflict[1]["event"]["item"]["arguments"] = json.dumps(changed)
            conflict[1]["event_id"] = "wire-conflicting-item"
            for event in conflict[:-1]:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            assert len(calls) == 2
            pending = asyncio.create_task(
                assistant.ergo_voice_intent(
                    SimpleNamespace(function_call=calls[-1]),
                    **changed,
                )
            )
            await asyncio.sleep(0)
            assert not pending.done()
            wire(assistant, sdk, conflict[-1])
            await assistant.flush_native_events()
            result = json.loads(await pending)
            assert result["status"] == "rejected"
            assert result["clarification"]
            assert len(held_messages) == 1
            await settle(assistant)

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_native_request_without_normal_grant_cannot_fall_back_to_a_caption_budget(cooking, held_messages):
    _, voice_id, _ = cooking
    from ergonaut.apps.bots.models import RealtimeVoiceSession

    voice = RealtimeVoiceSession.objects.get(pk=voice_id)
    result = consume(voice, native_events())
    assert result["receipts"]["call-A"]["status"] == "rejected"
    assert result["receipts"]["call-A"]["clarification"]
    assert held_messages == []
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_delayed_root_and_child_status_stay_silent_without_losing_results(interpreter_voice, held_messages, use_bots):
    chat, target, voice = interpreter_voice
    scripted = use_bots(
        tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "Check the disposable task."}),
        say("Handed off to the existing thread."),
        say("The disposable child returned cobalt-spoon."),
    )
    consume(voice, native_events())

    async def scenario():
        snapshot = await sync_to_async(realtime_voice.bridge_state)(voice.pk, include_briefing=False)
        assistant = bridge.Assistant(str(voice.pk), snapshot, None)
        sdk = native_sdk(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            drain(sdk)
            await assistant.deliver_results(snapshot)
            queued = drain(sdk)
            assert any(row["type"] == "session.thinking.append" for row in queued)
            assert not any(row["type"] == "session.commentary.append" for row in queued)
            for _ in range(20):
                await assistant.deliver_results(snapshot)
            assert drain(sdk) == []

            await sync_to_async(messaging.deliver)(held_messages[0], registry=webhooks.get_registry())
            handoff = await sync_to_async(realtime_voice.bridge_state)(voice.pk, include_briefing=False)
            await assistant.deliver_results(handoff)
            outgoing = drain(sdk)
            spoken = [row for row in outgoing if row["type"] == "session.commentary.append"]
            assert len(spoken) == 1 and "Handed off" in spoken[0]["content"]
            contexts = [row["content"] for row in outgoing if row["type"] == "session.thinking.append"]
            child = await sync_to_async(ThreadMessage.objects.get)(sender_session=chat, recipient_session=target)
            assert any(str(child.pk) in content and "queued" in content for content in contexts)
            # A stale root snapshot cannot regress its terminal result into pending speech/context.
            await assistant.deliver_results(snapshot)
            assert drain(sdk) == []

            await sync_to_async(messaging.deliver)(str(child.pk), registry=webhooks.get_registry())
            completed = await sync_to_async(realtime_voice.bridge_state)(voice.pk, include_briefing=False)
            await assistant.deliver_results(completed)
            results = [row for row in drain(sdk) if row["type"] == "session.commentary.append"]
            assert len(results) == 1 and "cobalt-spoon" in results[0]["content"]
            for _ in range(20):
                await assistant.deliver_results(completed)
            assert drain(sdk) == []
            # A new bridge cannot claim either durable outcome a second time.
            restored = bridge.Assistant(str(voice.pk), completed, None)
            await restored.deliver_results(completed)
            assert not any(row["type"] == "session.commentary.append" for row in drain(sdk))
            await settle(assistant)

    async_to_sync(scenario)()
    assert len(scripted.calls) == 3
    assert voice.events.filter(kind="result", delivery="announcement_claimed").count() == 2
    assert not voice.events.filter(kind="task_status", delivery="announcement_claimed").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("outcome", ["waiting", "failed"])
def test_approval_wait_and_failure_keep_one_announcement_attempt(
    interpreter_voice, held_messages, use_bots, monkeypatch, outcome
):
    _, target, voice = interpreter_voice
    scripted = use_bots(tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "Needs normal approval."}))
    if outcome == "waiting":
        monkeypatch.setattr(orchestrator.ergo_thread_send.__bot_tool__, "requires_approval", True)
    else:

        async def unavailable(**kwargs):
            raise RuntimeError("Private disposable failure must not be relayed.")

        scripted.create = unavailable
    consume(voice, native_events())
    messaging.deliver(held_messages[0], registry=webhooks.get_registry())
    snapshot = realtime_voice.bridge_state(voice.pk, include_briefing=False)
    assert snapshot["delegations"][0]["status"] == outcome

    async def scenario():
        assistant = bridge.Assistant(str(voice.pk), snapshot, None)
        sdk = native_sdk(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            drain(sdk)
            for _ in range(20):
                await assistant.deliver_results(snapshot)
            outgoing = drain(sdk)
            commentary = [row for row in outgoing if row["type"] == "session.commentary.append"]
            assert len(commentary) == 1
            assert not any(row["type"].startswith("response.") for row in outgoing)
            if outcome == "waiting":
                assert "voice has not approved" in commentary[0]["content"]
            else:
                assert "Private disposable failure" not in commentary[0]["content"]
            await settle(assistant)

    async_to_sync(scenario)()
    assert voice.events.filter(delivery="announcement_claimed").count() == 1
    assert not ThreadMessage.objects.filter(recipient_session=target).exists()
