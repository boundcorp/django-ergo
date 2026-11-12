"""Reviewed normal-thread execution, separate from the prototype's priced API lookup.

No blanket toggle or browser request can grant this. An exact chat/owner/bot
reviewed policy and matching durable session grant are both required. Empty by default.
"""

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django_ergo.conversation.models import ThreadMessage

NORMAL_RESTRICTION = (
    "Normal originating-thread execution: one request, using this bot's existing tools, engine, "
    "task limits and normal action approvals. Normal task costs are separate from the live voice "
    "budget. Voice does not approve actions; use the originating thread's normal approval controls."
)


def configured_authorization(chat, user):
    policies = getattr(settings, "REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS", {})
    policy = policies.get(str(chat.pk)) if isinstance(policies, dict) else None
    if not isinstance(policy, dict) or not policy:
        return None
    if (
        policy.get("owner") != user.get_username()
        or policy.get("bot") != chat.bot_name
        or chat.user_id != user.pk
        or policy.get("normal_task_costs_approved") is not True
        or not isinstance(policy.get("approval_reference"), str)
        or not policy["approval_reference"].strip()
    ):
        raise ValueError("Normal thread execution authorization does not match the exact owner/chat/bot or costs.")
    return {
        "chat_id": str(chat.pk),
        "owner_id": str(user.pk),
        "bot": chat.bot_name,
        "approval_reference": policy["approval_reference"],
        "max_requests": 1,
        "execution_mode": "normal_thread",
    }


def session_authorization(session):
    grant = (session.state or {}).get("normal_thread_authorization")
    if not grant:
        return None
    expected = configured_authorization(session.chat, session.user)
    if not expected or any(grant.get(key) != value for key, value in expected.items()):
        raise ValueError("Normal thread execution authorization was revoked or no longer matches this session.")
    return expected


def initial_state(chat, user):
    # Only conversational state survives into a new session; grants never inherit from history.
    saved = chat.metadata or {}
    state = {
        key: value
        for key, value in saved.get("voice_state", {}).items()
        if key
        in (
            "decisions",
            "constraints",
            "completed_steps",
            "current_step",
        )
    }
    grant = configured_authorization(chat, user)
    if grant:
        state["normal_thread_authorization"] = {**grant, "authorized_at": timezone.now().isoformat()}
    return state


def thread_updates(session, delegations):
    """Observe durable exact-thread work, not generated speech or a global owner scan."""
    normal = [d for d in delegations if (d.message.metadata or {}).get("voice_execution_mode") == "normal_thread"]
    if not normal:
        return []
    root = normal[0]
    # Causal sends/replies propagate the root's identifiers. Existing worker reports
    # arriving in the launch chat are observations, explicitly not claimed as started by voice.
    rows = (
        ThreadMessage.objects.filter(
            Q(metadata__voice_session_id=str(session.pk))
            | Q(recipient_session=session.chat, metadata__has_key="worker", created_at__gte=session.created_at)
        )
        .filter(Q(sender_session=session.chat) | Q(recipient_session=session.chat))
        .exclude(pk=root.message_id)
        .select_related("recipient_session", "sender_session")
        .order_by("created_at", "id")[:100]
    )
    updates = []
    for row in rows:
        if row.recipient_session.user_id != session.user_id or (
            row.sender_session_id and row.sender_session.user_id != session.user_id
        ):
            continue
        observation = not (row.metadata or {}).get("voice_event_id")
        updates.append(
            {
                "event_id": (row.metadata or {}).get("voice_event_id", root.event_id),
                "update_id": f"thread:{row.pk}:{row.status}",
                "text": ("Existing launch-thread worker report: " if observation else "Thread task: ") + row.text[:500],
                "status": row.status,
                "result": row.reply_text[:4000],
                "error": row.error[:4000],
                "chat_id": str(row.recipient_session_id),
                "observation": observation,
            }
        )
    return updates
