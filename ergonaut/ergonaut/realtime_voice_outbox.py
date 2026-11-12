"""Durable completion records for the realtime voice bridge."""

import hashlib

from django.db import transaction
from django.utils import timezone
from django_ergo.conversation.budget import relayable

from ergonaut.apps.bots.models import RealtimeVoiceEvent, RealtimeVoiceSession


def outbox_event(voice_id, event_id, kind, text):
    """Server-derived outcomes survive transport End; never reopen or mutate conversational state."""
    with transaction.atomic():
        session = RealtimeVoiceSession.objects.select_for_update().get(pk=voice_id)
        now = timezone.now()
        event, _ = RealtimeVoiceEvent.objects.get_or_create(
            session=session,
            event_id=event_id,
            defaults={"kind": kind, "text": text, "delivery": "unknown", "received_at": now, "processed_at": now},
        )
        if (event.kind, event.text) != (kind, text):
            raise ValueError("Outbox event ID already used")
        return event


def completion_event(voice_id, task):
    """Persist a stable completion before attempting to announce it.

    The result stays visible even when an at-most-once announcement is lost.
    Neither recording nor attempting an announcement establishes playback.
    """
    if task["status"] == "answered":
        text = task["result"] or "The background request completed without a text result."
    elif task["status"] == "failed":
        # Relay the reason only when this library wrote it (a budget limit names what was reached and
        # used; a queue gate says nothing ran). Exception or model text is never read to the user.
        reason = (task.get("error") or "").strip()
        reason = reason if relayable(reason) else ""
        text = f"The background request failed: {reason}" if reason else "The background request failed."
    else:
        return None
    event_id = "result:" + hashlib.sha256(task["event_id"].encode()).hexdigest()
    return outbox_event(voice_id, event_id, "result", text[:4000])


def thread_update_event(voice_id, task):
    """One durable receipt per observed task transition, distinct from an answered result."""
    if task["status"] in ("answered", "failed"):
        event = completion_event(voice_id, {**task, "event_id": task.get("update_id", task["event_id"])})
        return event
    labels = {
        "queued": "Task queued; it may not have started.",
        "delivered": "Task delivered; no final result yet.",
        "waiting": "Task waiting for normal thread approval; voice has not approved any action.",
    }
    text = labels.get(task["status"])
    if text is None:
        return None
    key = task.get("update_id", f"{task['event_id']}:{task['status']}")
    event_id = "task-status:" + hashlib.sha256(key.encode()).hexdigest()
    return outbox_event(voice_id, event_id, "task_status", text)


def claim_announcement(event):
    """Claim at most one announcement attempt; this does not establish playback."""
    with transaction.atomic():
        locked = RealtimeVoiceEvent.objects.select_for_update().get(pk=event.pk)
        if locked.delivery != "unknown":
            return False
        locked.delivery = "announcement_claimed"
        locked.save(update_fields=["delivery"])
        return True
