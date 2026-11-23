"""Permission-scoped, aggregate-only self-health observations over seven UTC days."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from django.db.models import Count
from django.db.models import Sum

from django_ergo.bots.snapshot import scope_bots
from django_ergo.conversation.models import AgentUsageEvent
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import RecurringWakeOccurrence
from django_ergo.conversation.models import Wake
from django_ergo.conversation.models import Worker

MAX_SESSIONS = 200


def report(ctx, end_utc: str, session_ids: list[str] | None = None) -> dict:
    """Aggregate only explicitly selected, accessible sessions; never read payloads."""
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
    ids = session_ids or []
    if not isinstance(ids, list) or len(ids) > MAX_SESSIONS:
        msg = "session_ids must be a list of at most 200 IDs"
        raise ValueError(msg)
    try:
        selected = {UUID(str(value)) for value in ids}
    except (ValueError, TypeError, AttributeError) as exc:
        msg = "Invalid session ID"
        raise ValueError(msg) from exc
    names = {bot.name for bot in scope_bots(ctx) if not bot.definition.private}
    sessions = ConversationSession.objects.filter(
        pk__in=selected,
        user_id=ctx.session.user_id,
        bot_name__in=names,
        created_at__lt=end,
    )
    if sessions.count() != len(selected):
        msg = "One or more sessions are inaccessible or outside the window"
        raise ValueError(msg)
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
    return {
        "window": {
            "start_inclusive": start.isoformat(),
            "end_exclusive": end.isoformat(),
        },
        "scope": {
            "selected_sessions": len(selected),
            "selection": "explicit; no sessions selected by default",
            "personal_classification": "caller must exclude personal conversations",
            "private_bots": "excluded",
            "max_sessions": MAX_SESSIONS,
        },
        "inventory": {
            "sessions": len(selected),
            "basis": "selected accessible sessions existing before window end",
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
            "coverage": "partial or unknown; missing events and null timestamps are not zero; totals suppressed for multiple native sessions because cross-session duplicates cannot be ruled out",
        },
    }
