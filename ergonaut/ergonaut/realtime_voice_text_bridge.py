"""Alternate media protocol: finalized STT batches, normal bot tasks, exact-result TTS.

There is deliberately no live/realtime LLM, interpreter, tool schema or caption admission.
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime

from asgiref.sync import sync_to_async
from django.conf import settings
from django_ergo.conversation.attachments import openai_transcription
from django_ergo.conversation.inference_policy import effective_openai_base_url
from django_ergo.openai_credentials import voice_openai_key
from livekit.agents import Agent, AgentSession, APIConnectOptions, APIError, stt, utils
from livekit.agents.voice import room_io
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import openai, silero

from ergonaut.api.realtime_voice import (
    begin_standby_close,
    bridge_state,
    end_bridge_session,
    finish_standby_close,
    publish_live_state,
    record_event,
    renew_bridge_lease,
)
from ergonaut.api.voice import CHUNK_SECONDS, _charge, _chunks, _join_transcripts, _record_transcription
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.realtime_voice_outbox import thread_update_event
from ergonaut.realtime_voice_text import (
    begin_utterance,
    claim_output,
    fail_utterance,
    finish_utterance,
    pending_outputs,
    record_synthesis,
    require_session,
)

logger = logging.getLogger(__name__)


def media_scope(voice_id, claim_id):
    voice = RealtimeVoiceSession.objects.select_related("chat", "user").get(pk=voice_id)
    return voice, require_session(voice, claim_id)


class OriginatingThreadSTT(stt.STT):
    """The SDK calls recognize only for a VAD-finalized audio batch, never captions."""

    def __init__(self, voice_id, claim_id):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.voice_id, self.claim_id = voice_id, claim_id
        self.pending = 0
        self.accepting = True

    async def _recognize_impl(self, buffer, *, language, conn_options):
        # Assign before STT, independent of transcript content, time and SDK captions.
        utterance_id = uuid.uuid4().hex
        self.pending += 1
        try:
            if not self.accepting:
                raise ValueError("Audio admission has closed.")
            await sync_to_async(begin_utterance)(self.voice_id, self.claim_id, utterance_id)
            voice, grant = await sync_to_async(media_scope)(self.voice_id, self.claim_id)
            if not await sync_to_async(_charge)(voice.user):
                raise ValueError("Existing transcription service rate limit exceeded.")
            frame = utils.merge_frames(buffer)
            duration = frame.samples_per_channel / frame.sample_rate
            data = frame.to_wav_bytes()
            if (
                duration > settings.VOICE_TRANSCRIPTION_MAX_DURATION
                or len(data) > settings.VOICE_TRANSCRIPTION_MAX_BYTES
            ):
                raise ValueError("Audio exceeds the existing transcription service per-recording limit.")
            chunks = (
                [(data, "audio/wav", "utterance.wav", duration)]
                if duration <= CHUNK_SECONDS
                else await asyncio.to_thread(_chunks, data, "wav", duration)
            )
            texts = []
            for audio, mime, name, seconds in chunks:
                # Recheck consent at each media operation; no inferred approval or retries.
                await sync_to_async(media_scope)(self.voice_id, self.claim_id)
                if not self.accepting:
                    raise ValueError("Audio admission has closed.")
                response = await asyncio.wait_for(
                    openai_transcription(
                        audio,
                        mime,
                        name,
                        model=grant["stt_model"],
                        request_timeout=settings.VOICE_TRANSCRIPTION_TIMEOUT,
                    ),
                    timeout=settings.VOICE_TRANSCRIPTION_TIMEOUT,
                )
                await sync_to_async(_record_transcription)(
                    voice.user, voice.chat, grant["stt_model"], response, seconds
                )
                texts.append(response.text)
            text = _join_transcripts(texts)
            if not self.accepting:
                raise ValueError("Audio admission has closed.")
            await asyncio.shield(sync_to_async(finish_utterance)(self.voice_id, self.claim_id, utterance_id, text))
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                request_id=utterance_id,
                alternatives=[stt.SpeechData(language=language or "", text=text)],
            )
        except BaseException as exc:
            await asyncio.shield(sync_to_async(fail_utterance)(self.voice_id, self.claim_id, utterance_id))
            if isinstance(exc, asyncio.CancelledError):
                raise
            # SDK logging must not disclose provider messages/audio/transcripts.
            raise APIError("Speech could not be submitted; check the originating thread.") from None
        finally:
            self.pending -= 1


class TextAssistant(Agent):
    def __init__(self, voice_id, claim_id, recognizer, speech):
        super().__init__(instructions="", llm=None, stt=recognizer, tts=speech)
        self.voice_id, self.claim_id = voice_id, claim_id
        self.recognizer = recognizer
        self.user_speaking = False
        self.generation = 0
        self.closed = False
        self.outputs = set()
        self.agent_state = "unknown"
        self.user_state = "unknown"
        self.observed_at = None

    def user_state_changed(self, event):
        if self.closed:
            return
        self.observed_at = datetime.now(UTC).isoformat()
        self.user_state = event.new_state
        self.user_speaking = event.new_state == "speaking"
        if self.user_speaking:
            self.generation += 1
            # Interrupt playback/queued speech, never a normal task or its approvals.
            self.session.interrupt(force=True)

    async def publish_runtime(self):
        await sync_to_async(publish_live_state)(
            self.voice_id,
            self.claim_id,
            [],
            {"user_state": self.user_state, "agent_state": self.agent_state, "observed_at": self.observed_at},
        )

    async def deliver_results(self):
        for task in await sync_to_async(pending_outputs)(self.voice_id):
            event = await sync_to_async(thread_update_event)(self.voice_id, task)
            if event is None or task["status"] in ("queued", "delivered"):
                continue
            if self.closed or self.user_speaking or self.recognizer.pending:
                return  # Retain unclaimed durable outcomes for the next conversational gap.
            generation = self.generation
            if not await sync_to_async(claim_output)(self.voice_id, self.claim_id, event, task):
                continue
            speech_id = f"spoken:{event.event_id}"
            interrupted = self.closed or self.user_speaking or self.generation != generation
            published = await sync_to_async(record_event)(
                self.voice_id,
                speech_id,
                "assistant",
                event.text,
                "interrupted" if interrupted else "unknown",
                bridge_claim_id=self.claim_id,
            )
            # Recording can yield while new speech or End changes the boundary.
            interrupted = interrupted or self.closed or self.user_speaking or self.generation != generation
            if published is None:
                return
            if interrupted:
                continue
            handle = self.session.say(event.text, allow_interruptions=True, add_to_chat_ctx=False)
            monitor = asyncio.create_task(self.observe_output(handle, speech_id, event.text))
            self.outputs.add(monitor)
            monitor.add_done_callback(self.outputs.discard)
            # One output at a time; a fresh user onset interrupts it through the SDK.
            await monitor

    async def observe_output(self, handle, speech_id, text):
        try:
            await handle
            if handle.interrupted:
                await sync_to_async(record_event)(
                    self.voice_id,
                    speech_id,
                    "assistant",
                    text,
                    "interrupted",
                    trusted_delivery=True,
                    bridge_claim_id=self.claim_id,
                )
            if handle.exception() is not None:
                await sync_to_async(record_event)(
                    self.voice_id,
                    f"failed:{speech_id}",
                    "background_status",
                    "Spoken output failed; the verified result remains in the originating thread.",
                    bridge_claim_id=self.claim_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("text voice output evidence failed exception_type=%s", type(exc).__name__)


async def text_voice_session(ctx, voice_id, claim_id, snapshot):
    """The room/lease lifecycle is independent of durable normal work and provider-native voice."""
    _, grant = await sync_to_async(media_scope)(voice_id, claim_id)
    recognizer = OriginatingThreadSTT(voice_id, claim_id)
    speech = openai.TTS(
        model=grant["tts_model"],
        voice=grant["tts_voice"],
        base_url=effective_openai_base_url(),
        api_key=voice_openai_key(),
    )
    try:
        session = AgentSession(
            # Retain eligible long utterances; any actual overflow now exceeds
            # the existing recording limit and is rejected before transcription.
            vad=silero.VAD.load(max_buffered_speech=settings.VOICE_TRANSCRIPTION_MAX_DURATION + 1),
            # SDK warmup otherwise sends silence to STT during early barge-in.
            aec_warmup_duration=None,
            turn_handling={
                "turn_detection": "vad",
                "interruption": {"enabled": True, "mode": "vad"},
            },
            conn_options=SessionConnectOptions(
                stt_conn_options=APIConnectOptions(max_retry=0), tts_conn_options=APIConnectOptions(max_retry=0)
            ),
        )
        assistant = TextAssistant(voice_id, claim_id, recognizer, speech)
    except BaseException:
        await asyncio.gather(speech.aclose(), recognizer.aclose(), return_exceptions=True)
        raise
    disconnected = asyncio.Event()
    owner = f"user-{snapshot['user_id']}"
    tearing_down = False
    accounting = set()

    @speech.on("metrics_collected")
    def synthesis_usage(usage):
        task = asyncio.create_task(sync_to_async(record_synthesis)(voice_id, grant["tts_model"], usage))
        accounting.add(task)
        task.add_done_callback(accounting.discard)

    @ctx.room.on("participant_disconnected")
    def participant_left(participant):
        if participant.identity == owner and not tearing_down:
            recognizer.accepting = False
            disconnected.set()

    @ctx.room.on("disconnected")
    def room_left(*_):
        if not tearing_down:
            recognizer.accepting = False
            disconnected.set()

    @session.on("close")
    def session_closed(_):
        if not tearing_down:
            recognizer.accepting = False
            disconnected.set()

    @session.on("user_state_changed")
    def user_state_changed(event):
        assistant.user_state_changed(event)

    @session.on("agent_state_changed")
    def agent_state_changed(event):
        assistant.agent_state = event.new_state
        assistant.observed_at = datetime.now(UTC).isoformat()

    watcher = None
    standby = False
    try:
        await ctx.connect()
        # Owner-only input, not mixed room output; never feed the TTS output track to STT.
        await session.start(
            room=ctx.room,
            agent=assistant,
            room_options=room_io.RoomOptions(participant_identity=owner, text_input=False),
            record=False,
        )

        async def results():
            while not disconnected.is_set():
                await assistant.deliver_results()
                await asyncio.sleep(0.25)

        watcher = asyncio.create_task(results())
        renewed = 0.0
        while not disconnected.is_set():
            if time.monotonic() - renewed >= 5:
                if not await sync_to_async(renew_bridge_lease)(voice_id, claim_id):
                    break
                await sync_to_async(media_scope)(voice_id, claim_id)
                renewed = time.monotonic()
            current = await sync_to_async(bridge_state)(voice_id, include_briefing=False)
            if current["status"] != "active":
                break
            await assistant.publish_runtime()
            if (
                current["standby_state"] == "requested"
                and not assistant.user_speaking
                and not recognizer.pending
                and not assistant.outputs
            ):
                standby = await sync_to_async(begin_standby_close)(voice_id, claim_id)
                if standby:
                    break
            if watcher.done():
                await watcher  # Do not leave an apparently healthy call with a dead result watcher.
                break
            try:
                await asyncio.wait_for(disconnected.wait(), timeout=0.25)
            except TimeoutError:
                pass
    finally:
        recognizer.accepting = False
        assistant.closed = True
        assistant.generation += 1
        tearing_down = True
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        try:
            try:
                # End admission before SDK close drains final STT. Previously
                # accepted normal work continues independently of this call.
                if not standby or disconnected.is_set():
                    await sync_to_async(end_bridge_session)(voice_id, claim_id)
            finally:
                if session.current_speech:
                    session.interrupt(force=True)
                await session.aclose()
                if assistant.outputs:
                    await asyncio.gather(*assistant.outputs, return_exceptions=True)
        finally:
            await asyncio.gather(speech.aclose(), recognizer.aclose(), ctx.room.disconnect(), return_exceptions=True)
            if accounting:
                await asyncio.gather(*accounting, return_exceptions=True)
            if standby and not disconnected.is_set():
                await sync_to_async(finish_standby_close)(voice_id, claim_id, audio_uncertain=True)
            ctx.shutdown(reason="text-agent voice bridge closed")
