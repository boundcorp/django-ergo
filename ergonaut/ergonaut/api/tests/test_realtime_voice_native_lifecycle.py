"""Native bridge lifecycle regressions found by independent offline review."""

# ruff: noqa: F811 -- imported pytest fixtures are intentional.

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async

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


@pytest.mark.django_db(transaction=True)
def test_standby_waits_for_native_interpretation_and_its_tool_continuation(interpreter_voice, held_messages, settings):
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
            for event in events[2:]:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            await asyncio.sleep(0)
            assert not assistant.native_jobs
            assert not assistant.safe_to_standby(snapshot)
            reply = await assistant.ergo_voice_intent(SimpleNamespace(function_call=calls[-1]), **arguments())
            assert json.loads(reply)["status"] == "submitted"
            await sdk._append_items(
                [
                    llm.FunctionCallOutput(
                        call_id=calls[-1].call_id,
                        name="ergo_voice_intent",
                        output=reply,
                        is_error=False,
                    )
                ]
            )
            assert any(event["type"] == "response.create" for event in drain(sdk))
            assert not assistant.safe_to_standby(snapshot)
            continuation = native_events(identity="continuation", delegation_id="delegation-A")
            wire(assistant, sdk, continuation[1])
            wire(assistant, sdk, continuation[-1])
            await assistant.flush_native_events()
            await asyncio.sleep(0)
            assert assistant.safe_to_standby(snapshot)
            assert len(held_messages) == 1
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
