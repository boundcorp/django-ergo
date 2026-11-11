"""Durable completion records for the realtime voice bridge."""

import hashlib

from django.db import transaction
from django_ergo.conversation.budget import relayable

from ergonaut.api.realtime_voice import record_event
from ergonaut.apps.bots.models import RealtimeVoiceEvent


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
    return record_event(voice_id, event_id, "result", text[:4000], "unknown")


def claim_announcement(event):
    """Claim at most one announcement attempt; this does not establish playback."""
    with transaction.atomic():
        locked = RealtimeVoiceEvent.objects.select_for_update().get(pk=event.pk)
        if locked.delivery != "unknown":
            return False
        locked.delivery = "announcement_claimed"
        locked.save(update_fields=["delivery"])
        return True
