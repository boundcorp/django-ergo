"""Durable completion records for the realtime voice bridge."""

import hashlib

from django.db import transaction

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
        text = "The background request failed."
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
