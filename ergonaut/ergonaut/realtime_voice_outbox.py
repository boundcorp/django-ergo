"""Durable completion records for the realtime voice bridge."""

import hashlib

from ergonaut.api.realtime_voice import record_event


def completion_event(voice_id, task):
    """Persist a stable completion before attempting to announce it.

    An unknown or interrupted record stays pending after a bridge disconnect;
    recording an intent is not evidence that the listener heard the result.
    """
    if task["status"] == "answered":
        text = task["result"] or "The background request completed without a text result."
    elif task["status"] == "failed":
        text = "The background request failed."
    else:
        return None
    event_id = "result:" + hashlib.sha256(task["event_id"].encode()).hexdigest()
    return record_event(voice_id, event_id, "result", text[:4000], "unknown")
