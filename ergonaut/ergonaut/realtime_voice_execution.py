"""Reviewed normal-thread execution, separate from the prototype's priced API lookup.

No blanket toggle or browser request can grant this. An exact chat/owner/bot
reviewed policy and matching durable session grant are both required. Empty by default.
"""

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django_ergo.conversation.models import ThreadMessage

NORMAL_RESTRICTION = (
    "Normal originating-thread execution uses this bot's existing tools, engine, "
    "task limits and normal action approvals. Normal task costs are separate from the live voice "
    "budget. Voice does not approve actions; use the originating thread's normal approval controls."
)

# This is backpressure, not a lifetime request allowance. Direct-client grants
# retain their separately reviewed finite limits.
NATIVE_MAX_INFLIGHT = 4


def native_request_capacity(session):
    authorization = session_authorization(session)
    inflight = session.delegations.exclude(message__status__in=("answered", "failed")).count()
    followups_available = bool(
        authorization
        and (
            authorization.get("followup_costs_approved") is True
            or not ((session.state or {}).get("voice_request_ids") or session.delegations.exists())
        )
    )
    return {
        "authorized": bool(authorization),
        "max_inflight": NATIVE_MAX_INFLIGHT,
        "inflight": inflight,
        "remaining": max(0, NATIVE_MAX_INFLIGHT - inflight) if followups_available else 0,
    }


def native_budget_projection(session):
    """UI-only native allowance; never used by the direct-client admission path."""
    legacy = request_capacity(session)
    try:
        authorization = session_authorization(session)
    except ValueError:
        return legacy
    if not authorization or authorization.get("followup_costs_approved") is not True:
        return legacy
    native = native_request_capacity(session)
    return {
        **legacy,
        "limit": None,
        "remaining": None,
        "inflight_limit": NATIVE_MAX_INFLIGHT,
        "inflight_count": native["inflight"],
    }


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
    limit = policy.get("max_requests", 1)
    if type(limit) is not int or limit not in (1, 3):
        raise ValueError("Voice execution permits only one request or an explicitly reviewed three-request cap.")
    if limit > 1 and (
        policy.get("followup_costs_approved") is not True
        or not isinstance(policy.get("followup_approval_reference"), str)
        or not policy["followup_approval_reference"].strip()
    ):
        raise ValueError("Sequential voice follow-ups require separate reviewed cost approval.")
    return {
        "chat_id": str(chat.pk),
        "owner_id": str(user.pk),
        "bot": chat.bot_name,
        "approval_reference": policy["approval_reference"],
        "max_requests": limit,
        **(
            {
                "followup_approval_reference": policy["followup_approval_reference"],
                "followup_costs_approved": True,
            }
            if limit > 1
            else {}
        ),
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


def request_capacity(session):
    """A bounded projection of existing voice receipts, not a second task ledger."""
    try:
        grant = session_authorization(session)
    except ValueError:
        limit = 0  # Revoked/mismatched grants fail closed even in read-only status polling.
    else:
        limit = grant["max_requests"] if grant else 1
    ids = list((session.state or {}).get("voice_request_ids", []))
    ids.extend(session.delegations.values_list("event_id", flat=True))
    pending = (session.state or {}).get("pending_delegation") or {}
    if pending.get("id"):
        ids.append(pending["id"])
    ids = list(dict.fromkeys(ids))
    return {
        "limit": limit,
        "used": len(ids),
        "remaining": max(0, limit - len(ids)),
        "request_ids": ids,
        "collecting": pending.get("status") == "collecting",
        "inflight": session.delegations.exclude(message__status__in=("answered", "failed")).exists(),
    }


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
        .exclude(pk__in=[delegation.message_id for delegation in normal])
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
