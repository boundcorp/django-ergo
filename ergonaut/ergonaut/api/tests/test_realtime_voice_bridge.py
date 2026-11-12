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
from livekit.agents.llm.duplex_adapter import AdaptiveNoiseGate, _DuplexRealtimeSession
from livekit.agents.voice.events import AgentStateChangedEvent, UserStateChangedEvent
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
    session._audio_ch = utils.aio.Chan()
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
    if assistant.final_jobs:
        await asyncio.gather(*assistant.final_jobs, return_exceptions=True)
    if assistant.publisher:
        await asyncio.gather(assistant.publisher, return_exceptions=True)
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


@pytest.mark.django_db(transaction=True)
def test_standby_requires_observed_vad_end_and_final_drained_transcript(cooking):
    _, vid, _ = cooking
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None)
        assistant.grace = 1.0
        try:
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
        finally:
            assistant.stop()
            await assistant.finish_live()

    async_to_sync(scenario)()


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


@pytest.mark.django_db(transaction=True)
def test_launch_thread_history_survives_standby_without_duplicate_provider_transcript(cooking, settings):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    settings.REALTIME_VOICE_STANDBY_ENABLED = True
    realtime_voice.record_event(vid, "continuity-marker", "user", "The pan is warm now.")
    assert realtime_voice.claim_bridge(vid, "initial")
    request = SimpleNamespace(auth=chat.user)
    realtime_voice.request_standby(request, vid)
    assert realtime_voice.begin_standby_close(vid, "initial")
    assert realtime_voice.finish_standby_close(vid, "initial")
    original_deadline = realtime_voice.bridge_state(vid)["bridge_started_at"]
    realtime_voice.resume_session(request, vid)
    assert realtime_voice.claim_bridge(vid, "resumed")
    snapshot = realtime_voice.bridge_state(vid)
    assert snapshot["chat_id"] == str(chat.id)
    assert snapshot["bridge_started_at"] == original_deadline
    assert "continuity-marker" in snapshot["briefing"]["voice_event_ids"]
    restored = bridge.startup_context(snapshot)
    text = "\n".join(item.text_content for item in restored.items if isinstance(item, llm.ChatMessage))
    assert text.count("The pan is warm now.") == 1
    assert not any(item.id == "continuity-marker" for item in restored.items)

    async def instructions():
        assistant = bridge.Assistant(vid, snapshot, None)
        assert "exact launch thread" in assistant.instructions
        assert "Restricted read-only mode" in assistant.instructions
        assert "gpt-6-luna" in assistant.instructions
        assert "thread forwarding/sends" in assistant.instructions

    async_to_sync(instructions)()


def test_old_unpersisted_voice_events_are_not_lost_when_thread_context_is_available():
    snapshot = {
        "state": {},
        "events": [{"event_id": "old-event", "kind": "user", "text": "Earlier voice text."}],
        "delegations": [],
        "briefing": {"context": "Exact launch context.", "voice_event_ids": []},
    }
    restored = bridge.startup_context(snapshot)
    assert any(item.id == "old-event" for item in restored.items)


@pytest.mark.django_db(transaction=True)
def test_sdk_identified_generation_stream_correlates_partial_interrupted_and_late_final(cooking, settings):
    chat, vid, other = cooking
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    assert realtime_voice.claim_bridge(vid, "progressive")
    snapshot = realtime_voice.bridge_state(vid)
    baseline = chat.messages.count()

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None, claim_id="progressive")
        sdk = offline_session(assistant)
        adapter = _DuplexRealtimeSession(assistant.llm, sdk, AdaptiveNoiseGate(), audio_timeout=1)
        generations = []
        adapter.on("generation_created", generations.append)
        with (
            patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk),
            patch.object(bridge.Assistant, "realtime_llm_session", new_callable=PropertyMock, return_value=adapter),
        ):
            try:
                await bind(assistant, sdk)
                # Raw SDK transcript deltas lack IDs. Do not guess a bubble or audio delivery.
                sdk._handle_event(
                    {"type": "session.output_transcript.delta", "delta": "First ", "start_ms": 0, "end_ms": 100}
                )
                sdk._handle_event(
                    {"type": "session.output_transcript.delta", "delta": "answer.", "start_ms": 100, "end_ms": 200}
                )
                assert not assistant.partials
                # The installed adapter creates a real, identified late-text generation.
                adapter._emit_as_text(2)
                generation = generations.pop()
                async for message in generation.message_stream:
                    turn_id = message.message_id
                    stream = message.text_stream.__aiter__()
                    first = await anext(stream)
                    assert str(first) == "First "
                    await assistant.flush_live()
                    partial = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                    [draft] = [t for t in partial["transcript"] if t["turn_id"] == turn_id]
                    assert not draft["is_final"] and draft["text"] == "First "
                    assert draft["delivery"] == "unknown"
                    assert not any(e["event_id"] == turn_id for e in partial["events"])
                    assert await sync_to_async(chat.messages.count)() == baseline
                    assert str(await anext(stream)) == "answer."
                    with pytest.raises(StopAsyncIteration):
                        await anext(stream)
                    await assistant.flush_live()
                item = llm.ChatMessage(id=turn_id, role="assistant", content=["First"], interrupted=True)
                assistant.queue_turn(item)
                assistant.queue_turn(item)  # duplicate framework final before the worker runs
                await settle(assistant)
                assistant.update_partial(turn_id, "assistant", "Late unfinalized suffix")
                await assistant.flush_live()
                result = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                [final] = [t for t in result["transcript"] if t["turn_id"] == turn_id]
                assert final["is_final"] and final["event_id"] == turn_id
                assert final["text"] == "First" and final["delivery"] == "interrupted"
                assert await sync_to_async(chat.messages.filter(provenance__voice_event_id=turn_id).count)() == 1
                assert sum(e["event_id"] == turn_id for e in result["events"]) == 1
                sdk._handle_event(
                    {"type": "session.input_transcript.delta", "delta": "A question", "start_ms": 300, "end_ms": 400}
                )
                user_id = sdk._speech["user"].message_id
                await assistant.flush_live()
                alternating = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                [user_turn] = [t for t in alternating["transcript"] if t["turn_id"] == user_id]
                assert user_turn["role"] == "user" and not user_turn["is_final"]
                sdk._end_speech("user")
                assistant.queue_turn(llm.ChatMessage(id=user_id, role="user", content=["A question"]))
                await settle(assistant)
                alternating = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                [user_turn] = [t for t in alternating["transcript"] if t["turn_id"] == user_id]
                assert user_turn["is_final"] and user_turn["text"] == "A question"
                # Later words create another adapter ID, never attach by text-prefix heuristics.
                sdk._handle_event(
                    {"type": "session.output_transcript.delta", "delta": "Next", "start_ms": 1000, "end_ms": 1100}
                )
                adapter._emit_as_text(1)
                async for message in generations.pop().message_stream:
                    assert message.message_id != turn_id
                    async for _ in message.text_stream:
                        pass
                await assistant.flush_live()
                result = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                assert any(not t["is_final"] and t["text"] == "Next" for t in result["transcript"])
                assert not any(e["kind"] == "result" for e in result["events"])
            finally:
                assistant.stop()
                await utils.aio.cancel_and_wait(adapter._segment_atask)
                await assistant.finish_live()
        untouched = await sync_to_async(realtime_voice.bridge_state)(other, include_briefing=False)
        assert untouched["transcript"] == [] and untouched["runtime"] == realtime_voice.UNKNOWN_RUNTIME

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_sdk_user_drafts_coalesce_and_final_flush_survives_stop_without_history_drafts(cooking, settings):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    assert realtime_voice.claim_bridge(vid, "coalesced")
    snapshot = realtime_voice.bridge_state(vid)
    baseline = chat.messages.count()

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None, claim_id="coalesced")
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            await bind(assistant, sdk)
            assistant.pending_id = "pending"
            await sync_to_async(realtime_voice.begin_pending)(vid, "pending", {})
            with patch.object(bridge, "publish_live_state", wraps=realtime_voice.publish_live_state) as publish:
                for index in range(200):
                    sdk._handle_event(
                        {
                            "type": "session.input_transcript.delta",
                            "delta": "x",
                            "start_ms": index * 10,
                            "end_ms": (index + 1) * 10,
                        }
                    )
                turn_id = sdk._speech["user"].message_id
                assert publish.call_count == 0
                await assistant.flush_live()
                assert publish.call_count == 1
                partial = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                [draft] = [t for t in partial["transcript"] if t["turn_id"] == turn_id]
                assert not draft["is_final"] and draft["text"] == "x" * 200
                assert partial["state"]["pending_delegation"]["fragments"] == [{"id": turn_id, "text": "x" * 200}]
                assert await sync_to_async(chat.messages.count)() == baseline
                sdk._end_speech("user")
                # The framework's conversation_item_added is the sole final-record path.
                item = llm.ChatMessage(id=turn_id, role="user", content=["x" * 200])
                assistant.queue_turn(item)
                assistant.queue_turn(item)
                assistant.stop()  # queued final is not one of the cancelled assembly jobs
                assistant.on_transcription(SimpleNamespace(item_id=turn_id, transcript="late", is_final=False))
                await assistant.finish_live()
                assert publish.call_count <= 3
            final = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
            [turn] = [t for t in final["transcript"] if t["turn_id"] == turn_id]
            assert turn["is_final"] and turn["text"] == "x" * 200
            assert turn["event_id"] == turn_id
            assert final["runtime"] == realtime_voice.UNKNOWN_RUNTIME
            assert not any(not t["is_final"] for t in final["transcript"])
            assert await sync_to_async(chat.messages.count)() == baseline + 1
            assert not final["delegations"]

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_runtime_uses_framework_events_not_transcript_or_queue_and_invalidates_microphone(cooking, settings):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    assert realtime_voice.claim_bridge(vid, "runtime")
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None, claim_id="runtime")
        stamp = datetime.now(UTC).timestamp()
        assistant.update_partial("generated", "assistant", "Thinking aloud is not a runtime observation")
        await assistant.flush_live()
        state = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
        assert state["runtime"] == realtime_voice.UNKNOWN_RUNTIME
        assistant.on_user_state(UserStateChangedEvent(old_state="speaking", new_state="listening", created_at=stamp))
        for index, agent_state in enumerate(("listening", "thinking", "speaking")):
            assistant.on_agent_state(
                AgentStateChangedEvent(old_state="idle", new_state=agent_state, created_at=stamp + index + 1)
            )
            await assistant.flush_live()
            state = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
            assert state["runtime"] == {
                "user_state": "listening",
                "agent_state": agent_state,
                "observed_at": datetime.fromtimestamp(stamp + index + 1, UTC).isoformat(),
            }
        assistant.on_agent_state(AgentStateChangedEvent(old_state="idle", new_state="thinking", created_at=stamp - 1))
        assert assistant.runtime["agent_state"] == "speaking"  # late callback cannot regress observed state
        assistant.microphone_live = lambda: False
        assistant.invalidate_runtime(user_only=True)
        assistant.on_user_state(
            UserStateChangedEvent(old_state="speaking", new_state="listening", created_at=stamp + 4)
        )
        await assistant.flush_live()
        state = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
        assert state["runtime"]["user_state"] == "unknown"
        assistant.stop()
        assistant.on_agent_state(AgentStateChangedEvent(old_state="speaking", new_state="listening"))
        await assistant.finish_live()
        state = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
        assert state["runtime"] == realtime_voice.UNKNOWN_RUNTIME
        assert not any(not turn["is_final"] for turn in state["transcript"])

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("publish_drafts", [False, True])
def test_opposite_order_finals_preserve_first_observed_turn_order_and_timestamps(cooking, settings, publish_drafts):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    assert realtime_voice.claim_bridge(vid, "ordered")
    snapshot = realtime_voice.bridge_state(vid)
    started = datetime.now(UTC) - timedelta(seconds=2)
    expected = [
        ("user-overlap", started.isoformat()),
        ("assistant-overlap", (started + timedelta(seconds=1)).isoformat()),
    ]

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None, claim_id="ordered")
        sdk = offline_session(assistant)
        with patch.object(bridge.Assistant, "duplex_session", new_callable=PropertyMock, return_value=sdk):
            try:
                with patch.object(bridge, "datetime", wraps=datetime) as clock:
                    clock.now.side_effect = [started, started + timedelta(seconds=1)]
                    assistant.update_partial("user-overlap", "user", "Question draft")
                    assistant.update_partial("assistant-overlap", "assistant", "Answer draft")
                # Deterministically exercise a final before the first timer flush.
                assistant.publisher.cancel()
                await asyncio.gather(assistant.publisher, return_exceptions=True)
                if publish_drafts:
                    await assistant.flush_live()
                before = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                overlapping = [turn for turn in before["transcript"] if turn["turn_id"] in dict(expected)]
                assert [(turn["turn_id"], turn["created_at"]) for turn in overlapping] == (
                    expected if publish_drafts else []
                )
                # The later-starting assistant final arrives first, potentially before
                # the coalescer ever persisted either draft.
                assistant.queue_turn(
                    llm.ChatMessage(id="assistant-overlap", role="assistant", content=["Answer final"])
                )
                await settle(assistant)
                intermediate = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                overlapping = [turn for turn in intermediate["transcript"] if turn["turn_id"] in dict(expected)]
                assert [(turn["turn_id"], turn["created_at"]) for turn in overlapping] == expected
                assert [turn["is_final"] for turn in overlapping] == [False, True]
                assistant.queue_turn(llm.ChatMessage(id="user-overlap", role="user", content=["Question final"]))
                await settle(assistant)
                final = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                overlapping = [turn for turn in final["transcript"] if turn["turn_id"] in dict(expected)]
                assert [(turn["turn_id"], turn["created_at"]) for turn in overlapping] == expected
                assert all(turn["is_final"] for turn in overlapping)
                assert len({turn["turn_id"] for turn in overlapping}) == 2
                evidence = [event for event in final["events"] if event["event_id"] in dict(expected)]
                assert [event["event_id"] for event in evidence] == ["assistant-overlap", "user-overlap"]
                assert all(
                    datetime.fromisoformat(event["received_at"]) > datetime.fromisoformat(event["created_at"])
                    for event in evidence
                )
            finally:
                assistant.stop()
                await assistant.finish_live()

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_delayed_final_write_never_removes_visible_draft_before_atomic_final_replacement(cooking, settings):
    chat, vid, _ = cooking
    settings.REALTIME_VOICE_USERS = {chat.user.get_username()}
    assert realtime_voice.claim_bridge(vid, "delayed-final")
    snapshot = realtime_voice.bridge_state(vid)

    async def scenario():
        assistant = bridge.Assistant(vid, snapshot, None, claim_id="delayed-final")
        entered = asyncio.Event()
        release = asyncio.Event()
        original_adapter = bridge.sync_to_async

        def delay_final(function, *args, **kwargs):
            if function is bridge.record_event:

                async def delayed(*call_args, **call_kwargs):
                    entered.set()
                    await release.wait()
                    return await original_adapter(function, *args, **kwargs)(*call_args, **call_kwargs)

                return delayed
            return original_adapter(function, *args, **kwargs)

        try:
            assistant.update_partial("delayed", "assistant", "Visible draft")
            await assistant.flush_live()
            first = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
            [original] = [turn for turn in first["transcript"] if turn["turn_id"] == "delayed"]
            with patch.object(bridge, "sync_to_async", side_effect=delay_final):
                assistant.queue_turn(llm.ChatMessage(id="delayed", role="assistant", content=["Authoritative final"]))
                await asyncio.wait_for(entered.wait(), timeout=5)
                # A runtime event makes the coalescer flush while the final write is
                # still waiting outside the transaction; the old bubble must remain.
                assistant.on_agent_state(AgentStateChangedEvent(old_state="speaking", new_state="listening"))
                await assistant.flush_live()
                pending = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
                [visible] = [turn for turn in pending["transcript"] if turn["turn_id"] == "delayed"]
                assert visible == original and not visible["is_final"]
                assert not any(event["event_id"] == "delayed" for event in pending["events"])
                release.set()
                await settle(assistant)
            final = await sync_to_async(realtime_voice.bridge_state)(vid, include_briefing=False)
            [visible] = [turn for turn in final["transcript"] if turn["turn_id"] == "delayed"]
            assert visible["is_final"] and visible["text"] == "Authoritative final"
            assert visible["created_at"] == original["created_at"]
            assert sum(event["event_id"] == "delayed" for event in final["events"]) == 1
        finally:
            release.set()
            assistant.stop()
            await assistant.finish_live()

    async_to_sync(scenario)()
