"""Default-off STT -> normal originating-thread request -> TTS admission.

No SDK imports here: web and bridge enforce the same immutable, scoped contract.
"""

import hashlib
import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django_ergo.bots import messaging
from django_ergo.conversation.identity import django_user_identity
from django_ergo.conversation.models import StructuredCall, StructuredCallStatus, ThreadMessage

from ergonaut.apps.bots.models import RealtimeVoiceDelegation, RealtimeVoiceLedgerEntry, RealtimeVoiceSession
from ergonaut.realtime_voice_execution import configured_authorization, session_authorization

# Supported by the pinned livekit-plugins-openai 1.8.11, never inferred defaults.
TTS_LEGACY_VOICES = {"alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"}
TTS_MODEL_VOICES = {
    "tts-1": TTS_LEGACY_VOICES,
    "tts-1-hd": TTS_LEGACY_VOICES,
    "gpt-4o-mini-tts": TTS_LEGACY_VOICES | {"ballad"},
}


def media_authorization(chat, user):
    from ergonaut.api.voice import TRANSCRIPTION_PRICES, _enabled

    if not getattr(settings, "REALTIME_VOICE_TEXT_AGENT_ENABLED", False):
        raise ValueError("Text-agent voice is disabled; an administrator must enable its separate media gate.")
    if not _enabled():
        raise ValueError("Enable the existing OpenAI voice transcription integration and key first.")
    if settings.VOICE_TRANSCRIPTION_MODEL not in TRANSCRIPTION_PRICES:
        raise ValueError(
            "Select an existing supported and priced transcription model before enabling text-agent voice."
        )
    model = getattr(settings, "REALTIME_VOICE_TEXT_AGENT_TTS_MODEL", "")
    voice = getattr(settings, "REALTIME_VOICE_TEXT_AGENT_TTS_VOICE", "")
    if voice not in TTS_MODEL_VOICES.get(model, ()):
        raise ValueError("Configure an explicitly supported OpenAI TTS model and voice; neither has a default.")
    policies = getattr(settings, "REALTIME_VOICE_TEXT_AGENT_MEDIA_AUTHORIZATIONS", {})
    policy = policies.get(str(chat.pk)) if isinstance(policies, dict) else None
    expected = {
        "owner": user.get_username(),
        "bot": chat.bot_name,
        "stt_model": settings.VOICE_TRANSCRIPTION_MODEL,
        "tts_model": model,
        "tts_voice": voice,
        "stt_tts_costs_approved": True,
    }
    if (
        not isinstance(policy, dict)
        or any(policy.get(key) != value for key, value in expected.items())
        or chat.user_id != user.pk
        or not isinstance(policy.get("approval_reference"), str)
        or not policy["approval_reference"].strip()
    ):
        raise ValueError("This owner/thread/bot needs separate reviewed STT + TTS cost consent for these exact models.")
    normal = configured_authorization(chat, user)
    if not normal or normal.get("unlimited_intentional_requests") is not True:
        raise ValueError("This thread needs reviewed normal-task and intentional follow-up cost authorization.")
    # The same existing public endpoint restriction applies to both media adapters.
    from django_ergo.conversation.inference_policy import PolicyError, assert_public_endpoint, effective_openai_base_url

    try:
        assert_public_endpoint(effective_openai_base_url(), what="Text-agent voice media")
    except PolicyError:
        raise ValueError("Text-agent media cannot use a private inference endpoint.") from None
    return {**expected, "approval_reference": policy["approval_reference"], "chat_id": str(chat.pk)}


def availability(chat, user):
    try:
        media_authorization(chat, user)
    except ValueError as exc:
        return {"enabled": False, "reason": str(exc)}
    return {"enabled": True, "reason": "Uses separate STT/TTS spending consent and normal thread action approvals."}


def status(session):
    from ergonaut.api.realtime_voice import bridge_lease_expired

    try:
        grant = media_authorization(session.chat, session.user)
        if grant != session.state.get("text_media_authorization") or not session_authorization(session):
            raise ValueError("Text-agent voice authorization changed; review configuration before starting a new call.")
        if session.status != "active" or bridge_lease_expired(session):
            raise ValueError("Voice has ended; accepted normal work and results remain in the originating thread.")
    except ValueError as exc:
        return {
            "context": "",
            "execution_mode": "normal_thread",
            "delegation_available": False,
            "restriction": str(exc),
            "reason": str(exc),
        }
    return {
        "context": "",
        "execution_mode": "normal_thread",
        "delegation_available": True,
        "restriction": "Normal originating-thread execution and action approvals; separate reviewed STT/TTS media costs.",
    }


def require_session(session, claim_id):
    from ergonaut.api.bots import get_bot
    from ergonaut.api.realtime_voice import bridge_lease_expired, voice_allowed

    if (
        session.state.get("voice_mode", "native") != "text_agent"
        or session.status != "active"
        or session.bridge_claim_id != claim_id
        or not claim_id
        or not session.bridge_started_at
        or session.standby_state not in ("live", "requested")
        or bridge_lease_expired(session)
        or not voice_allowed(session.user)
    ):
        raise ValueError("Text-agent voice is no longer active on this bridge.")
    grant = media_authorization(session.chat, session.user)
    if session.state.get("text_media_authorization") != grant:
        raise ValueError("Text-agent media authorization changed; end this call and review its configuration.")
    if not session_authorization(session):
        raise ValueError("Normal originating-thread authorization was revoked.")
    get_bot(session.chat.bot_name, session.user)
    return grant


def _record(session, claim_id, utterance_id):
    if not isinstance(utterance_id, str) or not utterance_id or len(utterance_id) > 80:
        raise ValueError("Invalid finalized utterance identity.")
    identity = json.dumps([claim_id, utterance_id], separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    row = session.native_ledger.filter(namespace="text_turn", key_sha256=digest).first()
    if row and row.identity != identity:
        raise ValueError("Utterance identity conflict.")
    return row, identity, digest


def begin_utterance(voice_id, claim_id, utterance_id):
    """Reserve the finalized audio-batch identity BEFORE calling STT; store no audio."""
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        require_session(session, claim_id)
        old, identity, digest = _record(session, claim_id, utterance_id)
        if old:
            return old
        return RealtimeVoiceLedgerEntry.objects.create(
            session=session,
            namespace="text_turn",
            key_sha256=digest,
            identity=identity,
            data={"status": "transcribing", "event_id": f"text:{digest}"},
            pending=True,
        )


def finish_utterance(voice_id, claim_id, utterance_id, text):
    """One finalized utterance, one durable normal request; End and replay serialize here."""
    from ergonaut.api.realtime_voice import record_event

    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        row, _, _ = _record(session, claim_id, utterance_id)
        if row is None:
            raise ValueError("No reserved finalized utterance.")
        if row.data["status"] == "submitted":
            if row.data["text"] != text:
                raise ValueError("Finalized utterance identity already has different text.")
            return session.delegations.get(event_id=row.data["event_id"]).message
        require_session(session, claim_id)
        if not row.pending:
            raise ValueError("This finalized utterance was already rejected; no automatic retry.")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Finalized speech is empty or exceeds the per-utterance text limit.")
        event_id = row.data["event_id"]
        metadata = {
            "voice_session_id": str(session.pk),
            "voice_event_id": event_id,
            "voice_execution_mode": "normal_thread",
            "tool_execution_policy": "at_most_once",
            "model_retry_policy": "never",
            "voice_text_identity": {"claim_id": claim_id, "utterance_id": utterance_id},
            "normal_thread_authorization": session_authorization(session),
            "message_author": django_user_identity(session.user),
            "message_provenance": {"kind": "message", "voice_session_id": str(session.pk)},
        }
        message = messaging.send(None, session.chat, text, metadata=metadata)
        RealtimeVoiceDelegation.objects.create(session=session, event_id=event_id, text=text, message=message)
        row.data = {**row.data, "status": "submitted", "text": text, "message_id": str(message.pk)}
        row.pending = False
        row.save(update_fields=["data", "pending"])
        record_event(voice_id, event_id, "user", text, bridge_claim_id=claim_id)
    message.refresh_from_db()
    return message


def fail_utterance(voice_id, claim_id, utterance_id):
    """A failed/late media operation is visible but cannot become unreviewed retry work."""
    from ergonaut.api.realtime_voice import record_event

    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        row, _, _ = _record(session, claim_id, utterance_id)
        if row is None or not row.pending:
            return
        row.data = {**row.data, "status": "not_submitted"}
        row.pending = False
        row.save(update_fields=["data", "pending"])
        record_event(
            voice_id,
            f"failed:{row.data['event_id']}",
            "background_status",
            "Speech could not be submitted. Check transcription availability and the originating thread; no automatic retry.",
            bridge_claim_id=claim_id,
        )


def pending_outputs(voice_id):
    """Read a bounded batch of unannounced outcomes, never the bounded restore window."""
    session = RealtimeVoiceSession.objects.get(pk=voice_id)
    if session.status != "active":
        return []
    bindings = (
        session.native_ledger.filter(namespace="text_output")
        .annotate(
            bound_message=KeyTextTransform("message_id", "data"),
            bound_status=KeyTextTransform("status", "data"),
        )
        .filter(
            bound_message=Cast(OuterRef("pk"), CharField()),
            bound_status=OuterRef("status"),
        )
    )
    # Only normal, causally linked, same-owner work under an exact accepted root.
    # Root membership is authoritative even when its request left the history window.
    roots = session.delegations.filter(event_id=OuterRef("root_event"))
    rows = list(
        ThreadMessage.objects.filter(
            metadata__voice_session_id=str(session.pk),
            metadata__voice_execution_mode="normal_thread",
            recipient_session__user_id=session.user_id,
            status__in=("waiting", "answered", "failed"),
        )
        .filter(Q(sender_session__isnull=True) | Q(sender_session__user_id=session.user_id))
        .annotate(
            root_event=KeyTextTransform("voice_event_id", "metadata"),
            authorized_root=Exists(roots),
            output_claimed=Exists(bindings),
        )
        .filter(
            authorized_root=True,
            output_claimed=False,
        )
        .select_related("recipient_session")
        .order_by("created_at", "pk")[:32]
    )
    root_messages = set(
        session.delegations.filter(message_id__in=[row.pk for row in rows]).values_list("message_id", flat=True)
    )
    return [
        {
            "message_id": str(row.pk),
            "event_id": row.metadata["voice_event_id"],
            **({} if row.pk in root_messages else {"update_id": f"thread:{row.pk}:{row.status}"}),
            "text": row.text[:500],
            "status": row.status,
            "result": row.reply_text[:4000],
            "error": row.error[:4000],
            "execution_mode": "normal_thread",
            "chat_id": str(row.recipient_session_id),
            "observation": False,
        }
        for row in rows
    ]


def claim_output(voice_id, claim_id, event, task):
    from ergonaut.realtime_voice_outbox import claim_announcement

    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().select_related("chat", "user").get(pk=voice_id)
        require_session(session, claim_id)
        identity = event.event_id
        digest = hashlib.sha256(identity.encode()).hexdigest()
        row, created = session.native_ledger.get_or_create(
            namespace="text_output",
            key_sha256=digest,
            defaults={
                "identity": identity,
                "data": {
                    "message_id": task["message_id"],
                    "status": task["status"],
                    "root_event_id": task["event_id"],
                    "task_identity": task.get("update_id", f"root:{task['event_id']}"),
                    "result_event_id": event.event_id,
                    "claim_id": claim_id,
                    "speech_event_id": f"spoken:{event.event_id}",
                },
            },
        )
        if not created or row.identity != identity:
            return False
        return claim_announcement(event)


def record_synthesis(voice_id, model, usage):
    """Usage-only list-price estimate; absent token billing data is UNKNOWN, never free."""
    session = RealtimeVoiceSession.objects.get(pk=voice_id)
    amount = None
    if model in ("tts-1", "tts-1-hd"):
        amount = Decimal(usage.characters_count) * Decimal(15 if model == "tts-1" else 30) / Decimal(1_000_000)
    elif model == "gpt-4o-mini-tts" and usage.input_tokens > 0 and usage.output_tokens > 0:
        amount = (Decimal(usage.input_tokens) * Decimal("0.60") + Decimal(usage.output_tokens) * 12) / Decimal(
            1_000_000
        )
    StructuredCall.objects.create(
        kind="voice_synthesis",
        user_id=session.user_id,
        session_id=session.chat_id,
        status=StructuredCallStatus.COMPLETED,
        engine_type="openai",
        model_name=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=amount,
        metadata={
            "synthesis": {
                "voice_session_id": str(session.pk),
                "provider_request_id": usage.request_id,
                "speech_id": usage.speech_id,
                "characters": usage.characters_count,
                "audio_seconds": usage.audio_duration,
                "cancelled": usage.cancelled,
                "estimated": True,
                "unpriced": amount is None,
            },
        },
    )
