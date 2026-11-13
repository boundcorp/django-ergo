"""Authenticated realtime voice sessions and durable bridge state."""

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django_ergo.bots import messaging
from django_ergo.conversation.budget import TurnBudget, attach
from django_ergo.conversation.context import MessageContextSource
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.identity import bot_identity, django_user_identity
from django_ergo.conversation.messages import add_message
from django_ergo.conversation.models import ConversationSession
from ninja import Router, Schema
from ninja.errors import HttpError

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import get_bot, uuid_or_404, visible_sessions
from ergonaut.api.realtime_voice_state import apply_user_event, current_time
from ergonaut.apps.bots.models import RealtimeVoiceDelegation, RealtimeVoiceEvent, RealtimeVoiceSession
from ergonaut.realtime_voice_execution import (
    NORMAL_RESTRICTION,
    initial_state,
    session_authorization,
    thread_updates,
)

router = Router(tags=["realtime voice"], auth=user_auth)


def voice_allowed(user):
    """The kill switch is on and the user is on the REALTIME_VOICE_USERS allowlist."""
    return bool(getattr(settings, "REALTIME_VOICE_ENABLED", False)) and user.get_username() in getattr(
        settings, "REALTIME_VOICE_USERS", set()
    )


def standby_allowed(user):
    return voice_allowed(user) and bool(getattr(settings, "REALTIME_VOICE_STANDBY_ENABLED", False))


def require_standby(user):
    if not standby_allowed(user):
        raise HttpError(404, "Realtime voice standby is disabled")


def require_enabled(user):
    if not voice_allowed(user):
        raise HttpError(404, "Realtime voice is disabled")


def owned_session(user, session_id):
    require_enabled(user)
    row = (
        RealtimeVoiceSession.objects.select_related("chat")
        .filter(id=uuid_or_404(session_id), user=user, chat__in=visible_sessions(user))
        .first()
    )
    if row is None:
        raise HttpError(404, "No such voice session")
    get_bot(row.chat.bot_name, user)
    return row


def event_out(event):
    def iso(value):
        return value.astimezone(UTC).isoformat() if value else None

    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "text": event.text,
        "delivery": event.delivery,
        "created_at": iso(event.created_at),
        "received_at": iso(event.received_at),
        "processed_at": iso(event.processed_at),
        "client_event_at": iso(event.client_event_at),
    }


# A bridge job (one GPT-Live connection) lasts at most this long; the bridge enforces it.
BRIDGE_JOB_SECONDS = 90

LIVE_STATE_KEY = "_voice_live"
MAX_PARTIAL_TURNS = 4
MAX_TRANSCRIPT_CHARS = 4000
UNKNOWN_RUNTIME = {"user_state": "unknown", "agent_state": "unknown", "observed_at": None}


def clear_live_state(session):
    session.state = {key: value for key, value in session.state.items() if key != LIVE_STATE_KEY}


def publish_live_state(voice_id, claim_id, partials, runtime, pending_id=None, fragments=None):
    """Coalesced, claim-scoped transient text; never normal thread history or delivery evidence."""
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().select_related("user").get(pk=voice_id)
        if (
            not voice_allowed(session.user)
            or not claim_id
            or not session.bridge_started_at
            or session.status != "active"
            or session.bridge_claim_id != claim_id
            or session.standby_state not in ("live", "requested")
            or deadline_expired(session)
        ):
            return False
        partials = partials[-MAX_PARTIAL_TURNS:]
        final_ids = set(
            session.events.filter(event_id__in=[p["turn_id"] for p in partials]).values_list("event_id", flat=True)
        )
        drafts = [
            {**p, "text": p["text"][:MAX_TRANSCRIPT_CHARS]}
            for p in partials
            if p["turn_id"] not in final_ids
            and p["role"] in ("user", "assistant")
            and p["text"]
            and 0 < len(p["turn_id"]) <= 200
        ]
        state = {**session.state, LIVE_STATE_KEY: {"claim_id": claim_id, "partials": drafts, "runtime": dict(runtime)}}
        pending = state.get("pending_delegation")
        if pending_id and pending and pending["id"] == pending_id and pending["status"] == "collecting":
            state["pending_delegation"] = {
                **pending,
                "fragments": [{"id": key, "text": value} for key, value in (fragments or {}).items()],
            }
        if state != session.state:
            session.state = state
            session.save(update_fields=["state"])
        return True


def end_voice_session(voice_id):
    """End an active session: Stop, or its bridge job finishing (timeout, disconnect, error)."""
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().filter(pk=voice_id, status="active").first()
        if session:
            clear_live_state(session)
            session.status, session.ended_at = "ended", timezone.now()
            session.save(update_fields=["state", "status", "ended_at"])


def end_bridge_session(voice_id, claim_id):
    """A stale job cannot end standby or a later bridge's claim."""
    with transaction.atomic():
        session = (
            RealtimeVoiceSession.objects.select_for_update()
            .filter(pk=voice_id, status="active", bridge_claim_id=claim_id)
            .exclude(standby_state__in=("standby", "resuming"))
            .first()
        )
        if session:
            clear_live_state(session)
            session.status, session.ended_at = "ended", timezone.now()
            session.save(update_fields=["state", "status", "ended_at"])


def deadline_expired(session, now=None):
    return bool(
        session.bridge_started_at
        and (now or timezone.now()) >= session.bridge_started_at + timedelta(seconds=BRIDGE_JOB_SECONDS)
    )


def claim_bridge(voice_id, claim_id=None):
    """Atomically claim the initial job or one explicitly resumed job."""
    if not getattr(settings, "REALTIME_VOICE_ENABLED", False):
        return False
    field = get_user_model().USERNAME_FIELD
    with transaction.atomic():
        session = (
            RealtimeVoiceSession.objects.select_for_update()
            .filter(
                pk=voice_id,
                status="active",
                **{f"user__{field}__in": getattr(settings, "REALTIME_VOICE_USERS", set())},
            )
            .first()
        )
        if session is None:
            return False
        now = timezone.now()
        if deadline_expired(session, now):
            session.status = "ended"
            session.ended_at = now
            clear_live_state(session)
            session.save(update_fields=["state", "status", "ended_at"])
            return False
        session.bridge_claim_id = claim_id or uuid.uuid4().hex
        clear_live_state(session)
        if session.bridge_started_at is None:
            session.bridge_started_at = now
            session.save(update_fields=["state", "bridge_started_at", "bridge_claim_id"])
            return True
        if not standby_allowed(session.user) or session.standby_state != "resuming":
            return False
        session.standby_state = "live"
        session.save(update_fields=["state", "standby_state", "bridge_claim_id"])
        return True


def begin_standby_close(voice_id, claim_id):
    """Reserve teardown; only the running bridge may acknowledge the request."""
    if not getattr(settings, "REALTIME_VOICE_STANDBY_ENABLED", False) or not getattr(
        settings, "REALTIME_VOICE_ENABLED", False
    ):
        return False
    field = get_user_model().USERNAME_FIELD
    return bool(
        RealtimeVoiceSession.objects.filter(
            pk=voice_id,
            status="active",
            standby_state="requested",
            bridge_claim_id=claim_id,
            bridge_started_at__gt=timezone.now() - timedelta(seconds=BRIDGE_JOB_SECONDS),
            **{f"user__{field}__in": getattr(settings, "REALTIME_VOICE_USERS", set())},
        ).update(standby_state="closing")
    )


def cancel_standby_close(voice_id, claim_id):
    RealtimeVoiceSession.objects.filter(
        pk=voice_id, status="active", standby_state="closing", bridge_claim_id=claim_id
    ).update(standby_state="requested")


def finish_standby_close(voice_id, claim_id, *, audio_uncertain=False):
    """Publish standby after teardown, preserving uncertainty about boundary audio."""
    if not getattr(settings, "REALTIME_VOICE_STANDBY_ENABLED", False) or not getattr(
        settings, "REALTIME_VOICE_ENABLED", False
    ):
        return False
    now = timezone.now()
    field = get_user_model().USERNAME_FIELD
    with transaction.atomic():
        session = (
            RealtimeVoiceSession.objects.select_for_update()
            .filter(
                pk=voice_id,
                status="active",
                standby_state="closing",
                bridge_claim_id=claim_id,
                bridge_started_at__gt=now - timedelta(seconds=BRIDGE_JOB_SECONDS),
                **{f"user__{field}__in": getattr(settings, "REALTIME_VOICE_USERS", set())},
            )
            .first()
        )
        if session is None:
            return False
        session.standby_state = "standby"
        session.standby_at = now
        clear_live_state(session)
        if audio_uncertain:
            session.state = {**session.state, "standby_audio_uncertain": True}
        session.save(update_fields=["state", "standby_state", "standby_at"])
        return True


def bridge_state(voice_id, *, include_briefing=True):
    """Durable snapshot; only bridge startup/reconnect should render the full briefing."""
    session = RealtimeVoiceSession.objects.select_related("chat", "user").get(pk=voice_id)
    events = list(session.events.order_by("-id")[:100])
    delegations = list(session.delegations.select_related("message").order_by("-id")[:100])
    delegations.reverse()
    state = {"decisions": [], "constraints": [], "current_step": "", **session.state}
    live = state.pop(LIVE_STATE_KEY, {})
    has_live_bridge = (
        session.status == "active"
        and bool(session.bridge_started_at and session.bridge_claim_id)
        and session.standby_state in ("live", "requested")
        and live.get("claim_id") == session.bridge_claim_id
        and not deadline_expired(session)
    )
    transcript = [
        {
            "turn_id": e.event_id,
            "event_id": e.event_id,
            "role": "assistant" if e.kind == "assistant" else "user",
            "text": e.text,
            "is_final": True,
            "delivery": e.delivery,
            "created_at": e.created_at.astimezone(UTC).isoformat(),
        }
        for e in reversed(events)
        if e.kind in ("user", "user_transcript", "assistant")
    ]
    if has_live_bridge:
        final_ids = {e.event_id for e in events}
        transcript.extend(p for p in live.get("partials", []) if p["turn_id"] not in final_ids)
    transcript.sort(key=lambda turn: turn["created_at"])
    pending = state.get("pending_delegation")
    if pending and pending["status"] == "collecting":
        if session.status != "active":
            state["pending_delegation"] = {**pending, "status": "cancelled"}
        elif timezone.now() >= datetime.fromisoformat(pending["expires_at"]):
            state["pending_delegation"] = {**pending, "status": "expired"}
    state["pending_task_ids"] = list(
        session.delegations.filter(message__status__in=("queued", "delivered", "waiting"))
        .order_by("-id")
        .values_list("event_id", flat=True)[:100]
    )
    state["completed_task_ids"] = list(
        session.delegations.filter(message__status="answered").order_by("-id").values_list("event_id", flat=True)[:100]
    )
    activities = [
        {
            "event_id": a.event.event_id,
            "description": a.description,
            "reported_at_utc": a.event.received_at.astimezone(UTC).isoformat() if a.event.received_at else None,
        }
        for a in reversed(list(session.activities.select_related("event").order_by("-id")[:50]))
    ]
    return {
        "session_id": str(session.id),
        "chat_id": str(session.chat_id),
        "user_id": str(session.user_id),
        "status": session.status,
        "standby_state": session.standby_state,
        "bridge_started_at": session.bridge_started_at.isoformat() if session.bridge_started_at else None,
        "resumed_at": session.resumed_at.isoformat() if session.resumed_at else None,
        "state": state,
        "events": [event_out(e) for e in reversed(events)],
        "transcript": transcript,
        "runtime": live.get("runtime", UNKNOWN_RUNTIME) if has_live_bridge else dict(UNKNOWN_RUNTIME),
        "activities": activities,
        "briefing": startup_briefing(session) if include_briefing else delegation_status(session),
        "thread_updates": thread_updates(session, delegations),
        "delegations": [
            {
                "event_id": d.event_id,
                "text": d.text,
                "status": d.message.status,
                "result": d.message.reply_text[:4000],
                "error": d.message.error[:4000],
                "execution_mode": (d.message.metadata or {}).get("voice_execution_mode", "read_only"),
            }
            for d in delegations
        ],
    }


def persist_thread_event(session, event):
    """Write a finalized voice event once to the same thread, preserving delivery uncertainty."""
    if event.kind not in ("user", "user_transcript", "assistant"):
        return
    chat = ConversationSession.objects.select_for_update().select_related("user").get(pk=session.chat_id)
    if chat.user_id != session.user_id:
        raise ValueError("Voice session owner does not own the launch chat")
    provenance = {
        "kind": "message",
        "voice_session_id": str(session.id),
        "voice_event_id": event.event_id,
        "voice_delivery": event.delivery,
        "reported_at": (event.client_event_at or event.received_at).isoformat(),
    }
    text = event.text
    role = "assistant" if event.kind == "assistant" else "user"
    if role == "assistant":
        text = f"[Generated voice speech; delivery {event.delivery}] {text}"
    message = chat.messages.filter(
        provenance__voice_session_id=str(session.id), provenance__voice_event_id=event.event_id
    ).first()
    if message:
        message.provenance = provenance
        message.save(update_fields=["provenance"])
        message.content_blocks.filter(block_type="text").update(text=text)
    else:
        message = async_to_sync(add_message)(
            chat,
            role,
            [{"block_type": "text", "text": text}],
            author=bot_identity(chat) if role == "assistant" else django_user_identity(chat.user),
            provenance=provenance,
        )
        chat.messages.filter(pk=message.pk).update(created_at=event.received_at)
    if role == "user":
        prior = (chat.metadata or {}).get("voice_state", {})
        state = {"current_step": session.state.get("current_step") or prior.get("current_step", "")}
        for key in ("decisions", "constraints", "completed_steps"):
            values = list(dict.fromkeys([*prior.get(key, []), *session.state.get(key, [])]))
            state[key] = values[-10:]
        chat.metadata = {**(chat.metadata or {}), "voice_state": state}
        chat.save(update_fields=["metadata", "updated_at"])


def record_event(
    voice_id,
    event_id,
    kind,
    text,
    delivery="unknown",
    client_event_at=None,
    *,
    trusted_delivery=False,
    bridge_claim_id=None,
    received_at=None,
    turn_started_at=None,
):
    received_at = received_at or timezone.now()
    if (
        not event_id
        or len(event_id) > 200
        or not kind
        or len(kind) > 40
        or len(text) > 4000
        or delivery not in ("unknown", "interrupted", "delivered")
    ):
        raise ValueError("Invalid voice event")
    if client_event_at is not None and (
        not isinstance(client_event_at, datetime) or timezone.is_naive(client_event_at)
    ):
        raise ValueError("Client event time must include a timezone")
    if turn_started_at is not None and (
        not isinstance(turn_started_at, datetime) or timezone.is_naive(turn_started_at)
    ):
        raise ValueError("Turn start time must include a timezone")
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        if bridge_claim_id is not None:
            # An accepted final may flush after Stop, but callbacks from an old claim
            # or after the deadline cannot resurrect a session or change its history.
            if (
                session.bridge_claim_id != bridge_claim_id
                or not bridge_claim_id
                or not session.bridge_started_at
                or not voice_allowed(session.user)
                or deadline_expired(session, received_at)
                or session.standby_state in ("standby", "resuming")
                or (session.status != "active" and (not session.ended_at or received_at > session.ended_at))
            ):
                return None
        old = session.events.filter(event_id=event_id).first()
        live = session.state.get(LIVE_STATE_KEY)
        draft = next((p for p in (live or {}).get("partials", []) if p["turn_id"] == event_id), None)
        if draft:
            # Server-observed first text, not final receipt or evidence of playback.
            turn_started_at = datetime.fromisoformat(draft["created_at"])
            session.state = {
                **session.state,
                LIVE_STATE_KEY: {**live, "partials": [p for p in live["partials"] if p["turn_id"] != event_id]},
            }
            session.save(update_fields=["state"])
        if old:
            if (old.kind, old.text, old.client_event_at) != (kind, text, client_event_at):
                raise ValueError("Event ID already used")
            if delivery != old.delivery and delivery != "unknown":
                if not trusted_delivery or old.delivery == "delivered":
                    raise ValueError("Event ID already used")
                old.delivery = delivery
                old.save(update_fields=["delivery"])
                persist_thread_event(session, old)
            return old
        if session.status != "active" and bridge_claim_id is None:
            raise ValueError("Voice session ended")
        event = RealtimeVoiceEvent.objects.create(
            session=session,
            event_id=event_id,
            kind=kind,
            text=text,
            delivery=delivery,
            received_at=received_at,
            processed_at=timezone.now(),
            client_event_at=client_event_at,
        )
        if turn_started_at is not None and kind in ("user", "user_transcript", "assistant"):
            # auto_now_add records final receipt by default. Preserve a published
            # draft's sort timestamp while received_at/processed_at retain final evidence.
            event.created_at = turn_started_at
            event.save(update_fields=["created_at"])
        if kind in ("user", "user_transcript"):
            apply_user_event(session, event)
        persist_thread_event(session, event)
        return event


def delegation_budget():
    """The hard per-request budget every voice delegation runs under.

    ``REALTIME_VOICE_DELEGATION_BUDGET`` (a mapping, see ``TurnBudget.from_dict`` plus
    ``start_within_seconds``) is required: with none, or an invalid one, delegation is off.
    """
    config = getattr(settings, "REALTIME_VOICE_DELEGATION_BUDGET", None)
    if not config:
        raise ValueError("Background delegation is disabled: no enforced request budget is configured")
    config = dict(config)
    start_within = config.pop("start_within_seconds", config.get("deadline_seconds"))
    if isinstance(start_within, bool) or not isinstance(start_within, int | float) or start_within <= 0:
        raise ValueError("Background delegation budget needs a positive start_within_seconds")
    return TurnBudget.from_dict({**config, "expires_at": timezone.now().timestamp() + start_within})


# A configured budget can narrow this ceiling, never authorize writes or new inference.
VOICE_MODEL = "gpt-6-luna"
VOICE_READ_TOOLS = frozenset(
    {
        "ergo_chat_history_sources",
        "ergo_chat_history_read",
        "ergo_chat_history_search",
        "ergo_bot_list",
        "ergo_thread_list",
        "ergo_worker_list",
    }
)
BRIEFING_CONTEXT_CHARS = 16000
VOICE_RESTRICTION = (
    "Restricted read-only mode: one budgeted gpt-6-luna request at most; no retries, writes, "
    "worker starts, thread forwarding/sends, or approvals. Only this chat's authorized read tools are available."
)


def normal_target(session):
    authorization = session_authorization(session)
    if authorization:
        if not voice_allowed(session.user):
            raise ValueError("Normal thread execution is disabled for this owner.")
        get_bot(session.chat.bot_name, session.user)  # normal registered bot access, not a global grant
    return authorization


def delegation_target(session, budget):
    """Check the actual selected chat, without changing its route or making a model call."""
    if session.chat.user_id != session.user_id:
        raise ValueError("Background delegation unavailable: the selected chat is not owned by this user.")
    if budget.model != VOICE_MODEL:
        raise ValueError("Background delegation unavailable: the request budget must pin gpt-6-luna.")
    if budget.allowed_tools - VOICE_READ_TOOLS:
        raise ValueError("Background delegation unavailable: the budget contains tools outside the read-only ceiling.")
    bot = get_bot(session.chat.bot_name, session.user)
    engine = bot.make_engine(session.chat)
    if not getattr(engine, "supports_request_limits", False):
        raise ValueError(
            "Background delegation unavailable: this chat's engine cannot enforce per-request limits "
            "(subscription/CLI routes are not supported). A dedicated budgeted API-engine profile "
            "preserving this thread is required."
        )
    if getattr(engine, "model", "") != VOICE_MODEL:
        raise ValueError(
            "Background delegation unavailable: this chat is not on the pinned gpt-6-luna model. "
            "A dedicated budgeted API-engine profile preserving this thread is required; voice will not reroute it."
        )
    # Only tools already offered by this chat survive; do not load skills or persist metadata.
    skills = bot.skillset(session.chat)
    offered = {
        name
        for skill in skills.active
        for name in set(skills.tool_names(skill)) & budget.allowed_tools
        if not skills.requires_approval(name)
    }
    return replace(budget, allowed_tools=budget.allowed_tools & offered)


def persisted_voice_event_ids(session):
    return list(
        session.chat.messages.filter(provenance__voice_session_id=str(session.id))
        .order_by("-sequence")
        .values_list("provenance__voice_event_id", flat=True)[:100]
    )


def delegation_status(session):
    """Lightweight web/polling status, independent of the web process's budget settings."""
    restriction = NORMAL_RESTRICTION if (session.state or {}).get("normal_thread_authorization") else VOICE_RESTRICTION
    if session.status != "active":
        status = {
            "delegation_available": False,
            "restriction": f"{restriction} Background delegation unavailable: the voice session has ended.",
            "reason": "Background delegation unavailable: the voice session has ended.",
        }
    elif session.delegations.exists():
        status = {
            "delegation_available": False,
            "restriction": f"{restriction} This session's one background request is already used.",
            "reason": "This session's one background request is already used.",
        }
    else:
        status = (session.state or {}).get("voice_delegation") or {
            "delegation_available": None,
            "restriction": f"{restriction} Bridge has not validated delegation for this session.",
            "reason": "Bridge has not validated delegation for this session.",
        }
    return {
        "context": "",
        "execution_mode": "normal_thread" if (session.state or {}).get("normal_thread_authorization") else "read_only",
        **status,
    }


def startup_briefing(session):
    """The exact launch chat's bounded normal context, never a global work briefing."""
    instructions = ""
    context = (
        f"Stored launch-thread context, NOT lookup results. Voice continues this same thread.\n"
        f"User: {session.user.get_username()[:160]}. Bot: {session.chat.bot_name[:160]}. "
        f"Chat: {session.chat_id}.\n"
    )
    if session.chat.user_id != session.user_id:
        raise ValueError("Voice session owner does not own the launch chat")
    try:
        bot = get_bot(session.chat.bot_name, session.user)
        skills = bot.skillset(session.chat)
        builder = bot.context_builder(session.chat, "", skills, incoming=False)
        # The ordinary builder supplies identity, skills and the chat's configured sources.
        # Native history is supplied once below; the global orchestration overview is on-demand only.
        from django_ergo.bots.overview import OverviewSource

        builder.sources = [
            source for source in builder.sources if not isinstance(source, OverviewSource | MessageContextSource)
        ]
        builder.budget_tokens = min(builder.budget_tokens, 500)
        instructions = (
            session.chat.system_prompt
            if (session.chat.metadata or {}).get("custom_instructions")
            else bot.instructions_for(bot.chat_name(session.chat))
        )
        instructions = instructions[:8000]
        context += builder.build().text[:2200] + "\n"
    except Exception:  # noqa: BLE001 - a broken configured source must not erase saved launch-thread history
        context += "Configured bot context: not known; could not render this chat's context.\n"
    summary = session.chat.compactions.order_by("-created_at").first()
    if summary:
        context += "Durable compaction summary (bounded):\n" + summary.summary[:2000] + "\n"
    history = MessageContextSource(
        SessionSource(session.chat, last_rows=40),
        max_messages=40,
        granularity="conversation",
        title="Recent messages in this launch thread (including finalized voice)",
    ).render(1750)
    if history:
        context += history.render()[:7200] + "\n"
    state = (session.chat.metadata or {}).get("voice_state", {})
    context += "Durable reported decisions/current task:\n" + json.dumps(state, ensure_ascii=False)[:1500] + "\n"
    context += (
        f"This thread's open task state: "
        f"{session.chat.workers.filter(status__in=('queued', 'running')).count()} queued/running workers; "
        f"pending approval: {session.chat.structured_calls.filter(messaging.awaiting_approval()).exists()}; "
        f"running turn: {session.chat.structured_calls.filter(messaging.running_turn()).exists()}.\n"
    )
    attachments = list(session.chat.attachments.order_by("-updated_at").values("id", "filename", "kind")[:5])
    context += "This thread's attachment references (not file contents):\n"
    context += json.dumps(attachments, default=str, ensure_ascii=False)[:1000] + "\n"
    context += (
        "Cross-thread bots/workers/approvals and external PR state: not known; query only on demand "
        "through this chat's authorized read tools. Context may be truncated; use history tools for more."
    )
    authorization = None
    try:
        authorization = normal_target(session)
        if session.status != "active":
            raise ValueError("Background delegation unavailable: the voice session has ended.")
        if session.delegations.exists():
            raise ValueError(
                "Background delegation unavailable: this session's one background request is already used."
            )
        budget = None if authorization else delegation_target(session, delegation_budget())
    except ValueError as exc:
        available, reason = False, str(exc)
    except Exception:  # noqa: BLE001 - configuration errors must not disclose credentials or fail session startup
        available, reason = (
            False,
            "Background delegation unavailable: this chat's budgeted engine could not be validated.",
        )
    else:
        available = True
        reason = (
            NORMAL_RESTRICTION
            if authorization
            else "Background delegation is available. Offered read tools: "
            + (", ".join(sorted(budget.allowed_tools)) or "none")
            + "."
        )
    bot_name = session.chat.bot_name[:160]
    if not available:
        capability = (
            f"Delegation is unavailable in this call: {reason} If asked whether you have tools or can do "
            "something, say you cannot run background work in this call and give that reason."
        )
    elif authorization:
        capability = (
            f"Delegation is available: you have no tools of your own, but with client delegation you can hand "
            f"one request per call to {bot_name}, the bot that owns this thread. It runs a normal turn of that "
            "bot with its own tools (for example chat history, threads and workers) under its normal action "
            "approvals. Its answer is posted in this thread and returned to you here as a verified result to "
            f"speak. If asked whether you have tools or can do something, say yes: you can ask {bot_name} to do "
            "it, once per call. Never say you have no tools."
        )
    else:
        capability = (
            f"Delegation is available: you have no tools of your own, but with client delegation you can hand "
            f"one read-only request per call to {bot_name}, limited to these read tools: "
            f"{', '.join(sorted(budget.allowed_tools)) or 'none'}. Its answer is returned to you here as a "
            "verified result to speak. If asked whether you have tools, say you can run one read-only lookup "
            "this call; never say you have no tools."
        )
    base = NORMAL_RESTRICTION if authorization else VOICE_RESTRICTION
    briefing = {
        "context": context[:BRIEFING_CONTEXT_CHARS],
        "restriction": base if reason == base else f"{base} {reason}",
        "reason": reason,
        "capability": capability,
        "delegation_available": available,
        "execution_mode": "normal_thread" if authorization else "read_only",
        "instructions": instructions,
        "voice_event_ids": persisted_voice_event_ids(session),
    }
    # Only bridge settings establish capability; the web process has no budget.
    # Persist sanitized status, never the thread's instructions or rendered context.
    with transaction.atomic():
        locked = RealtimeVoiceSession.objects.select_for_update().get(pk=session.id)
        locked.state = {
            **locked.state,
            "voice_delegation": {
                "delegation_available": available,
                "execution_mode": briefing["execution_mode"],
                "restriction": briefing["restriction"],
                "reason": reason,
            },
        }
        locked.save(update_fields=["state"])
    return briefing


PENDING_SECONDS = 60


def begin_pending(voice_id, event_id, fragments):
    """Record one model-selected client signal while speech is being assembled."""
    if not event_id or len(event_id) > 180:
        raise ValueError("Invalid delegation ID")
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        state = dict(session.state)
        if session.status != "active" or state.get("pending_delegation") or session.delegations.exists():
            return
        state["pending_delegation"] = {
            "id": event_id,
            "status": "collecting",
            "expires_at": (timezone.now() + timedelta(seconds=PENDING_SECONDS)).isoformat(),
            "fragments": [{"id": key, "text": value} for key, value in fragments.items()],
        }
        session.state = state
        session.save(update_fields=["state"])
        record_event(voice_id, f"signal:{event_id}", "delegation_signal", "Client delegation signal received.")


def close_pending(voice_id, event_id, status="cancelled"):
    """Fail closed on shutdown or timeout; never cancel submitted work."""
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        state = dict(session.state)
        pending = state.get("pending_delegation")
        if pending and pending["id"] == event_id and pending["status"] == "collecting":
            pending["status"] = status
            state["pending_delegation"] = pending
            session.state = state
            session.save(update_fields=["state"])


def resolve_pending(voice_id, event_id, text):
    """Atomically submit assembled speech only while the session and signal remain live."""
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        state = dict(session.state)
        pending = state.get("pending_delegation")
        if (
            session.status != "active"
            or not pending
            or pending["id"] != event_id
            or pending["status"] != "collecting"
            or timezone.now() >= datetime.fromisoformat(pending["expires_at"])
        ):
            raise ValueError("Pending request expired or unavailable")
        if not text.strip() or len(text) > 4000:
            raise ValueError("Invalid assembled request")
        delegate(voice_id, event_id, text.strip())
        pending["status"] = "submitted"
        state["pending_delegation"] = pending
        session.state = state
        session.save(update_fields=["state"])


def delegate(voice_id, event_id, text):
    """Persist one idempotent exact-thread request under its separately authorized execution policy."""
    if not event_id or len(event_id) > 200 or not text.strip() or len(text) > 4000:
        raise ValueError("Invalid delegation")
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        old = session.delegations.select_related("message").filter(event_id=event_id).first()
        if old:
            if old.text != text:
                raise ValueError("Delegation ID already used")
            return old.message
        if session.delegations.exists():
            raise ValueError("This voice session permits one background request")
        if session.status != "active" or deadline_expired(session):
            raise ValueError("Voice session ended or deadline expired")
        try:
            authorization = normal_target(session)
            budget = None if authorization else delegation_target(session, delegation_budget())
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                "Background delegation unavailable: this chat's budgeted engine could not be validated."
            ) from exc
        context = bridge_state(session.id, include_briefing=False)
        saved_ids = set(persisted_voice_event_ids(session))
        prompt = (
            "Voice session context (assistant speech delivery may be unknown or interrupted):\n"
            + json.dumps(
                {
                    "state": context["state"],
                    "events": [event for event in context["events"] if event["event_id"] not in saved_ids],
                    "activities": context["activities"],
                    "delegations": context["delegations"],
                    "launch_chat_id": context["chat_id"],
                    "owner_id": context["user_id"],
                    "restriction": NORMAL_RESTRICTION if authorization else VOICE_RESTRICTION,
                },
                ensure_ascii=False,
            )
            + "\n\nCurrent delegated request:\n"
            + text
        )
        metadata = {
            "voice_session_id": str(session.id),
            "voice_event_id": event_id,
            "message_author": django_user_identity(session.user),
            "message_provenance": {"kind": "message", "voice_session_id": str(session.id)},
        }
        if authorization:
            metadata.update(
                {
                    "voice_execution_mode": "normal_thread",
                    "tool_execution_policy": "at_most_once",
                    "model_retry_policy": "never",
                    "normal_thread_authorization": authorization,
                }
            )
        else:
            metadata = attach({**metadata, "context_policy": "current_chat_only"}, budget)
        message = messaging.send(None, session.chat, prompt, metadata=metadata)
        RealtimeVoiceDelegation.objects.create(session=session, event_id=event_id, text=text, message=message)
    message.refresh_from_db()
    return message


def session_out(session):
    session.refresh_from_db()
    if (
        (standby_allowed(session.user) or session.standby_state != "live")
        and session.status == "active"
        and deadline_expired(session)
    ):
        end_voice_session(session.id)
        session.refresh_from_db()
    state = bridge_state(session.id, include_briefing=False)
    # Task completion is independent of a live provider. Owner polling can persist
    # terminal receipts after End, without dispatching, approving or reopening voice.
    from ergonaut.realtime_voice_outbox import completion_event, thread_update_event

    observed_ids = {event["event_id"] for event in state["events"]}
    for task in [*state["delegations"], *state["thread_updates"]]:
        if task["status"] not in ("answered", "failed"):
            continue
        event = (thread_update_event if "update_id" in task else completion_event)(session.pk, task)
        if event.event_id not in observed_ids:
            state["events"].append(event_out(event))
            observed_ids.add(event.event_id)
    state["events"] = sorted(state["events"], key=lambda event: event["created_at"])[-100:]
    standby_seconds = None
    if session.standby_at:
        stop = (
            session.resumed_at
            if session.resumed_at and session.resumed_at >= session.standby_at
            else session.ended_at or timezone.now()
        )
        standby_seconds = max(0.0, (stop - session.standby_at).total_seconds())
    return {
        "id": str(session.id),
        "chat_id": str(session.chat_id),
        "status": session.status,
        "standby_state": session.standby_state,
        "bridge_started_at": session.bridge_started_at.isoformat() if session.bridge_started_at else None,
        "standby_at": session.standby_at.isoformat() if session.standby_at else None,
        "resumed_at": session.resumed_at.isoformat() if session.resumed_at else None,
        "standby_seconds": standby_seconds,
        "created_at": session.created_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "events": state["events"],
        "transcript": state["transcript"],
        "runtime": state["runtime"],
        "state": state["state"],
        "delegations": state["delegations"],
        "thread_updates": state["thread_updates"],
        "activities": state["activities"],
        "briefing": state["briefing"],
    }


class CreateIn(Schema):
    chat_id: str


@router.get("/realtime-voice/config")
def config(request):
    return {"enabled": voice_allowed(request.auth), "standby_enabled": standby_allowed(request.auth)}


@router.get("/realtime-voice/sessions")
def list_voice_sessions(request, chat_id: str):
    require_enabled(request.auth)
    chat = visible_sessions(request.auth).filter(id=uuid_or_404(chat_id), user=request.auth).first()
    if chat is None:
        raise HttpError(404, "No such chat")
    get_bot(chat.bot_name, request.auth)
    return [
        session_out(row)
        for row in RealtimeVoiceSession.objects.filter(chat=chat, user=request.auth).order_by("-created_at")
    ]


@router.post("/realtime-voice/sessions")
def create_session(request, data: CreateIn):
    require_enabled(request.auth)
    chat = visible_sessions(request.auth).filter(id=uuid_or_404(data.chat_id)).first()
    if chat is None or chat.user_id != request.auth.pk:
        raise HttpError(404, "No such chat")
    get_bot(chat.bot_name, request.auth)
    if chat.status == "completed":
        raise HttpError(409, "Chat is closed")
    return session_out(
        RealtimeVoiceSession.objects.create(chat=chat, user=request.auth, state=initial_state(chat, request.auth))
    )


@router.get("/realtime-voice/sessions/{session_id}")
def get_voice_session(request, session_id: str):
    return session_out(owned_session(request.auth, session_id))


@router.get("/realtime-voice/sessions/{session_id}/time")
def voice_time(request, session_id: str, activity_id: str | None = None):
    session = owned_session(request.auth, session_id)
    return current_time(session.id, activity_id)


@router.post("/realtime-voice/sessions/{session_id}/end")
def end_session(request, session_id: str):
    session = owned_session(request.auth, session_id)
    end_voice_session(session.pk)
    session.refresh_from_db()
    return session_out(session)


@router.post("/realtime-voice/sessions/{session_id}/standby")
def request_standby(request, session_id: str):
    session = owned_session(request.auth, session_id)
    require_standby(request.auth)
    now = timezone.now()
    updated = RealtimeVoiceSession.objects.filter(
        pk=session.pk,
        status="active",
        standby_state="live",
        bridge_started_at__gt=now - timedelta(seconds=BRIDGE_JOB_SECONDS),
    ).update(standby_state="requested")
    if not updated:
        if deadline_expired(session, now):
            end_voice_session(session.pk)
        raise HttpError(409, "Standby unavailable")
    return session_out(session)


@router.post("/realtime-voice/sessions/{session_id}/resume")
def resume_session(request, session_id: str):
    session = owned_session(request.auth, session_id)
    require_standby(request.auth)
    now = timezone.now()
    updated = RealtimeVoiceSession.objects.filter(
        pk=session.pk,
        status="active",
        standby_state="standby",
        bridge_started_at__gt=now - timedelta(seconds=BRIDGE_JOB_SECONDS),
    ).update(standby_state="resuming", resumed_at=now)
    if not updated:
        if deadline_expired(session, now):
            end_voice_session(session.pk)
        raise HttpError(409, "Resume unavailable")
    return session_out(session)


@router.post("/realtime-voice/sessions/{session_id}/token")
def token(request, session_id: str):
    session = owned_session(request.auth, session_id)
    if session.status != "active":
        raise HttpError(409, "Voice session ended")
    if session.bridge_started_at:
        if standby_allowed(request.auth) and deadline_expired(session):
            end_voice_session(session.pk)
            raise HttpError(409, "Voice session expired")
        if not standby_allowed(request.auth) or session.standby_state != "resuming":
            if timezone.now() - session.bridge_started_at > timedelta(seconds=BRIDGE_JOB_SECONDS + 30):
                end_voice_session(session.pk)
            raise HttpError(409, "Voice session already connected; start a new one")
    url, key, secret = settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET
    if not all((url, key, secret)):
        raise HttpError(503, "Realtime voice is not configured")
    from livekit import api

    grant = api.VideoGrants(
        room_join=True,
        room=f"{settings.REALTIME_VOICE_ROOM_PREFIX}{session.id}",
        can_publish=True,
        can_publish_sources=["microphone"],
        can_publish_data=False,
        can_subscribe=True,
    )
    access = (
        api.AccessToken(key, secret)
        .with_identity(f"user-{request.auth.pk}")
        .with_ttl(timedelta(minutes=5))
        .with_grants(grant)
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=settings.REALTIME_VOICE_AGENT_NAME)])
        )
    )
    return {"url": url, "token": access.to_jwt()}


@router.get("/realtime-voice/sessions/{session_id}/events")
def events(request, session_id: str):
    return session_out(owned_session(request.auth, session_id))["events"]


# Browser-observed playback transitions: evidence for "I heard nothing", never proof of hearing.
CLIENT_AUDIO_STATUSES = {
    "track_subscribed": "Browser subscribed to the assistant's audio track.",
    "playback_started": "Browser started playing assistant audio; hearing is not confirmed.",
    "playback_blocked": "Browser blocked assistant audio playback.",
    "playback_failed": "Browser could not play assistant audio.",
    "sound_enabled": "Sound was enabled from the call page.",
    "audio_arriving": "Assistant audio is arriving in the browser (signal level detected).",
    "turn_unplayed": "An assistant turn finished before the browser confirmed playback.",
}
MAX_CLIENT_AUDIO_EVENTS = 40
CLIENT_AUDIO_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
CLIENT_AUDIO_DETAIL = re.compile(r"[A-Za-z0-9 ._:/()-]{0,120}")


class ClientAudioIn(Schema):
    event_id: str
    status: str
    detail: str = ""


@router.post("/realtime-voice/sessions/{session_id}/client-audio")
def client_audio(request, session_id: str, data: ClientAudioIn):
    """Record one bounded browser playback report for an active session."""
    session = owned_session(request.auth, session_id)
    if (
        data.status not in CLIENT_AUDIO_STATUSES
        or not CLIENT_AUDIO_ID.fullmatch(data.event_id)
        or not CLIENT_AUDIO_DETAIL.fullmatch(data.detail)
    ):
        raise HttpError(400, "Invalid client audio report")
    detail = data.detail.strip()
    text = CLIENT_AUDIO_STATUSES[data.status] + (f" ({detail})" if detail else "")
    event_id = f"client-audio:{data.event_id}"
    with transaction.atomic():
        locked = RealtimeVoiceSession.objects.select_for_update().get(pk=session.pk)
        if locked.status != "active":
            raise HttpError(409, "Voice session ended")
        if not locked.events.filter(event_id=event_id).exists() and (
            locked.events.filter(kind="client_audio").count() >= MAX_CLIENT_AUDIO_EVENTS
        ):
            return {"recorded": False}
        try:
            record_event(locked.pk, event_id, "client_audio", text)
        except ValueError as exc:
            raise HttpError(409, "Client audio report ID already used") from exc
    return {"recorded": True}
