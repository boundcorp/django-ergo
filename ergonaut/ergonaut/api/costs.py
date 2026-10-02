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
from django_ergo.conversation.models import StructuredCall
from django_ergo.pricing import call_cost_parts
from ninja import Router, Schema
from ninja.security import django_auth

router = Router(tags=["costs"], auth=django_auth)


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


class DayOut(Schema):
    date: str
    cost: float
    calls: int


class CostsOut(Schema):
    days: int
    total: Bucket
    by_kind: list[Bucket]
    chat_reply_by_bot: list[Bucket]
    by_model: list[Bucket]
    by_day: list[DayOut]
    unpriced_models: list[str]


# (bucket token key, StructuredCall field, Price attribute)
PARTS = (
    ("input", "input_tokens", "input"),
    ("cache_write", "cache_creation_input_tokens", "cache_write"),
    ("cache_read", "cache_read_input_tokens", "cache_read"),
    ("output", "output_tokens", "output"),
)


def _empty(name: str) -> dict:
    bucket = {"name": name, "calls": 0, "cost": 0.0, "unpriced_calls": 0, "reasoning_tokens": 0}
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


@router.get("/costs", response=CostsOut)
def costs(request, days: int = 30):
    days = max(1, min(days, 366))
    since = timezone.now() - timedelta(days=days)
    calls = StructuredCall.objects.filter(created_at__gte=since).select_related("session")
    if not request.auth.is_superuser:
        calls = calls.filter(user=request.auth)
    calls = list(calls.order_by("created_at"))

    total = _empty("total")
    by_kind: dict[str, dict] = {}
    by_bot: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
    unpriced: set[str] = set()
    for call in calls:
        parts = call_costs(call)
        cost = None if parts is None else sum(parts.values())
        if parts is None:
            unpriced.add(call.model_name or "(unknown)")
        _add(total, call, parts)
        _add(by_kind.setdefault(call.kind, _empty(call.kind)), call, parts)
        model = call.model_name or "(unknown)"
        _add(by_model.setdefault(model, _empty(model)), call, parts)
        if call.kind == CHAT_REPLY_KIND:
            bot = (call.session.bot_name if call.session else "") or "(no bot)"
            _add(by_bot.setdefault(bot, _empty(bot)), call, parts)
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
    }
