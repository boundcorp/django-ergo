"""Conservative, extractive state updates from finalized user reports.

These rules intentionally retain the user's words rather than inferring actions. They
are not a semantic summarizer; unmatched statements remain in the transcript.
"""

import re
from datetime import UTC

from django.utils import timezone

from ergonaut.apps.bots.models import RealtimeVoiceActivity, RealtimeVoiceSession

_DECISION = re.compile(
    r"\b(?:using|we(?:'ll| will) use|i(?:'ve| have) decided|we(?:'ve| have) decided|(?:i|we) choose|(?:i|we) chose)\b",
    re.I,
)
_CONSTRAINT = re.compile(r"\b(?:missing|without|allergic to|cannot|can't|must not|do not|don't|no deployment)\b", re.I)
_STEP = re.compile(r"\b(?:ready to|currently|current step|is complete)\b", re.I)
_ACTIVITY = re.compile(
    r"^\s*(?:i|we) (?:just |have |already )?(?:started|began|flipped|turned|finished|completed|put|placed|added|removed)\b",
    re.I,
)


def apply_user_event(session, event):
    """Update under the caller's session lock, once per finalized user event."""
    state = dict(session.state)
    # Keep each source sentence verbatim, not a model-generated claim.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", event.text) if s.strip()]
    for sentence in sentences:
        for key, pattern in (("decisions", _DECISION), ("constraints", _CONSTRAINT)):
            if pattern.search(sentence):
                values = list(state.get(key, []))
                if sentence not in values:
                    values.append(sentence[:1000])
                state[key] = values[-50:]
        if _STEP.search(sentence):
            state["current_step"] = sentence[:1000]
    if state != session.state:
        session.state = state
        session.save(update_fields=["state"])
    if _ACTIVITY.search(event.text):
        RealtimeVoiceActivity.objects.get_or_create(
            session=session, event=event, defaults={"description": event.text[:4000]}
        )


def current_time(voice_id, activity_id=None):
    """Return UTC now and elapsed time since a report, never since an inferred action."""
    now = timezone.now().astimezone(UTC)
    session = RealtimeVoiceSession.objects.get(pk=voice_id)
    result = {"now_utc": now.isoformat(), "activity": None, "elapsed_since_report_seconds": None}
    if activity_id is not None:
        activity = session.activities.select_related("event").filter(event__event_id=activity_id).first()
        if activity is not None:
            received = activity.event.received_at
            result["activity"] = {
                "event_id": activity.event.event_id,
                "description": activity.description,
                "reported_at_utc": received.astimezone(UTC).isoformat() if received else None,
            }
            if received is not None:
                result["elapsed_since_report_seconds"] = (now - received).total_seconds()
    return result
