"""Authenticated realtime voice sessions and durable bridge state."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django_ergo.bots import messaging
from django_ergo.conversation.budget import TurnBudget, attach
from ninja import Router, Schema
from ninja.errors import HttpError

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import get_bot, uuid_or_404, visible_sessions
from ergonaut.api.realtime_voice_state import apply_user_event, current_time
from ergonaut.apps.bots.models import RealtimeVoiceDelegation, RealtimeVoiceEvent, RealtimeVoiceSession

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


def end_voice_session(voice_id):
    """End an active session: Stop, or its bridge job finishing (timeout, disconnect, error)."""
    RealtimeVoiceSession.objects.filter(pk=voice_id, status="active").update(status="ended", ended_at=timezone.now())


def end_bridge_session(voice_id, claim_id):
    """A stale job cannot end standby or a later bridge's claim."""
    RealtimeVoiceSession.objects.filter(pk=voice_id, status="active", bridge_claim_id=claim_id).exclude(
        standby_state__in=("standby", "resuming")
    ).update(status="ended", ended_at=timezone.now())


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
            session.save(update_fields=["status", "ended_at"])
            return False
        session.bridge_claim_id = claim_id or uuid.uuid4().hex
        if session.bridge_started_at is None:
            session.bridge_started_at = now
            session.save(update_fields=["bridge_started_at", "bridge_claim_id"])
            return True
        if not standby_allowed(session.user) or session.standby_state != "resuming":
            return False
        session.standby_state = "live"
        session.save(update_fields=["standby_state", "bridge_claim_id"])
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


def finish_standby_close(voice_id, claim_id):
    """Publish standby only after all transport teardown succeeded."""
    if not getattr(settings, "REALTIME_VOICE_STANDBY_ENABLED", False) or not getattr(
        settings, "REALTIME_VOICE_ENABLED", False
    ):
        return False
    now = timezone.now()
    field = get_user_model().USERNAME_FIELD
    return bool(
        RealtimeVoiceSession.objects.filter(
            pk=voice_id,
            status="active",
            standby_state="closing",
            bridge_claim_id=claim_id,
            bridge_started_at__gt=now - timedelta(seconds=BRIDGE_JOB_SECONDS),
            **{f"user__{field}__in": getattr(settings, "REALTIME_VOICE_USERS", set())},
        ).update(standby_state="standby", standby_at=now)
    )


def bridge_state(voice_id):
    """Bounded recent transcript plus durable work and explicit state for reconnect."""
    session = RealtimeVoiceSession.objects.get(pk=voice_id)
    events = list(session.events.order_by("-id")[:100])
    delegations = list(session.delegations.select_related("message").order_by("-id")[:100])
    delegations.reverse()
    state = {"decisions": [], "constraints": [], "current_step": "", **session.state}
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
        "activities": activities,
        "delegations": [
            {
                "event_id": d.event_id,
                "text": d.text,
                "status": d.message.status,
                "result": d.message.reply_text[:4000],
                "error": d.message.error[:4000],
            }
            for d in delegations
        ],
    }


def record_event(voice_id, event_id, kind, text, delivery="unknown", client_event_at=None, *, trusted_delivery=False):
    received_at = timezone.now()
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
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        old = session.events.filter(event_id=event_id).first()
        if old:
            if (old.kind, old.text, old.client_event_at) != (kind, text, client_event_at):
                raise ValueError("Event ID already used")
            if delivery != old.delivery and delivery != "unknown":
                if not trusted_delivery or old.delivery == "delivered":
                    raise ValueError("Event ID already used")
                old.delivery = delivery
                old.save(update_fields=["delivery"])
            return old
        if session.status != "active":
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
        if kind in ("user", "user_transcript"):
            apply_user_event(session, event)
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


def update_pending_fragment(voice_id, event_id, item_id, text):
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        state = dict(session.state)
        pending = state.get("pending_delegation")
        if not pending or pending["id"] != event_id or pending["status"] != "collecting" or session.status != "active":
            return
        fragments = list(pending["fragments"])
        for fragment in fragments:
            if fragment["id"] == item_id:
                fragment["text"] = text
                break
        else:
            fragments.append({"id": item_id, "text": text})
        pending["fragments"] = fragments
        state["pending_delegation"] = pending
        session.state = state
        session.save(update_fields=["state"])


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
    """Atomically persist a deduplicated request in the existing thread-message queue.

    The queued message carries its budget; ``messaging`` runs it only under that budget."""
    budget = delegation_budget()
    if not event_id or len(event_id) > 200 or not text.strip() or len(text) > 4000:
        raise ValueError("Invalid delegation")
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat").get(pk=voice_id)
        old = session.delegations.select_related("message").filter(event_id=event_id).first()
        if old:
            if old.text != text:
                raise ValueError("Delegation ID already used")
            return old.message
        if session.delegations.exists():
            raise ValueError("This local voice slice permits one background request")
        if session.status != "active":
            raise ValueError("Voice session ended")
        context = bridge_state(session.id)
        prompt = (
            "Voice session context (assistant speech delivery may be unknown or interrupted):\n"
            + json.dumps(
                {
                    "state": context["state"],
                    "events": context["events"],
                    "activities": context["activities"],
                    "delegations": context["delegations"],
                },
                ensure_ascii=False,
            )
            + "\n\nCurrent delegated request:\n"
            + text
        )
        message = messaging.send(
            None,
            session.chat,
            prompt,
            metadata=attach({"voice_session_id": str(session.id), "voice_event_id": event_id}, budget),
        )
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
    state = bridge_state(session.id)
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
        "standby_state": "requested" if session.standby_state == "closing" else session.standby_state,
        "bridge_started_at": session.bridge_started_at.isoformat() if session.bridge_started_at else None,
        "standby_at": session.standby_at.isoformat() if session.standby_at else None,
        "resumed_at": session.resumed_at.isoformat() if session.resumed_at else None,
        "standby_seconds": standby_seconds,
        "created_at": session.created_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "events": state["events"],
        "state": state["state"],
        "delegations": state["delegations"],
        "activities": state["activities"],
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
    return session_out(RealtimeVoiceSession.objects.create(chat=chat, user=request.auth))


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
