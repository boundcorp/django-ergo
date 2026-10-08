"""Spending: every structured call's tokens, priced with django_ergo.pricing.

    GET /api/costs?days=30

Totals by kind, with chat replies (bot turns) split by bot, plus totals by
model and by day. Tokens and cost are split into uncached input, cache writes,
cache reads and output, each priced at its own rate. Superusers see everyone's calls; others see their own.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone
from django_ergo.conversation.chat_reply import CHAT_REPLY_KIND
from django_ergo.conversation.models import AgentUsage, StructuredCall
from django_ergo.pricing import call_cost_parts
from ninja import Router, Schema

from ergonaut.api.auth import user_auth

router = Router(tags=["costs"], auth=user_auth)


class Bucket(Schema):
    name: str
    calls: int
    # Uncached input only; cache writes and reads are counted separately.
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0  # part of output_tokens (billed as output)
    input_cost: float
    cache_write_cost: float
    cache_read_cost: float
    output_cost: float
    cost: float
    unpriced_calls: int
    subscription_calls: int


class DayOut(Schema):
    date: str
    cost: float
    calls: int


class UsageHeadline(Schema):
    threads: int
    tokens: int
    cache_hit: float
    main_chats: float
    subscription: float
    api_spend: float
    compaction: float


class UsageThread(Schema):
    id: str
    title: str
    bot: str
    model: str
    role: str
    subscription: bool
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    tokens: int
    cache_hit: float
    share: float
    cost: float


class UsageOut(Schema):
    headline: UsageHeadline
    threads: list[UsageThread]


class AgentHeadline(Schema):
    sessions: int
    tokens: int
    cache_hit: float


class AgentUsageRow(Schema):
    worker_id: str
    worker_title: str
    agent: str
    model: str
    chat_id: str
    chat_title: str
    bot: str
    worker_status: str
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_tokens: int
    tokens: int
    cache_hit: float
    requests: int


class AgentsOut(Schema):
    headline: AgentHeadline
    rows: list[AgentUsageRow]


class CostsOut(Schema):
    days: int
    total: Bucket
    by_kind: list[Bucket]
    chat_reply_by_bot: list[Bucket]
    by_model: list[Bucket]
    by_day: list[DayOut]
    unpriced_models: list[str]
    agents: AgentsOut
    usage: UsageOut


# (bucket token key, StructuredCall field, Price attribute)
PARTS = (
    ("input", "input_tokens", "input"),
    ("cache_write", "cache_creation_input_tokens", "cache_write"),
    ("cache_read", "cache_read_input_tokens", "cache_read"),
    ("output", "output_tokens", "output"),
)


def _empty(name: str) -> dict:
    bucket = {
        "name": name,
        "calls": 0,
        "cost": 0.0,
        "unpriced_calls": 0,
        "subscription_calls": 0,
        "reasoning_tokens": 0,
    }
    for part, _, _ in PARTS:
        bucket[f"{part}_tokens"] = 0
        bucket[f"{part}_cost"] = 0.0
    return bucket


def call_costs(call: StructuredCall) -> dict[str, float] | None:
    """The call's cost per part (input, cache_write, cache_read, output), or None if unpriced:
    what was recorded request by request, else priced from its totals (older calls)."""
    return call_cost_parts(call)


def _add(bucket: dict, call: StructuredCall, costs: dict[str, float] | None) -> None:
    bucket["calls"] += 1
    if call.session and call.session.transport_type == "cli":
        bucket["subscription_calls"] += 1
    for part, field, _ in PARTS:
        bucket[f"{part}_tokens"] += getattr(call, field) or 0
    bucket["reasoning_tokens"] += getattr(call, "reasoning_tokens", 0) or 0
    if costs is None:
        bucket["unpriced_calls"] += 1
        return
    for part, value in costs.items():
        bucket[f"{part}_cost"] += value
    bucket["cost"] += sum(costs.values())


def _sorted(buckets: dict) -> list[dict]:
    return sorted(buckets.values(), key=lambda b: (-b["cost"], -b["calls"], b["name"]))


def _tokens(bucket: dict) -> int:
    return sum(bucket[f"{part}_tokens"] for part, _, _ in PARTS)


def _cache_hit(bucket: dict) -> float:
    denominator = bucket["input_tokens"] + bucket["cache_write_tokens"] + bucket["cache_read_tokens"]
    return bucket["cache_read_tokens"] / denominator if denominator else 0.0


def _add_session(by_session: dict[str, dict], call: StructuredCall, parts: dict | None) -> tuple[int, int]:
    session = call.session
    if session is None:
        return 0, 0
    session_id = str(session.id)
    if session_id not in by_session:
        meta = session.metadata or {}
        by_session[session_id] = {
            "id": session_id,
            "title": meta.get("title") or f"{session.bot_name or 'Bot'} main",
            "bot": session.bot_name or "(no bot)",
            "model": call.model_name or "(unknown)",
            "role": meta.get("bot_role") or "",
            "subscription": session.transport_type == "cli",
            "bucket": _empty(session_id),
        }
    row = by_session[session_id]
    _add(row["bucket"], call, parts)
    if call.model_name:
        row["model"] = call.model_name
    call_tokens = sum((getattr(call, field) or 0) for _, field, _ in PARTS)
    return (
        call_tokens if row["role"] != "thread" else 0,
        call_tokens if row["subscription"] else 0,
    )


def _usage(
    total: dict,
    by_session: dict[str, dict],
    main: int,
    subscription: int,
    compaction: int,
) -> dict:
    token_total = _tokens(total)
    threads = []
    for row in by_session.values():
        bucket = row.pop("bucket")
        row_tokens = _tokens(bucket)
        threads.append(
            {
                **row,
                **{f"{part}_tokens": bucket[f"{part}_tokens"] for part, _, _ in PARTS},
                "tokens": row_tokens,
                "cache_hit": _cache_hit(bucket),
                "share": row_tokens / token_total if token_total else 0.0,
                "cost": bucket["cost"],
            }
        )
    threads.sort(key=lambda row: (-row["share"], row["title"], row["id"]))
    return {
        "headline": {
            "threads": len(threads),
            "tokens": token_total,
            "cache_hit": _cache_hit(total),
            "main_chats": main / token_total if token_total else 0.0,
            "subscription": subscription / token_total if token_total else 0.0,
            "api_spend": total["cost"],
            "compaction": compaction / token_total if token_total else 0.0,
        },
        "threads": threads,
    }


def _agents(rows: list[AgentUsage]) -> dict:
    output = []
    total = {"input_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}
    workers = set()
    for usage in rows:
        session = usage.session
        worker = usage.worker
        tokens = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens + usage.output_tokens
        workers.add(usage.worker_id)
        total["input_tokens"] += usage.input_tokens
        total["cache_write_tokens"] += usage.cache_write_tokens
        total["cache_read_tokens"] += usage.cache_read_tokens
        output.append(
            {
                "worker_id": str(usage.worker_id or ""),
                "worker_title": worker.title if worker else "(deleted worker)",
                "agent": usage.agent,
                "model": usage.model,
                "chat_id": str(session.id),
                "chat_title": (session.metadata or {}).get("title") or f"{session.bot_name or 'Bot'} main",
                "bot": usage.bot_name,
                "worker_status": worker.status if worker else "deleted",
                "input_tokens": usage.input_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "tokens": tokens,
                "cache_hit": (
                    usage.cache_read_tokens / (usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens)
                    if usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
                    else 0.0
                ),
                "requests": usage.requests,
            }
        )
    output.sort(key=lambda row: (-row["tokens"], row["worker_title"], row["model"]))
    denominator = total["input_tokens"] + total["cache_write_tokens"] + total["cache_read_tokens"]
    return {
        "headline": {
            "sessions": len(workers),
            "tokens": sum(row["tokens"] for row in output),
            "cache_hit": total["cache_read_tokens"] / denominator if denominator else 0.0,
        },
        "rows": output,
    }


@router.get("/costs", response=CostsOut)
def costs(request, days: int = 30, bot: str = ""):
    days = max(1, min(days, 366))
    since = timezone.now() - timedelta(days=days)
    calls = StructuredCall.objects.filter(created_at__gte=since).select_related("session")
    if not request.auth.is_superuser:
        calls = calls.filter(user=request.auth)
    if bot:
        calls = calls.filter(session__bot_name=bot)
    calls = list(calls.order_by("created_at"))
    agent_rows = AgentUsage.objects.filter(last_at__gte=since, last_at__lte=timezone.now()).select_related(
        "worker", "session"
    )
    if not request.auth.is_superuser:
        agent_rows = agent_rows.filter(session__user=request.auth)
    if bot:
        agent_rows = agent_rows.filter(bot_name=bot)

    total = _empty("total")
    by_kind: dict[str, dict] = {}
    by_bot: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    unpriced: set[str] = set()
    by_session: dict[str, dict] = {}
    main_tokens = subscription_tokens = compaction_tokens = 0
    for call in calls:
        parts = call_costs(call)
        cost = None if parts is None else sum(parts.values())
        if parts is None:
            unpriced.add(call.model_name or "(unknown)")
        _add(total, call, parts)
        main_count, subscription_count = _add_session(by_session, call, parts)
        main_tokens += main_count
        subscription_tokens += subscription_count
        if call.kind == "compaction":
            compaction_tokens += sum((getattr(call, field) or 0) for _, field, _ in PARTS)
        _add(by_kind.setdefault(call.kind, _empty(call.kind)), call, parts)
        model = call.model_name or "(unknown)"
        _add(by_model.setdefault(model, _empty(model)), call, parts)
        if call.kind == CHAT_REPLY_KIND:
            chat_bot = (call.session.bot_name if call.session else "") or "(no bot)"
            _add(by_bot.setdefault(chat_bot, _empty(chat_bot)), call, parts)
        day = by_day[timezone.localtime(call.created_at).date().isoformat()]
        day["calls"] += 1
        day["cost"] += cost or 0.0

    today = timezone.localdate()
    series = [
        {"date": d, **by_day.get(d, {"cost": 0.0, "calls": 0})}
        for d in ((today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1))
    ]
    return {
        "days": days,
        "total": total,
        "by_kind": _sorted(by_kind),
        "chat_reply_by_bot": _sorted(by_bot),
        "by_model": _sorted(by_model),
        "by_day": series,
        "unpriced_models": sorted(unpriced),
        "agents": _agents(list(agent_rows)),
        "usage": _usage(total, by_session, main_tokens, subscription_tokens, compaction_tokens),
    }
