"""Exercise the installed GPT-Live wire parser and bridge without a socket or model.

Run with the isolated bridge SDK packages available. The SDK session constructor is
intentionally bypassed: it starts a network task. Parsing, events, transcript state,
client configuration and commentary serialization are the actual 1.8.11 implementation.
"""

# ruff: noqa: F811 -- imported pytest fixtures are intentional.

import asyncio
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django_ergo.bots import messaging, webhooks
from ninja.errors import HttpError

pytest.importorskip("livekit.plugins.openai.realtime.gpt_live_model")
from livekit import rtc
from livekit.agents import AgentSession, llm, utils
from livekit.agents.llm._realtime.gpt_live import GPTLiveSession
from livekit.agents.voice.io import AudioInput
from livekit.plugins import silero

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
        await asyncio.gather(*assistant.jobs, return_exceptions=True)
    await asyncio.sleep(0)  # run done callbacks


def drain(session):
    events = []
    while not session._msg_ch.empty():
        events.append(session._msg_ch.recv_nowait().model_dump(exclude_none=True))
    return events


@pytest.mark.django_db(transaction=True)
def test_sdk_delegation_assembles_resumed_speech_and_correlates_durable_commentary(cooking, use_bots, held_messages):
    chat, vid, other = cooking
    client = use_bots(tool_call("ergo_chat_history_sources", {}), say("The earlier report was pan-frying chicken."))
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None)
        assistant.grace = 0.2
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            config = sdk._session_start_event().model_dump(exclude_none=True)
            assert config["session"]["delegation"] == {"type": "client"}
            await sdk._update_tools([])
            assistant.on_user_state(SimpleNamespace(new_state="speaking"))
            sdk._handle_event(
                {"type": "session.input_transcript.delta", "delta": "Search my history. ", "start_ms": 0, "end_ms": 100}
            )
            wire = {"type": "session.delegation.created", "delegation": {"id": "offline-task", "target": "client"}}
            sdk._handle_event(wire)
            sdk._end_speech("user")
            assistant.on_user_state(SimpleNamespace(new_state="listening"))
            await asyncio.sleep(0.08)  # internal pause, shorter than the acoustic grace
            assert not held_messages
            assistant.on_user_state(SimpleNamespace(new_state="speaking"))
            sdk._handle_event(
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Do not claim you checked",
                    "start_ms": 1000,
                    "end_ms": 1100,
                }
            )
            assistant.on_user_state(SimpleNamespace(new_state="listening"))
            await asyncio.sleep(0.08)
            assert not held_messages
            # A late transcript tail must be drained and finalized, not dropped at VAD EOS.
            sdk._handle_event(
                {
                    "type": "session.input_transcript.delta",
                    "delta": " before the result.",
                    "start_ms": 1100,
                    "end_ms": 1200,
                }
            )
            sdk._end_speech("user")
            sdk._handle_event(wire)
            await settle(assistant)
            queued = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert queued["delegations"][0]["text"] == "Search my history. Do not claim you checked before the result."
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
            with patch.object(bridge, "PENDING_TIMEOUT_SECONDS", 0.1):
                await settle(assistant)
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert state["delegations"] == []
            assert state["state"]["pending_delegation"]["status"] == "expired"
            assert state["state"]["pending_delegation"]["fragments"] == []

    async_to_sync(scenario)()
    assert held_messages == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("stop", [True, False])
def test_stop_or_missing_final_never_dispatches(cooking, held_messages, stop):
    _, vid, _ = cooking

    async def scenario():
        assistant = bridge.Assistant(vid, await sync_to_async(realtime_voice.bridge_state)(vid), None)
        assistant.grace = 0.05
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            assistant.on_user_state(SimpleNamespace(new_state="speaking"))
            sdk._handle_event(
                {"type": "session.input_transcript.delta", "delta": "Search", "start_ms": 0, "end_ms": 100}
            )
            with patch.object(bridge, "PENDING_TIMEOUT_SECONDS", 0.3):
                sdk._handle_event(
                    {"type": "session.delegation.created", "delegation": {"id": "unfinished", "target": "client"}}
                )
                assistant.on_user_state(SimpleNamespace(new_state="listening"))
                if stop:
                    # Wait for durable signal before shutting the bridge down.
                    async with asyncio.timeout(5):
                        while not (await sync_to_async(realtime_voice.bridge_state)(vid))["state"].get(
                            "pending_delegation"
                        ):
                            await asyncio.sleep(0.01)
                    assistant.stop()
                    await settle(assistant)
                    await sync_to_async(realtime_voice.close_pending)(vid, "unfinished")
                else:
                    await settle(assistant)  # no SDK final, so the deadline must expire
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert state["state"]["pending_delegation"]["status"] == ("cancelled" if stop else "expired")
            assert state["delegations"] == []

    async_to_sync(scenario)()
    assert not held_messages


@pytest.mark.django_db(transaction=True)
def test_real_session_audio_input_drives_silero_speech_states(cooking, held_messages):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg with flite is required")
    speech = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "flite=text='Please search my earlier conversation for the pantry code'",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout

    class Input(AudioInput):
        def __init__(self):
            super().__init__(label="synthetic PCM")
            self.frames = asyncio.Queue()

        async def __anext__(self):
            return await self.frames.get()

    async def parked_provider(self):
        while not self._closing:
            await asyncio.sleep(0.01)

    async def scenario():
        _, vid, _ = cooking
        assistant = bridge.Assistant(vid, await sync_to_async(realtime_voice.bridge_state)(vid), None)
        session = AgentSession(vad=silero.VAD.load())
        audio = Input()
        session.input.audio = audio
        states = []
        ended = asyncio.Event()

        @session.on("user_state_changed")
        def state_changed(event):
            states.append(event.new_state)
            assistant.on_user_state(event)
            if "speaking" in states and event.new_state == "listening":
                ended.set()

        with patch.object(GPTLiveSession, "_main_task", parked_provider):
            try:
                await session.start(agent=assistant, record=False, session_host=False)
                # Real AudioInput -> AgentSession -> AgentActivity -> AudioRecognition ->
                # Silero ONNX, while GPT-Live's transport is parked.
                pcm = speech + bytes(16000 * 2 * 2)
                for offset in range(0, len(pcm) - 639, 640):
                    audio.frames.put_nowait(rtc.AudioFrame(pcm[offset : offset + 640], 16000, 1, 320))
                await asyncio.wait_for(ended.wait(), timeout=10)
                assert "speaking" in states and assistant.heard_speech
                assert not assistant.speaking and assistant.speech_ended_at is not None
                assert not held_messages  # VAD alone is never a delegation
            finally:
                assistant.closing = True
                if assistant.watcher:
                    assistant.watcher.cancel()
                    await asyncio.gather(assistant.watcher, return_exceptions=True)
                await session.aclose()

    async_to_sync(scenario)()


@pytest.mark.django_db
def test_standby_requires_observed_vad_end_and_final_drained_transcript(cooking):
    _, vid, _ = cooking
    snapshot = realtime_voice.bridge_state(vid)
    assistant = bridge.Assistant(vid, snapshot, None)
    assistant.grace = 1.0
    with patch.object(bridge.time, "monotonic", return_value=100.0) as clock:
        assistant.on_user_state(SimpleNamespace(new_state="listening"))
        assert not assistant.safe_to_standby(snapshot)  # silence is not proof of working VAD
        assistant.on_user_state(SimpleNamespace(new_state="speaking"))
        assert not assistant.safe_to_standby(snapshot)
        assistant.fragments["speech"] = "Partial"
        assistant.transcript_changed_at = 100.0
        assistant.on_user_state(SimpleNamespace(new_state="listening"))
        clock.return_value = 102.0
        assert not assistant.safe_to_standby(snapshot)
        assistant.final_fragments.add("speech")
        snapshot["state"]["pending_delegation"] = {"status": "collecting"}
        assert not assistant.safe_to_standby(snapshot)
        snapshot["state"]["pending_delegation"]["status"] = "submitted"
        assert assistant.safe_to_standby(snapshot)
        assistant.on_user_state(SimpleNamespace(new_state="speaking"))
        assert not assistant.safe_to_standby(snapshot)
        assistant.on_user_state(SimpleNamespace(new_state="listening"))
        assistant.on_user_state(SimpleNamespace(new_state="away"))
        clock.return_value = 104.0
        assert not assistant.safe_to_standby(snapshot)


@pytest.mark.django_db
def test_provider_usage_is_only_recorded_when_explicit_and_numeric(cooking):
    _, vid, _ = cooking
    assistant = bridge.Assistant(vid, realtime_voice.bridge_state(vid), None)
    assistant.on_protocol_event({"type": "session.usage.updated"})
    assistant.on_protocol_event({"type": "session.closed", "usage": {"seconds": "12"}})
    assert assistant.provider_usage_seconds is None
    assistant.on_protocol_event({"type": "session.usage.updated", "usage": {"seconds": 2.5}})
    assistant.on_protocol_event({"type": "session.closed", "usage": {"seconds": 3.75}})
    assert assistant.provider_usage_seconds == 3.75


@pytest.mark.django_db
def test_reconnect_latency_requires_provider_session_started(cooking):
    _, vid, _ = cooking
    snapshot = realtime_voice.bridge_state(vid)
    now = datetime.now(UTC)
    snapshot["resumed_at"] = (now - timedelta(seconds=2)).isoformat()
    assistant = bridge.Assistant(vid, snapshot, None)
    assistant.on_protocol_event({"type": "session.usage.updated", "usage": {"seconds": 1}})
    assert assistant.reconnect_latency_seconds is None
    with patch.object(bridge, "datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        assistant.on_protocol_event({"type": "session.started"})
    assert assistant.reconnect_latency_seconds == 2


@pytest.mark.django_db(transaction=True)
def test_disconnect_while_standby_waits_for_speech_is_terminal(cooking, settings):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    participant = SimpleNamespace(identity=f"user-{chat.user_id}", track_publications={})

    class Room:
        def __init__(self):
            self.handlers = {}
            self.remote_participants = {participant.identity: participant}

        def on(self, name):
            def register(handler):
                self.handlers[name] = handler
                return handler

            return register

        async def disconnect(self):
            self.handlers["disconnected"]()

    class Session:
        def __init__(self, **_kwargs):
            self.handlers = {}

        def on(self, name):
            def register(handler):
                self.handlers[name] = handler
                return handler

            return register

        async def start(self, *, agent, **_kwargs):
            agent.on_user_state(SimpleNamespace(new_state="speaking"))
            started.set()

        async def aclose(self):
            pass

    async def scenario():
        nonlocal started
        started = asyncio.Event()
        room = Room()

        class Context:
            job = SimpleNamespace(room=SimpleNamespace(name=f"{settings.REALTIME_VOICE_ROOM_PREFIX}{vid}"))

            def __init__(self):
                self.room = room

            async def connect(self):
                pass

            def shutdown(self, *, reason):
                self.reason = reason

        with (
            patch.object(bridge, "AgentSession", Session),
            patch.object(bridge.silero.VAD, "load", return_value=None),
            patch.object(realtime_voice, "get_bot", return_value=object()),
        ):
            task = asyncio.create_task(bridge.voice_session(Context()))
            await asyncio.wait_for(started.wait(), timeout=5)
            request = SimpleNamespace(auth=chat.user)
            result = await sync_to_async(realtime_voice.request_standby)(request, vid)
            assert result["standby_state"] == "requested"
            room.handlers["participant_disconnected"](participant)
            await asyncio.wait_for(task, timeout=5)
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert state["status"] == "ended"
            assert state["standby_state"] != "standby"

    started = None
    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("speech_timing", ["none", "before_commit", "during_close"])
def test_standby_speech_races_and_teardown(cooking, settings, speech_timing):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    publication = SimpleNamespace(
        source=rtc.TrackSource.SOURCE_MICROPHONE, subscribed=True, track=object(), muted=False
    )
    participant = SimpleNamespace(identity=f"user-{chat.user_id}", track_publications={"mic": publication})

    class Room:
        def __init__(self):
            self.handlers = {}
            self.remote_participants = {participant.identity: participant}
            self.disconnected = False

        def on(self, name):
            def register(handler):
                self.handlers[name] = handler
                return handler

            return register

        async def disconnect(self):
            self.disconnected = True
            self.handlers["disconnected"]()

    class Session:
        def __init__(self, **_kwargs):
            self.handlers = {}

        def on(self, name):
            def register(handler):
                self.handlers[name] = handler
                return handler

            return register

        async def start(self, *, agent, **_kwargs):
            self.agent = agent
            agents.append(agent)
            agent.on_user_state(SimpleNamespace(new_state="speaking"))
            agent.on_transcription(SimpleNamespace(item_id="one", transcript="Finished", is_final=True))
            agent.on_user_state(SimpleNamespace(new_state="listening"))
            agent.speech_ended_at -= agent.grace + 1
            agent.transcript_changed_at -= bridge.TRANSCRIPT_DRAIN_SECONDS + 1
            started.set()

        async def aclose(self):
            closing.set()
            await allow_close.wait()
            if speech_timing == "during_close":
                self.agent.on_user_state(SimpleNamespace(new_state="speaking"))

    async def scenario():
        nonlocal started, closing, allow_close
        started, closing, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
        race_detected = asyncio.Event()
        release_reservation = ThreadEvent()
        loop = asyncio.get_running_loop()
        room = Room()
        original_reserve = bridge.begin_standby_close

        def reserve(*args):
            result = original_reserve(*args)
            if result and speech_timing == "before_commit" and not release_reservation.is_set():

                def detect_speech():
                    agents[0].on_user_state(SimpleNamespace(new_state="speaking"))
                    race_detected.set()
                    release_reservation.set()

                loop.call_soon_threadsafe(detect_speech)
                assert release_reservation.wait(timeout=5)
            return result

        class Context:
            job = SimpleNamespace(room=SimpleNamespace(name=f"{settings.REALTIME_VOICE_ROOM_PREFIX}{vid}"))

            def __init__(self):
                self.room = room

            async def connect(self):
                pass

            def shutdown(self, *, reason):
                self.reason = reason

        with (
            patch.object(bridge, "AgentSession", Session),
            patch.object(bridge.silero.VAD, "load", return_value=None),
            patch.object(realtime_voice, "get_bot", return_value=object()),
            patch.object(bridge, "begin_standby_close", side_effect=reserve),
        ):
            task = asyncio.create_task(bridge.voice_session(Context()))
            await asyncio.wait_for(started.wait(), timeout=5)
            request = SimpleNamespace(auth=chat.user)
            await sync_to_async(realtime_voice.request_standby)(request, vid)
            if speech_timing == "before_commit":
                await asyncio.wait_for(race_detected.wait(), timeout=5)
                async with asyncio.timeout(5):
                    while (await sync_to_async(realtime_voice.bridge_state)(vid))["standby_state"] != "requested":
                        await asyncio.sleep(0.01)
                assert not closing.is_set()
                room.handlers["participant_disconnected"](participant)
                allow_close.set()
                await asyncio.wait_for(task, timeout=5)
                return
            await asyncio.wait_for(closing.wait(), timeout=5)
            state = await sync_to_async(realtime_voice.session_out)(
                await sync_to_async(RealtimeVoiceSession.objects.get)(pk=vid)
            )
            assert state["standby_state"] == "closing"
            assert not room.disconnected
            with pytest.raises(HttpError) as exc:
                await sync_to_async(realtime_voice.resume_session)(request, vid)
            assert exc.value.status_code == 409
            allow_close.set()
            await asyncio.wait_for(task, timeout=5)
            state = await sync_to_async(realtime_voice.bridge_state)(vid)
            assert room.disconnected
            assert state["status"] == "active"
            assert state["standby_state"] == "standby"
            assert state["state"].get("standby_audio_uncertain", False) is (speech_timing == "during_close")
            resumed = await sync_to_async(realtime_voice.resume_session)(request, vid)
            assert resumed["standby_state"] == "resuming"

    agents = []
    started = closing = allow_close = None
    async_to_sync(scenario)()
