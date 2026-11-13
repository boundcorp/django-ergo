"""Isolated LiveKit GPT-Live worker for an explicitly enabled local voice slice."""

import asyncio
import hashlib
import logging
import math
import os
import time
import uuid
from datetime import UTC, datetime

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")

import django

django.setup()

import aiohttp  # noqa: E402
from asgiref.sync import sync_to_async  # noqa: E402
from django.conf import settings  # noqa: E402
from livekit import agents, rtc  # noqa: E402
from livekit.agents import Agent, AgentServer, AgentSession, APIConnectOptions, llm  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402

from ergonaut.api.realtime_voice import (  # noqa: E402
    BRIDGE_JOB_SECONDS,
    MAX_PARTIAL_TURNS,
    MAX_TRANSCRIPT_CHARS,
    UNKNOWN_RUNTIME,
    begin_pending,
    begin_standby_close,
    bridge_state,
    cancel_standby_close,
    claim_bridge,
    close_pending,
    current_time,
    end_bridge_session,
    finish_standby_close,
    publish_live_state,
    record_event,
    resolve_pending,
)
from ergonaut.realtime_voice_outbox import claim_announcement, completion_event, thread_update_event  # noqa: E402

logger = logging.getLogger(__name__)
MAX_SECONDS = BRIDGE_JOB_SECONDS
_job_claimed = False
TRANSCRIPT_DRAIN_SECONDS = 0.8
PENDING_TIMEOUT_SECONDS = 60
LIVE_PUBLISH_SECONDS = 0.25


def pause_grace():
    """Additional acoustic pause after Silero EOS; not a semantic completion guarantee."""
    value = getattr(settings, "REALTIME_VOICE_PAUSE_GRACE_SECONDS", 2.0)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.5 <= value <= 10:
        raise ValueError("REALTIME_VOICE_PAUSE_GRACE_SECONDS must be between 0.5 and 10")
    return float(value)


class OneConnection:
    """Cap provider WebSocket creation, including SDK-initiated reconnects."""

    def __init__(self, session):
        self.session = session
        self.used = False

    async def ws_connect(self, *args, **kwargs):
        if self.used:
            raise RuntimeError("provider connection limit reached")
        self.used = True
        return await self.session.ws_connect(*args, **kwargs)


def startup_context(snapshot):
    """Restore bounded finalized text without claiming unconfirmed audio was heard."""
    context = llm.ChatContext.empty()
    context.add_message(
        id="saved-voice-state",
        role="user",
        content=f"Saved session state (reported context, NOT a history lookup result): {snapshot['state']}",
    )
    if snapshot.get("briefing"):
        context.add_message(
            id="owner-startup-briefing",
            role="user",
            content=snapshot["briefing"]["context"],
        )
    # Persisted voice messages appear once via launch-thread history. Pre-cutover events
    # without a thread-history row still use the bounded durable transcript fallback.
    saved_ids = set(snapshot.get("briefing", {}).get("voice_event_ids", []))
    for event in snapshot["events"][-40:]:
        if event["event_id"] in saved_ids:
            continue
        if event["kind"] == "user":
            context.add_message(id=event["event_id"], role="user", content=event["text"][:2000])
        elif event["kind"] == "assistant":
            context.add_message(
                role="assistant", content=f"[Generated speech; delivery {event['delivery']}] {event['text'][:2000]}"
            )
    for task in snapshot["delegations"]:
        if task["status"] == "answered" and task["result"]:
            context.add_message(
                id=f"verified-result:{task['event_id']}",
                role="user",
                content=f"Verified background result for {task['event_id']}: {task['result'][:1500]}",
            )
    for task in snapshot.get("thread_updates", [])[-10:]:
        context.add_message(
            id=f"thread-update:{task['update_id']}",
            role="user",
            content=(
                f"Durable thread observation for {task['event_id']} (not proof voice started it): "
                f"{task['status']}. {task['result'][:1000] if task['status'] == 'answered' else ''}"
            ),
        )
    return context


class Assistant(Agent):
    def __init__(self, voice_id, snapshot, http_session, *, claim_id=None):
        self.voice_id = voice_id
        self.announced = set()
        self.claim_id = claim_id
        self.partials = {}
        self.finished_turns = {}
        self.final_jobs = set()
        self.finalizing = set()
        self.accept_finals = True
        self.live_finished = False
        self.runtime = dict(UNKNOWN_RUNTIME)
        self.runtime_available = True
        self.runtime_times = {}
        self.microphone_live = None
        self.publisher = None
        self.live_dirty = False
        self.jobs = set()
        self.watcher = None
        self.fragments = {}
        self.fragments_overflow = False
        self.pending_id = None
        self.final_fragments = set()
        self.speaking = False
        self.heard_speech = False
        self.speech_ended_at = None
        self.transcript_changed_at = 0.0
        self.changed = asyncio.Event()
        self.closing = False
        self.vad_ready = False
        self.activity_generation = 0
        self.provider_usage_seconds = None
        self.resumed_at = datetime.fromisoformat(snapshot["resumed_at"]) if snapshot.get("resumed_at") else None
        self.reconnect_latency_seconds = None
        self.grace = pause_grace()
        super().__init__(
            instructions=(
                snapshot.get("briefing", {}).get("instructions", "")[:8000]
                + "\n\nVoice-interface restrictions (override conflicting bot instructions):\n"
                + "Maintain session context across topics. You are another interface to the exact launch thread, "
                "not a fresh assistant. Follow its supplied owning bot instructions within the restrictions below. "
                "To check stored conversation history or perform requested "
                "background work, delegate to the client background assistant using client delegation. "
                "There are no callable voice tools. Never substitute a recap of this conversation "
                "for an actual history lookup. Saved state and server time context are not lookup results. "
                "Until a verified background outcome arrives, say you have no verified result; "
                "never say you checked, searched or found history. Never say background work is running "
                "unless its verified status says so. A queued request may not have started. "
                "Report completion only when given a verified result. On reconnect do "
                "not repeat a saved result unless asked. Generated speech is not work evidence. "
                "Speech marked unknown or interrupted may not have been heard."
                " For activity durations use only supplied server-calculated elapsed "
                "time since the user reported the activity, not a claim that it happened. "
                "Never invent times, durations, timers or reminders."
                " Startup briefing is stored context, not a newly verified result. "
                "Explain the actual execution mode below. A voice utterance or delegation signal is never "
                "an action approval; normal thread approval controls remain authoritative. "
                + snapshot.get("briefing", {}).get(
                    "restriction", "Background delegation availability is not known; do not promise it."
                )
            ),
            chat_ctx=startup_context(snapshot),
            llm=openai.realtime.GPTLiveModel(
                model="gpt-live-1",
                delegation="client",
                http_session=OneConnection(http_session),
                max_session_duration=None,
                conn_options=APIConnectOptions(max_retry=0),
            ),
        )

    def stop(self):
        self.closing = True
        self.invalidate_runtime()
        for job in tuple(self.jobs):
            job.cancel()

    def spawn(self, coroutine, *, delegation_id=None):
        if self.closing:
            coroutine.close()
            return
        task = asyncio.create_task(self.run_job(coroutine, delegation_id=delegation_id))
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def run_job(self, coroutine, *, delegation_id=None):
        try:
            await coroutine
        except Exception as exc:
            # Consume async callback exceptions without logging payloads, credentials or transcripts.
            logger.error("voice bridge callback failed exception_type=%s", type(exc).__name__)
            if self.closing:
                return
            notice = "A voice bridge action could not be confirmed. Check the durable background status."
            try:
                await sync_to_async(record_event, thread_sensitive=True)(
                    self.voice_id, f"bridge-error:{uuid.uuid4().hex}", "background_status", notice
                )
                self.duplex_session.append_commentary(notice, delegation_id=delegation_id)
            except Exception as notice_exc:
                logger.error("voice bridge status failed exception_type=%s", type(notice_exc).__name__)

    def queue_live(self):
        self.live_dirty = True
        if self.publisher is None or self.publisher.done():
            self.publisher = asyncio.create_task(self.publish_updates())

    async def publish_updates(self):
        try:
            while self.live_dirty:
                await asyncio.sleep(LIVE_PUBLISH_SECONDS)
                await self.flush_live()
        except Exception as exc:
            # No per-token retries or transcript-bearing error messages.
            logger.error("voice live state failed exception_type=%s", type(exc).__name__)

    async def flush_live(self):
        if not self.live_dirty:
            return
        self.live_dirty = False
        await sync_to_async(publish_live_state, thread_sensitive=True)(
            self.voice_id,
            self.claim_id,
            [dict(p) for p in self.partials.values()],
            dict(self.runtime),
            self.pending_id,
            dict(self.fragments),
        )

    def update_partial(self, turn_id, role, text):
        if self.closing or turn_id in self.finished_turns or not turn_id or len(turn_id) > 200:
            return
        prior = self.partials.get(turn_id)
        self.partials[turn_id] = {
            "turn_id": turn_id,
            "role": role,
            "text": text[:MAX_TRANSCRIPT_CHARS],
            "is_final": False,
            "delivery": "unknown",
            "created_at": prior["created_at"] if prior else datetime.now(UTC).isoformat(),
        }
        while len(self.partials) > MAX_PARTIAL_TURNS:
            self.partials.pop(next(iter(self.partials)))
        self.queue_live()

    def observe_runtime(self, role, event):
        if self.closing or not self.runtime_available:
            return
        allowed = (
            ("speaking", "listening", "away")
            if role == "user"
            else ("initializing", "idle", "listening", "thinking", "speaking")
        )
        stamp = getattr(event, "created_at", None)
        if not isinstance(stamp, int | float) or not math.isfinite(stamp):
            stamp = datetime.now(UTC).timestamp()
        if stamp < self.runtime_times.get(role, 0):
            return
        self.runtime_times[role] = stamp
        state = event.new_state if event.new_state in allowed else "unknown"
        if role == "user" and self.microphone_live and not self.microphone_live():
            state = "unknown"
        self.runtime[f"{role}_state"] = state
        self.runtime["observed_at"] = datetime.fromtimestamp(max(self.runtime_times.values()), UTC).isoformat()
        self.queue_live()

    def invalidate_runtime(self, *, user_only=False):
        if user_only:
            self.runtime = {**self.runtime, "user_state": "unknown", "observed_at": None}
        else:
            self.runtime = dict(UNKNOWN_RUNTIME)
            self.runtime_available = False
        self.queue_live()

    def on_agent_state(self, event):
        self.observe_runtime("agent", event)

    def on_generation(self, event):
        # Raw duplex transcript_delta has no message ID. The pinned SDK adapter
        # assigns IDs only after splitting audio bursts and late text generations.
        # Observe its identified text stream inline, without consuming it twice or
        # claiming that generated text/audio was played.
        if not self.closing:
            event.message_stream = self.observe_messages(event.message_stream)

    async def observe_messages(self, messages):
        async for message in messages:
            message.text_stream = self.observe_text(message.message_id, message.text_stream)
            yield message

    async def observe_text(self, turn_id, stream):
        text = ""
        async for delta in stream:
            text = (text + delta)[:MAX_TRANSCRIPT_CHARS]
            self.update_partial(turn_id, "assistant", text)
            yield delta  # Preserve SDK TimedString and synchronizer behavior verbatim.

    def queue_turn(self, item):
        if not self.accept_finals or item.id in self.finished_turns or item.id in self.finalizing:
            return
        self.finalizing.add(item.id)
        # Finals use a separate drain set: Stop cancels request assembly, never
        # a final already accepted from the SDK.
        task = asyncio.create_task(self.run_job(self.persist_turn(item, accepted_at=datetime.now(UTC))))
        self.final_jobs.add(task)
        task.add_done_callback(self.final_jobs.discard)
        task.add_done_callback(lambda _: self.finalizing.discard(item.id))

    async def finish_live(self):
        if self.live_finished:
            return
        self.accept_finals = False
        if self.final_jobs:
            await asyncio.gather(*self.final_jobs, return_exceptions=True)
        self.partials.clear()
        self.invalidate_runtime()
        if self.publisher:
            await asyncio.gather(self.publisher, return_exceptions=True)
        await self.flush_live()
        self.live_finished = True

    async def on_enter(self):
        self.duplex_session.on("delegation_created", self.on_delegation)
        self.duplex_session.on("input_audio_transcription_completed", self.on_transcription)
        self.duplex_session.on("openai_server_event_received", self.on_protocol_event)
        self.duplex_session.on("openai_client_event_queued", self.on_client_event)
        try:
            self.realtime_llm_session.on("generation_created", self.on_generation)
        except RuntimeError:
            # Without the adapter's identified stream, assistant drafts stay unknown.
            # Final conversation_item_added remains authoritative.
            pass
        logger.info("voice delegation_listener_ready")
        self.watcher = asyncio.create_task(self.watch_results())

    def on_protocol_event(self, event):
        if event.get("type") == "session.started" and self.resumed_at and self.reconnect_latency_seconds is None:
            self.reconnect_latency_seconds = max(0.0, (datetime.now(UTC) - self.resumed_at).total_seconds())
        if event.get("type") in ("session.usage.updated", "session.closed"):
            usage = event.get("usage")
            seconds = usage.get("seconds") if isinstance(usage, dict) else None
            if (
                isinstance(seconds, int | float)
                and not isinstance(seconds, bool)
                and math.isfinite(seconds)
                and seconds >= 0
            ):
                self.provider_usage_seconds = seconds
        if event.get("type") == "session.delegation.created":
            metadata = event.get("delegation") or {}
            delegation_id = metadata.get("id")
            logger.info(
                "voice delegation_wire_received has_id=%s client_target=%s correlation=%s",
                bool(delegation_id),
                metadata.get("target") == "client",
                self.correlation(delegation_id),
            )

    @staticmethod
    def correlation(delegation_id):
        return hashlib.sha256(delegation_id.encode()).hexdigest()[:16] if isinstance(delegation_id, str) else "none"

    def on_client_event(self, event):
        if event.get("type") == "session.commentary.append":
            logger.info("voice commentary_queued correlation=%s", self.correlation(event.get("delegation_id")))

    def on_transcription(self, event):
        if self.closing or event.item_id in self.finished_turns:
            if self.closing:
                self.activity_generation += 1
            return
        self.activity_generation += 1
        if (event.item_id not in self.fragments and len(self.fragments) >= 100) or len(event.transcript) > 4000:
            self.fragments_overflow = True
        if event.item_id in self.fragments or len(self.fragments) < 100:
            self.fragments[event.item_id] = event.transcript[:MAX_TRANSCRIPT_CHARS]
        if event.is_final and event.item_id in self.fragments:
            self.final_fragments.add(event.item_id)
        else:
            self.final_fragments.discard(event.item_id)
        self.update_partial(event.item_id, "user", event.transcript)
        self.transcript_changed_at = time.monotonic()
        self.changed.set()

    def on_user_state(self, event):
        if self.closing:
            if event.new_state == "speaking":
                self.activity_generation += 1
            return
        self.observe_runtime("user", event)
        # With an explicitly supplied VAD, the SDK's recognition pipeline drives
        # these states from its actual Silero audio stream, not GPT-Live segments.
        if event.new_state == "speaking":
            self.activity_generation += 1
            self.vad_ready = True
            logger.info("voice vad_speech_started correlation=%s", self.correlation(self.pending_id))
            if (
                not self.pending_id
                and self.speech_ended_at is not None
                and time.monotonic() - self.speech_ended_at > self.grace
            ):
                self.fragments.clear()
                self.final_fragments.clear()
                self.fragments_overflow = False
            self.speaking = True
            self.heard_speech = True
            self.speech_ended_at = None
        elif event.new_state == "listening" and self.speaking:
            self.speaking = False
            self.speech_ended_at = time.monotonic()
            logger.info("voice vad_speech_ended correlation=%s", self.correlation(self.pending_id))
        elif event.new_state != "listening":
            self.vad_ready = False
        self.changed.set()

    def safe_to_standby(self, snapshot):
        # Listening alone is not evidence that audio or VAD has ever been operational.
        pending = snapshot["state"].get("pending_delegation") or {}
        return (
            self.heard_speech
            and self.vad_ready
            and not self.speaking
            and self.speech_ended_at is not None
            and time.monotonic() - self.speech_ended_at >= self.grace
            and time.monotonic() - self.transcript_changed_at >= TRANSCRIPT_DRAIN_SECONDS
            and bool(self.fragments)
            and not self.fragments_overflow
            and self.final_fragments.issuperset(self.fragments)
            and pending.get("status") != "collecting"
            and not self.jobs
            and not self.closing
        )

    def on_delegation(self, event):
        self.activity_generation += 1
        logger.info("voice delegation_signal_received correlation=%s", self.correlation(event.id))
        if self.pending_id or self.closing:
            return
        self.pending_id = event.id
        self.spawn(self.assemble_request(event.id, dict(self.fragments)), delegation_id=event.id)

    async def assemble_request(self, event_id, fragments):
        await sync_to_async(begin_pending, thread_sensitive=True)(self.voice_id, event_id, fragments)
        self.duplex_session.append_thinking(
            "A background request is being assembled from speech; it is not submitted yet."
        )
        deadline = time.monotonic() + PENDING_TIMEOUT_SECONDS
        while not self.closing:
            self.changed.clear()
            now = time.monotonic()
            if now >= deadline:
                await sync_to_async(close_pending, thread_sensitive=True)(self.voice_id, event_id, "expired")
                return
            if self.heard_speech and not self.speaking and self.speech_ended_at is not None:
                ready_at = max(
                    self.speech_ended_at + self.grace,
                    self.transcript_changed_at + TRANSCRIPT_DRAIN_SECONDS,
                )
                if (
                    now >= ready_at
                    and self.fragments
                    and not self.fragments_overflow
                    and self.final_fragments.issuperset(self.fragments)
                ):
                    text = " ".join(part.strip() for part in self.fragments.values() if part.strip())
                    if text and len(text) <= 4000:
                        logger.info("voice delegation_boundary_ready correlation=%s", self.correlation(event_id))
                        await sync_to_async(resolve_pending, thread_sensitive=True)(self.voice_id, event_id, text)
                        return
                wait = max(0.01, ready_at - now) if now < ready_at else deadline - now
            else:
                wait = deadline - now
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=min(wait, deadline - now))
            except TimeoutError:
                pass

    async def persist_turn(self, item, *, accepted_at=None):
        if item.id in self.finished_turns:
            return
        self.finished_turns[item.id] = None
        while len(self.finished_turns) > 100:
            self.finished_turns.pop(next(iter(self.finished_turns)))
        draft = self.partials.get(item.id)
        if not item.text_content:
            # An authoritative empty/interrupted turn discards its draft, not history.
            self.partials.pop(item.id, None)
            self.queue_live()
            return
        delivery = "interrupted" if item.interrupted else "unknown"
        try:
            event = await sync_to_async(record_event, thread_sensitive=True)(
                self.voice_id,
                item.id,
                item.role,
                item.text_content[:MAX_TRANSCRIPT_CHARS],
                delivery,
                bridge_claim_id=self.claim_id,
                received_at=accepted_at,
                turn_started_at=datetime.fromisoformat(draft["created_at"]) if draft else None,
            )
        except Exception as exc:
            logger.error("voice final transcript failed exception_type=%s", type(exc).__name__)
            return
        if event is None:
            return
        # The server transaction already replaced the published draft with its final.
        # Keep local text until that succeeds so concurrent coalesced flushes cannot
        # expose a disappearing/reappearing turn while the final write is delayed.
        self.partials.pop(item.id, None)
        self.queue_live()
        if self.closing:
            return
        # Generated speech is always unverified; do not append a delayed no-result status
        # after a result may already have arrived.
        if item.role == "user":
            snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(self.voice_id, include_briefing=False)
            activities = snapshot["activities"][-3:]
            clock = await sync_to_async(current_time, thread_sensitive=True)(self.voice_id)
            elapsed = []
            for activity in activities:
                result = await sync_to_async(current_time, thread_sensitive=True)(self.voice_id, activity["event_id"])
                elapsed.append(
                    {
                        "report": activity["description"][:160],
                        "reported_at_utc": activity["reported_at_utc"],
                        "elapsed_since_report_seconds": result["elapsed_since_report_seconds"],
                    }
                )
            self.duplex_session.append_thinking(
                f"Server UTC now: {clock['now_utc']}. Elapsed since user reports "
                f"(not proof of physical activity): {elapsed}"
            )

    async def deliver_results(self, snapshot):
        for task in [*snapshot["delegations"], *snapshot.get("thread_updates", [])]:
            normal = task.get("execution_mode") == "normal_thread" or "update_id" in task
            key = task.get("update_id", f"{task['event_id']}:{task['status']}" if normal else task["event_id"])
            if key in self.announced:
                continue
            event = await sync_to_async(thread_update_event if normal else completion_event, thread_sensitive=True)(
                self.voice_id, task
            )
            if event is None or not await sync_to_async(claim_announcement, thread_sensitive=True)(event):
                continue
            # Silent authoritative context precedes correlated commentary. Neither proves
            # transmission, playback, or that generated speech follows the context.
            label = "Durable task status" if event.kind == "task_status" else "Verified background outcome"
            outcome = f"{label} for request {task['event_id']}: {event.text[:1500]}"
            self.duplex_session.append_thinking(outcome)
            self.duplex_session.append_commentary(outcome, delegation_id=task["event_id"])
            self.announced.add(key)

    async def watch_results(self):
        while True:
            try:
                snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(
                    self.voice_id, include_briefing=False
                )
                if snapshot["status"] != "active":
                    return
                await self.deliver_results(snapshot)
            except Exception as exc:
                logger.error("voice result delivery failed exception_type=%s", type(exc).__name__)
            await asyncio.sleep(1)


def watch_microphone(room, assistant, owner_identity):
    """RTC mute and unsubscribe use different argument order; invalidate only the owner's mic."""

    @room.on("track_muted")
    def on_track_muted(participant, publication):
        if participant.identity == owner_identity and publication.source == rtc.TrackSource.SOURCE_MICROPHONE:
            assistant.vad_ready = False
            assistant.invalidate_runtime(user_only=True)

    @room.on("track_unsubscribed")
    def on_track_unsubscribed(_track, publication, participant):
        if participant.identity == owner_identity and publication.source == rtc.TrackSource.SOURCE_MICROPHONE:
            assistant.vad_ready = False
            assistant.invalidate_runtime(user_only=True)


# One prewarmed job process (each loads Django and Silero), not one per CPU.
server = AgentServer(num_idle_processes=1)


@server.rtc_session(agent_name=settings.REALTIME_VOICE_AGENT_NAME)
async def voice_session(ctx: agents.JobContext):
    global _job_claimed
    if _job_claimed or not getattr(settings, "REALTIME_VOICE_ENABLED", False):
        ctx.shutdown(reason="voice disabled or local one-job limit reached")
        return
    # The local paid-test harness also caps the whole bridge launch at one job.
    cap_file = os.environ.get("ERGO_VOICE_CALL_CAP_FILE")
    if cap_file:
        try:
            fd = os.open(cap_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            ctx.shutdown(reason="one-shot call cap already used or unavailable")
            return
        os.close(fd)
        _job_claimed = True
    room = ctx.job.room.name
    prefix = settings.REALTIME_VOICE_ROOM_PREFIX
    if not prefix or not room.startswith(prefix):
        ctx.shutdown(reason="invalid room")
        return
    try:
        voice_id = str(uuid.UUID(room.removeprefix(prefix)))
    except ValueError:
        ctx.shutdown(reason="invalid room")
        return
    claim_id = uuid.uuid4().hex
    if not await sync_to_async(claim_bridge, thread_sensitive=True)(voice_id, claim_id):
        ctx.shutdown(reason="voice session not active, already claimed, or not allowed")
        return
    http_session = None
    try:
        snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(voice_id, include_briefing=True)
        await ctx.connect()
        started = time.monotonic()
        http_session = aiohttp.ClientSession()
        assistant = Assistant(voice_id, snapshot, http_session, claim_id=claim_id)
        session = AgentSession(vad=silero.VAD.load())
    except BaseException:
        if http_session is not None:
            await http_session.close()
        await sync_to_async(end_bridge_session, thread_sensitive=True)(voice_id, claim_id)
        ctx.shutdown(reason="voice initialization failed")
        raise
    session_closed = asyncio.Event()
    owner_identity = f"user-{snapshot['user_id']}"
    teardown_session = False
    teardown_room = False
    unexpected_close = False

    def microphone_live():
        participant = ctx.room.remote_participants.get(owner_identity)
        return bool(
            participant
            and any(
                publication.source == rtc.TrackSource.SOURCE_MICROPHONE
                and publication.subscribed
                and publication.track is not None
                and not publication.muted
                for publication in participant.track_publications.values()
            )
        )

    assistant.microphone_live = microphone_live

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        nonlocal unexpected_close
        if participant.identity == owner_identity and not teardown_room:
            unexpected_close = True
            assistant.vad_ready = False
            assistant.invalidate_runtime()
            session_closed.set()

    @ctx.room.on("disconnected")
    def on_room_disconnected(*_args):
        nonlocal unexpected_close
        if not teardown_room:
            unexpected_close = True
            assistant.vad_ready = False
            assistant.invalidate_runtime()
            session_closed.set()

    watch_microphone(ctx.room, assistant, owner_identity)

    @session.on("close")
    def on_close(_event):
        nonlocal unexpected_close
        if not teardown_session:
            unexpected_close = True
        assistant.stop()
        session_closed.set()

    @session.on("user_state_changed")
    def on_user_state(event):
        assistant.on_user_state(event)

    @session.on("agent_state_changed")
    def on_agent_state(event):
        assistant.on_agent_state(event)

    @session.on("conversation_item_added")
    def on_item(event):
        item = event.item
        if not isinstance(item, llm.ChatMessage) or item.role not in ("user", "assistant"):
            return
        if item.role == "assistant":
            logger.info("voice first_or_subsequent_assistant_turn_seconds=%.3f", time.monotonic() - started)
        assistant.queue_turn(item)

    standby_requested = False
    generation = None
    try:
        deadline = datetime.fromisoformat(snapshot["bridge_started_at"])
        remaining = max(0, (deadline - datetime.now(UTC)).total_seconds() + MAX_SECONDS)
        async with asyncio.timeout(remaining):
            await session.start(room=ctx.room, agent=assistant, record=False)
            while not session_closed.is_set():
                snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(voice_id, include_briefing=False)
                if snapshot["status"] != "active":
                    break
                if (
                    snapshot["standby_state"] == "requested"
                    and microphone_live()
                    and assistant.safe_to_standby(snapshot)
                ):
                    generation = assistant.activity_generation
                    reserved = await sync_to_async(begin_standby_close, thread_sensitive=True)(voice_id, claim_id)
                    if reserved:
                        # A speech callback or a disconnect may have raced the database claim.
                        if (
                            not session_closed.is_set()
                            and not unexpected_close
                            and microphone_live()
                            and generation == assistant.activity_generation
                            and assistant.safe_to_standby(snapshot)
                        ):
                            standby_requested = True
                            break
                        await sync_to_async(cancel_standby_close, thread_sensitive=True)(voice_id, claim_id)
                try:
                    await asyncio.wait_for(session_closed.wait(), timeout=0.2)
                except TimeoutError:
                    pass
    except TimeoutError:
        pass
    finally:
        closed_cleanly = False
        try:
            assistant.stop()
            if assistant.watcher:
                assistant.watcher.cancel()
                await asyncio.gather(assistant.watcher, return_exceptions=True)
            if assistant.jobs:
                await asyncio.gather(*assistant.jobs, return_exceptions=True)
            teardown_session = True
            await session.aclose()
            await assistant.finish_live()
            await http_session.close()
            teardown_room = True
            await ctx.room.disconnect()
            if assistant.pending_id:
                await sync_to_async(close_pending, thread_sensitive=True)(voice_id, assistant.pending_id)
            # Once teardown has started it cannot be reversed. Preserve the logical
            # session, but remember that boundary audio may have been interrupted.
            if standby_requested and not unexpected_close:
                closed_cleanly = await sync_to_async(finish_standby_close, thread_sensitive=True)(
                    voice_id, claim_id, audio_uncertain=generation != assistant.activity_generation
                )
        finally:
            await assistant.finish_live()
            if not closed_cleanly:
                await sync_to_async(end_bridge_session, thread_sensitive=True)(voice_id, claim_id)
            seconds = time.monotonic() - started
            logger.info(
                "voice bridge wall_seconds=%.3f provider_usage_seconds=%s reconnect_latency_seconds=%s",
                seconds,
                assistant.provider_usage_seconds,
                assistant.reconnect_latency_seconds,
            )
            ctx.shutdown(reason="voice bridge closed")


if __name__ == "__main__":
    agents.cli.run_app(server)
