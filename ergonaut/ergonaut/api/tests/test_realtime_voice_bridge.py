"""Exercise the installed GPT-Live wire parser and bridge without a socket or model.

Run with the isolated bridge SDK packages available. The SDK session constructor is
intentionally bypassed: it starts a network task. Parsing, events, transcript state,
client configuration and commentary serialization are the actual 1.8.11 implementation.
"""

# ruff: noqa: F811 -- imported pytest fixtures are intentional.

import asyncio
from unittest.mock import PropertyMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django_ergo.bots import messaging, webhooks

pytest.importorskip("livekit.plugins.openai.realtime.gpt_live_model")
from livekit.agents import llm, utils
from livekit.agents.llm._realtime.gpt_live import GPTLiveSession

from ergonaut import realtime_voice_bridge as bridge
from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice_budget import (  # noqa: F401
    bot_folder,
    cook,
    cooking,
    held_messages,
    use_bots,
)
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.apps.bots.tests.fakes import say, tool_call


@pytest.fixture(autouse=True)
def offline_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-no-provider-access")


def offline_session(assistant):
    session = GPTLiveSession.__new__(GPTLiveSession)
    llm.DuplexSession.__init__(session, assistant.llm.duplex_model)
    session._opts = assistant.llm.duplex_model._opts
    session._instructions = assistant.instructions
    session._history = assistant.chat_ctx.copy()
    session._speech = {}
    session._msg_ch = utils.aio.Chan()
    session._tools = llm.ToolContext.empty()
    session._session_start_sent = False

    # AgentActivity normally projects final transcriptions into Agent.chat_ctx.
    # Keep that framework boundary explicit; do not pretend the duplex session exposes chat_ctx.
    def finalize(event):
        if event.is_final:
            assistant._chat_ctx.add_message(id=event.item_id, role="user", content=event.transcript)

    session.on("input_audio_transcription_completed", finalize)
    return session


async def bind(assistant, session):
    await assistant.on_enter()
    # Polling is exercised explicitly, so no clock/scheduler race determines the outcome.
    assistant.watcher.cancel()
    await asyncio.gather(assistant.watcher, return_exceptions=True)


async def settle(assistant):
    if assistant.jobs:
        await asyncio.gather(*assistant.jobs)
    await asyncio.sleep(0)  # run done callbacks


def drain(session):
    events = []
    while not session._msg_ch.empty():
        events.append(session._msg_ch.recv_nowait().model_dump(exclude_none=True))
    return events


@pytest.mark.django_db(transaction=True)
def test_sdk_delegation_waits_for_confirmation_and_correlates_durable_commentary(cooking, use_bots, held_messages):
    chat, vid, other = cooking
    client = use_bots(tool_call("ergo_chat_history_sources", {}), say("The earlier report was pan-frying chicken."))
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None)
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            config = sdk._session_start_event().model_dump(exclude_none=True)
            assert config["session"]["delegation"] == {"type": "client"}
            await sdk._update_tools([])
            # Signal occurs during the first sentence; SDK pauses can finalize individual
            # fragments even while the speaker has more to say.
            sdk._handle_event(
                {"type": "session.input_transcript.delta", "delta": "Search my history. ", "start_ms": 0, "end_ms": 100}
            )
            wire = {"type": "session.delegation.created", "delegation": {"id": "offline-task", "target": "client"}}
            sdk._handle_event(wire)
            sdk._end_speech("user")
            sdk._handle_event(
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Do not claim you checked before the result.",
                    "start_ms": 1000,
                    "end_ms": 1100,
                }
            )
            sdk._end_speech("user")
            await settle(assistant)
            pending = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert pending["delegations"] == []
            assert [f["text"] for f in pending["state"]["pending_delegation"]["fragments"]] == [
                "Search my history. ",
                "Do not claim you checked before the result.",
            ]
            assert not held_messages
            text = "Search my history. Do not claim you checked before the result."
            await sync_to_async(realtime_voice.resolve_pending)(vid, "offline-task", text)
            sdk._handle_event(wire)  # late duplicate cannot create a second request
            await settle(assistant)
            queued = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert queued["delegations"][0]["text"] == text
            assert len(held_messages) == 1
            await sync_to_async(messaging.deliver)(held_messages[0], registry=webhooks.get_registry())
            result = await sync_to_async(realtime_voice.bridge_state)(vid)
            await assistant.deliver_results(result)
            await assistant.deliver_results(result)  # reconnect/poll duplicate cannot announce twice
            await assistant.persist_turn(llm.ChatMessage(role="assistant", content=["Delayed speech."]))
            events = drain(sdk)
            outcomes = [e for e in events if "Verified background outcome" in e.get("content", "")]
            assert [e["type"] for e in outcomes] == ["session.thinking.append", "session.commentary.append"]
            assert outcomes[1]["delegation_id"] == "offline-task"
            assert "The earlier report was pan-frying chicken." in outcomes[1]["content"]
            latest = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert not any("No verified background result" in e["text"] for e in latest["events"])

    async_to_sync(scenario)()
    task = realtime_voice.bridge_state(vid)["delegations"][0]
    assert task["status"] == "answered"
    assert chat.structured_calls.get().metadata["budget"]["used"]["model_turns"] == 2
    assert all(options["max_retries"] == 0 for options in client.options)
    assert (
        RealtimeVoiceSession.objects.get(pk=vid).events.filter(kind="result", delivery="announcement_claimed").exists()
    )
    assert not RealtimeVoiceSession.objects.get(pk=other).events.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("wire_delegation", [None, {"id": "wrong-target", "target": "responses"}, {"target": "client"}])
def test_no_client_signal_cannot_establish_background_success(cooking, held_messages, wire_delegation):
    _, vid, _ = cooking
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None)
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            if wire_delegation is not None:
                sdk._handle_event({"type": "session.delegation.created", "delegation": wire_delegation})
            await assistant.persist_turn(llm.ChatMessage(role="assistant", content=["I checked your history."]))
            await settle(assistant)
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert state["delegations"] == []
            assert not any(event["kind"] == "result" for event in state["events"])
            assert not any("Verified background outcome" in event.get("content", "") for event in drain(sdk))

    async_to_sync(scenario)()
    assert held_messages == []


@pytest.mark.django_db(transaction=True)
def test_callback_exception_is_consumed_and_durable_without_leaking_error(cooking, held_messages):
    _, vid, _ = cooking
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None)
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            sdk._history.add_message(role="user", content="Check history.")
            with patch.object(bridge, "begin_pending", side_effect=RuntimeError("sensitive-provider-detail")):
                sdk._handle_event(
                    {"type": "session.delegation.created", "delegation": {"id": "offline-error", "target": "client"}}
                )
                await settle(assistant)
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert any(
                event["kind"] == "background_status" and "could not be confirmed" in event["text"]
                for event in state["events"]
            )
            commentary = drain(sdk)
            assert any(
                "Check the durable background status" in event["content"] and event["delegation_id"] == "offline-error"
                for event in commentary
            )
            assert "sensitive-provider-detail" not in str(state) + str(commentary)

    async_to_sync(scenario)()
    assert held_messages == []


@pytest.mark.django_db(transaction=True)
def test_saved_context_is_not_an_implicit_user_request_or_fulfilled_lookup(cooking, held_messages):
    _, vid, _ = cooking
    snapshot = realtime_voice.bridge_state(vid)
    snapshot["events"] = []
    snapshot["state"] = {"current_step": "History checked"}

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None)
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            sdk._handle_event(
                {"type": "session.delegation.created", "delegation": {"id": "empty-context", "target": "client"}}
            )
            await settle(assistant)
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert state["delegations"] == []
            assert state["state"]["pending_delegation"]["status"] == "awaiting_confirmation"
            assert state["state"]["pending_delegation"]["fragments"] == []

    async_to_sync(scenario)()
    assert held_messages == []
