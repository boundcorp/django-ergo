"""Isolated LiveKit GPT-Live worker for an explicitly enabled local voice slice."""

import asyncio
import hashlib
import logging
import os
import time
import uuid

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")

import django

django.setup()

import aiohttp  # noqa: E402
from asgiref.sync import sync_to_async  # noqa: E402
from django.conf import settings  # noqa: E402
from livekit import agents  # noqa: E402
from livekit.agents import Agent, AgentServer, AgentSession, APIConnectOptions, llm  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402

from ergonaut.api.realtime_voice import bridge_state, current_time, delegate, record_event  # noqa: E402
from ergonaut.realtime_voice_outbox import claim_announcement, completion_event  # noqa: E402

logger = logging.getLogger(__name__)
MAX_SECONDS = 90
_job_claimed = False


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
    for event in snapshot["events"][-40:]:
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
    return context


class Assistant(Agent):
    def __init__(self, voice_id, snapshot, http_session):
        self.voice_id = voice_id
        self.announced = set()
        self.jobs = set()
        self.watcher = None
        super().__init__(
            instructions=(
                "Maintain session context across topics. You are the voice interface, not the "
                "background assistant. To check stored conversation history or perform requested "
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

    def spawn(self, coroutine, *, delegation_id=None):
        task = asyncio.create_task(self.run_job(coroutine, delegation_id=delegation_id))
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def run_job(self, coroutine, *, delegation_id=None):
        try:
            await coroutine
        except Exception as exc:
            # Consume async callback exceptions without logging payloads, credentials or transcripts.
            logger.error("voice bridge callback failed exception_type=%s", type(exc).__name__)
            notice = "A voice bridge action could not be confirmed. No successful background work is established."
            try:
                await sync_to_async(record_event, thread_sensitive=True)(
                    self.voice_id, f"bridge-error:{uuid.uuid4().hex}", "background_status", notice
                )
                self.duplex_session.append_commentary(notice, delegation_id=delegation_id)
            except Exception as notice_exc:
                logger.error("voice bridge status failed exception_type=%s", type(notice_exc).__name__)

    async def on_enter(self):
        self.duplex_session.on("delegation_created", self.on_delegation)
        self.duplex_session.on("openai_server_event_received", self.on_protocol_event)
        self.duplex_session.on("openai_client_event_queued", self.on_client_event)
        logger.info("voice delegation_listener_ready")
        self.watcher = asyncio.create_task(self.watch_results())

    def on_protocol_event(self, event):
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

    def on_delegation(self, event):
        logger.info("voice delegation_signal_received correlation=%s", self.correlation(event.id))
        self.spawn(self.queue_delegation(event), delegation_id=event.id)

    async def persist_turn(self, item):
        delivery = "interrupted" if item.interrupted else "unknown"
        await sync_to_async(record_event, thread_sensitive=True)(
            self.voice_id, item.id, item.role, item.text_content, delivery
        )
        if item.role == "assistant":
            snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(self.voice_id)
            if not any(task["status"] == "answered" and task["result"] for task in snapshot["delegations"]):
                await sync_to_async(record_event, thread_sensitive=True)(
                    self.voice_id,
                    f"work-status:{item.id}",
                    "background_status",
                    "No verified background result exists for this speech. Assistant claims are not lookup evidence.",
                )
        if item.role == "user":
            snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(self.voice_id)
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

    async def queue_delegation(self, event):
        # SDK client delegation carries an opaque ID, NOT a task/tool call. pending_transcript
        # is only the still-open speech segment and may be empty after speech ends.
        await sync_to_async(record_event, thread_sensitive=True)(
            self.voice_id, f"signal:{event.id}", "delegation_signal", "Client delegation signal received."
        )
        texts = [
            item.text_content
            for item in self.chat_ctx.items[-40:]
            if (
                isinstance(item, llm.ChatMessage)
                and item.role == "user"
                and item.text_content
                and item.id != "saved-voice-state"
                and not item.id.startswith("verified-result:")
            )
        ]
        pending = (event.pending_transcript or "").strip()
        if pending and (not texts or texts[-1].strip() != pending):
            texts.append(pending)
        text = "\n".join(texts)[-4000:].strip()
        if not text:
            await sync_to_async(record_event, thread_sensitive=True)(
                self.voice_id,
                f"submission:{event.id}",
                "background_status",
                "No background request submitted: the delegation signal contained no user context.",
            )
            self.duplex_session.append_commentary("Please clarify the background request.", delegation_id=event.id)
            return
        try:
            message = await sync_to_async(delegate, thread_sensitive=True)(self.voice_id, event.id, text)
        except ValueError:
            await sync_to_async(record_event, thread_sensitive=True)(
                self.voice_id,
                f"submission:{event.id}",
                "background_status",
                "Background submission was refused; no successful background work is established.",
            )
            self.duplex_session.append_commentary(
                "Background work is unavailable for this session. No successful lookup is established.",
                delegation_id=event.id,
            )
            return
        if message.status == "failed":
            # The watcher records and announces the failure through the outbox.
            return
        self.duplex_session.append_commentary(
            "The background request was submitted; it may not have started yet.", delegation_id=event.id
        )

    async def deliver_results(self, snapshot):
        for task in snapshot["delegations"]:
            key = task["event_id"]
            if key in self.announced:
                continue
            event = await sync_to_async(completion_event, thread_sensitive=True)(self.voice_id, task)
            if event is None or not await sync_to_async(claim_announcement, thread_sensitive=True)(event):
                continue
            # The durable claim is at-most-once, not proof of audio playback.
            # The result remains available in the session even if this send fails.
            self.duplex_session.append_commentary(
                f"Verified background outcome for request {key}: {event.text[:1500]}", delegation_id=key
            )
            self.announced.add(key)

    async def watch_results(self):
        while True:
            try:
                snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(self.voice_id)
                if snapshot["status"] != "active":
                    return
                await self.deliver_results(snapshot)
            except Exception as exc:
                logger.error("voice result delivery failed exception_type=%s", type(exc).__name__)
            await asyncio.sleep(1)


server = AgentServer()


@server.rtc_session(agent_name=settings.REALTIME_VOICE_AGENT_NAME)
async def voice_session(ctx: agents.JobContext):
    global _job_claimed
    if _job_claimed or not getattr(settings, "REALTIME_VOICE_ENABLED", False):
        ctx.shutdown(reason="voice disabled or local one-job limit reached")
        return
    cap_file = os.environ.get("ERGO_VOICE_CALL_CAP_FILE")
    if not cap_file:
        ctx.shutdown(reason="a unique one-shot call cap file is required")
        return
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
    voice_id = room.removeprefix(prefix)
    snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(voice_id)
    if snapshot["status"] != "active":
        ctx.shutdown(reason="voice session is not active")
        return
    await ctx.connect()
    started = time.monotonic()
    http_session = aiohttp.ClientSession()
    assistant = Assistant(voice_id, snapshot, http_session)
    session = AgentSession(vad=silero.VAD.load())

    @session.on("conversation_item_added")
    def on_item(event):
        item = event.item
        if not isinstance(item, llm.ChatMessage) or item.role not in ("user", "assistant") or not item.text_content:
            return
        if item.role == "assistant":
            logger.info("voice first_or_subsequent_assistant_turn_seconds=%.3f", time.monotonic() - started)
        assistant.spawn(assistant.persist_turn(item))

    try:
        async with asyncio.timeout(MAX_SECONDS):
            await session.start(room=ctx.room, agent=assistant, record=False)
            await asyncio.Event().wait()
    except TimeoutError:
        pass
    finally:
        await session.aclose()
        await http_session.close()
        if assistant.watcher:
            assistant.watcher.cancel()
            await asyncio.gather(assistant.watcher, return_exceptions=True)
        if assistant.jobs:
            await asyncio.gather(*assistant.jobs, return_exceptions=True)
        seconds = time.monotonic() - started
        logger.info(
            "voice bridge session_seconds=%.3f max_seconds=%d voice_cost_upper_estimate_usd=%.4f",
            seconds,
            MAX_SECONDS,
            seconds * 0.05 / 60,
        )
        ctx.shutdown(reason="local voice duration limit")


if __name__ == "__main__":
    agents.cli.run_app(server)
