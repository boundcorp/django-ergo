"""Isolated LiveKit GPT-Live worker for an explicitly enabled local voice slice."""

import asyncio
import logging
import os
import time

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
from ergonaut.realtime_voice_outbox import completion_event  # noqa: E402

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
    context.add_message(role="user", content=f"Saved session state (authoritative): {snapshot['state']}")
    for event in snapshot["events"][-40:]:
        if event["kind"] == "user":
            context.add_message(role="user", content=event["text"][:2000])
        elif event["kind"] == "assistant":
            context.add_message(
                role="assistant", content=f"[Generated speech; delivery {event['delivery']}] {event['text'][:2000]}"
            )
    for task in snapshot["delegations"]:
        if task["status"] == "answered" and task["result"]:
            context.add_message(
                role="user", content=f"Verified background result for {task['event_id']}: {task['result'][:1500]}"
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
                "You are Ergonaut's conversational voice assistant. Preserve supplied "
                "session context across topics. Delegate background work, say it is "
                "underway, and keep listening. Report completion only when given a "
                "verified result. Speech marked unknown or interrupted may not have been heard."
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

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

    async def on_enter(self):
        self.duplex_session.on("delegation_created", self.on_delegation)
        self.watcher = asyncio.create_task(self.watch_results())

    def on_delegation(self, event):
        self.spawn(self.queue_delegation(event))

    async def persist_turn(self, item):
        delivery = "interrupted" if item.interrupted else "unknown"
        await sync_to_async(record_event, thread_sensitive=True)(
            self.voice_id, item.id, item.role, item.text_content, delivery
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
        text = (event.pending_transcript or "").strip()
        if not text:
            self.duplex_session.append_commentary("Please clarify the background request.", delegation_id=event.id)
            return
        await sync_to_async(delegate, thread_sensitive=True)(self.voice_id, event.id, text)
        self.duplex_session.append_commentary(
            "The background request is queued. Continue the conversation while it runs.", delegation_id=event.id
        )

    async def deliver_results(self, snapshot):
        for task in snapshot["delegations"]:
            key = task["event_id"]
            if key in self.announced:
                continue
            event = await sync_to_async(completion_event, thread_sensitive=True)(self.voice_id, task)
            if event is None or event.delivery == "delivered":
                continue
            # A commentary submission is not a playback acknowledgement. Keep the
            # durable record pending and retry on the next bridge connection.
            self.duplex_session.append_commentary(f"Verified background outcome for request {key}: {event.text[:1500]}")
            self.announced.add(key)

    async def watch_results(self):
        while True:
            try:
                snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(self.voice_id)
                if snapshot["status"] != "active":
                    return
                await self.deliver_results(snapshot)
            except Exception:
                logger.exception("Could not deliver voice background result; will retry")
            await asyncio.sleep(1)


server = AgentServer()


@server.rtc_session(agent_name="ergo-voice")
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
    await ctx.connect()
    room = ctx.room.name
    if not room.startswith("voice-"):
        ctx.shutdown(reason="invalid room")
        return
    voice_id = room.removeprefix("voice-")
    snapshot = await sync_to_async(bridge_state, thread_sensitive=True)(voice_id)
    if snapshot["status"] != "active":
        ctx.shutdown(reason="voice session is not active")
        return
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
            await session.start(room=ctx.room, agent=assistant)
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
