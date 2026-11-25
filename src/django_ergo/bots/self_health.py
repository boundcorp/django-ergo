"""Permission-scoped, aggregate-only self-health observations over seven UTC days."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from django.db.models import Count
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Sum

from django_ergo.bots.registry import may_use
from django_ergo.bots.snapshot import scope_bots
from django_ergo.conversation.models import AgentUsageEvent
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import RecurringWakeOccurrence
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.models import Wake
from django_ergo.conversation.models import Worker

MAX_SESSIONS = 200


def classify(bot, session_id) -> tuple[bool, str]:
    """Whether a session may be counted, and why. Only owner declarations count.

    Order: private bot, per-session deny, personal bot, non-personal bot,
    per-session allow; anything else is ``unclassified`` and excluded. Names of
    bots or chats are never consulted.
    """
    declared = bot.definition.self_health
    sid = str(session_id)
    if bot.definition.private:
        return False, "private_bot"
    if sid in declared.exclude_sessions:
        return False, "denied_session"
    if declared.scope == "personal":
        return False, "personal_bot"
    if declared.scope == "non_personal":
        return True, "bot_non_personal"
    if sid in declared.include_sessions:
        return True, "session_allowed"
    return False, "unclassified"


def _explicit(ctx, bots, session_ids, end) -> set[UUID]:
    try:
        selected = {UUID(str(value)) for value in session_ids}
    except (ValueError, TypeError, AttributeError) as exc:
        msg = "Invalid session ID"
        raise ValueError(msg) from exc
    rows = ConversationSession.objects.filter(
        pk__in=selected, user_id=ctx.session.user_id, created_at__lt=end
    ).values_list("pk", "bot_name")
    found = {pk for pk, name in rows if name in bots and classify(bots[name], pk)[0]}
    if found != selected:
        msg = (
            "One or more sessions are inaccessible, outside the window or not "
            "classified non-personal"
        )
        raise ValueError(msg)
    return selected


def _discover(ctx, bots, start, end) -> tuple[set[UUID], dict]:
    """Sessions of the caller with activity in the window that classify as non-personal.

    Existence checks only; no message content is read. Ordered by creation then id so
    a capped list is stable.
    """
    in_window = {"created_at__gte": start, "created_at__lt": end}
    active = ConversationSession.objects.filter(
        user_id=ctx.session.user_id, created_at__lt=end
    ).filter(
        Exists(SessionMessage.objects.filter(session=OuterRef("pk"), **in_window))
        | Exists(Worker.objects.filter(session=OuterRef("pk"), **in_window))
        | Exists(
            Worker.objects.filter(
                session=OuterRef("pk"), completed_at__gte=start, completed_at__lt=end
            )
        )
        | Exists(
            Wake.objects.filter(
                session=OuterRef("pk"), fired_at__gte=start, fired_at__lt=end
            )
        )
        | Exists(
            RecurringWakeOccurrence.objects.filter(
                schedule__session=OuterRef("pk"),
                dispatched_at__gte=start,
                dispatched_at__lt=end,
            )
        )
    )
    included: list[UUID] = []
    excluded: dict[str, int] = {}
    considered = 0
    for pk, name in active.order_by("created_at", "pk").values_list("pk", "bot_name"):
        considered += 1
        ok, reason = (
            classify(bots[name], pk) if name in bots else (False, "out_of_scope")
        )
        if ok:
            included.append(pk)
        else:
            excluded[reason] = excluded.get(reason, 0) + 1
    truncated = len(included) > MAX_SESSIONS
    kept = included[:MAX_SESSIONS]
    return set(kept), {
        "active_sessions_considered": considered,
        "included": len(kept),
        "classified_non_personal": len(included),
        "excluded_by_reason": dict(sorted(excluded.items())),
        "truncated": truncated,
        "not_included_due_to_cap": len(included) - len(kept),
        "ordering": "created_at ascending, then session id",
        "activity_basis": "a stored message, worker start or completion, fired wake or dispatched recurring wake inside the window",
    }


def _coverage(discovery) -> str:
    if discovery is None:
        return "partial; explicit selection is not evidence of zero activity elsewhere"
    if discovery["truncated"]:
        return (
            "partial; the cap truncated the classified sessions, so totals understate"
        )
    if discovery["excluded_by_reason"]:
        return "partial; active sessions that are not classified non-personal are excluded (see discovery)"
    return "complete for classified non-personal sessions with recorded activity in the window"


def _work_assertions(completions) -> dict:
    """Project only the structured scalar; never load report prose or infer success."""
    counts = dict.fromkeys(("completed", "partial", "blocked", "failed", "unknown"), 0)
    for value in completions.values_list("result__work_disposition", flat=True):
        key = value if isinstance(value, str) and value in counts else "unknown"
        counts[key] += 1
    return {
        "counts": counts,
        "basis": "explicit worker result work_disposition assertions; missing or malformed fields unknown; not authenticated acceptance or verified execution",
    }


def report(ctx, end_utc: str, session_ids: list[str] | None = None) -> dict:
    """Aggregate counts for explicitly selected or automatically discovered non-personal sessions; never read payloads."""
    try:
        end = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        msg = "end_utc must be an ISO timestamp with timezone"
        raise ValueError(msg) from exc
    if end.tzinfo is None or end.utcoffset() is None:
        msg = "end_utc must include a timezone"
        raise ValueError(msg)
    end = end.astimezone(UTC)
    start = end - timedelta(days=7)
    user = ctx.user
    if (
        not user
        or not user.is_authenticated
        or user.pk != ctx.session.user_id
        or not may_use(ctx.bot, user)
    ):
        msg = "An authenticated authorized session owner is required"
        raise ValueError(msg)
    if session_ids is not None and (
        not isinstance(session_ids, list) or len(session_ids) > MAX_SESSIONS
    ):
        msg = "session_ids must be a list of at most 200 IDs"
        raise ValueError(msg)
    bots = {bot.name: bot for bot in scope_bots(ctx)}
    if session_ids:
        selected = _explicit(ctx, bots, session_ids, end)
        discovery = None
    else:
        selected, discovery = _discover(ctx, bots, start, end)
    sessions = ConversationSession.objects.filter(pk__in=selected)
    window = {"created_at__gte": start, "created_at__lt": end}
    workers = Worker.objects.filter(session__in=sessions, **window)
    completions = Worker.objects.filter(
        session__in=sessions, completed_at__gte=start, completed_at__lt=end
    )
    tool_results = MessageBlock.objects.filter(
        message__session__in=sessions, block_type="tool_result", **window
    )
    usage = AgentUsageEvent.objects.filter(
        run_session__worker__session__in=sessions,
        occurred_at__gte=start,
        occurred_at__lt=end,
    )
    totals = usage.aggregate(
        input_total=Sum("input_tokens"),
        output_total=Sum("output_tokens"),
        cache_read_total=Sum("cache_read_tokens"),
        cache_write_total=Sum("cache_write_tokens"),
        known_input=Count("input_tokens"),
        known_output=Count("output_tokens"),
        known_cache_read=Count("cache_read_tokens"),
        known_cache_write=Count("cache_write_tokens"),
        events=Count("id"),
        covered_workers=Count("run_session__worker_id", distinct=True),
        native_sessions=Count("session_id", distinct=True),
    )
    # Session-local event keys cannot prove two native sessions contain different
    # requests. Suppress sums when cross-session duplication cannot be excluded.
    if totals["native_sessions"] > 1:
        for key in (
            "input_total",
            "output_total",
            "cache_read_total",
            "cache_write_total",
        ):
            totals[key] = None
    for total, known in (
        ("input_total", "known_input"),
        ("output_total", "known_output"),
        ("cache_read_total", "known_cache_read"),
        ("cache_write_total", "known_cache_write"),
    ):
        if totals[known] != totals["events"]:
            totals[total] = None
    return {
        "window": {
            "start_inclusive": start.isoformat(),
            "end_exclusive": end.isoformat(),
        },
        "scope": {
            "selected_sessions": len(selected),
            "selection": "explicit" if discovery is None else "automatic",
            "coverage": _coverage(discovery),
            "personal_classification": "owner-declared non-personal only (bot.yaml self_health); anything not positively classified is excluded",
            "private_bots": "excluded",
            "max_sessions": MAX_SESSIONS,
            "discovery": discovery,
        },
        "inventory": {
            "sessions": len(selected),
            "basis": "selected accessible sessions existing before window end"
            if discovery is None
            else "classified non-personal sessions with recorded activity in the window",
        },
        "workers": {
            "attempts_started": workers.count(),
            "results_in_window": completions.count(),
            "results_by_current_status": {
                row["status"]: row["n"]
                for row in completions.values("status")
                .annotate(n=Count("id"))
                .order_by("status")
            },
            "work_assertions": _work_assertions(completions),
            "basis": "one Worker row per attempt; created_at for starts, completed_at for results",
        },
        "wakes": {
            "one_shot_fired": Wake.objects.filter(
                session__in=sessions, fired_at__gte=start, fired_at__lt=end
            ).count(),
            "recurring_dispatched": RecurringWakeOccurrence.objects.filter(
                schedule__session__in=sessions,
                dispatched_at__gte=start,
                dispatched_at__lt=end,
            ).count(),
            "basis": "Wake.fired_at and distinct RecurringWakeOccurrence.dispatched_at",
        },
        "tools": {
            "recorded_results": tool_results.count(),
            "recorded_failures": tool_results.filter(is_error=True).count(),
            "basis": "stored tool result blocks by block creation time; missing or unrecorded calls unknown",
        },
        "agent_tokens": {
            "input_tokens": totals.pop("input_total"),
            "output_tokens": totals.pop("output_total"),
            "cache_read_tokens": totals.pop("cache_read_total"),
            "cache_write_tokens": totals.pop("cache_write_total"),
            **totals,
            "basis": "uniquely stored timestamped native usage events linked to selected workers; cumulative worker totals excluded",
            "coverage": "partial or unknown; missing events and null timestamps are not zero; fields with missing values and totals for multiple native sessions are suppressed for multiple native sessions because cross-session duplicates cannot be ruled out",
        },
    }
