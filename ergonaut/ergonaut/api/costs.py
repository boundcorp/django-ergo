"""Spending: every structured call's tokens, priced with django_ergo.pricing.

    GET /api/costs?days=30

Totals by kind, with chat replies (bot turns) split by bot, plus totals by
model and by day. Superusers see everyone's calls; others see their own.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone
from django_ergo.conversation.chat_reply import CHAT_REPLY_KIND
from django_ergo.conversation.models import StructuredCall
from django_ergo.pricing import call_cost, call_cost_parts
from ninja import Router, Schema
from ninja.security import django_auth

router = Router(tags=["costs"], auth=django_auth)


class Bucket(Schema):
    name: str
    calls: int
    # Billed separately: uncached input, cache reads, cache writes, and output (which
    # includes reasoning; reasoning_tokens says how much).
    input_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int
    reasoning_tokens: int = 0
    cost: float
    input_cost: float = 0.0
    cache_read_cost: float = 0.0
    cache_write_cost: float = 0.0
    output_cost: float = 0.0
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


def _empty(name: str) -> dict:
    return {
        "name": name,
        "calls": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cost": 0.0,
        "input_cost": 0.0,
        "cache_read_cost": 0.0,
        "cache_write_cost": 0.0,
        "output_cost": 0.0,
        "unpriced_calls": 0,
    }


def _add(bucket: dict, call: StructuredCall, cost: float | None, parts: dict | None) -> None:
    bucket["calls"] += 1
    bucket["input_tokens"] += call.input_tokens
    bucket["cache_read_tokens"] += call.cache_read_input_tokens
    bucket["cache_write_tokens"] += call.cache_creation_input_tokens
    bucket["output_tokens"] += call.output_tokens
    bucket["reasoning_tokens"] += getattr(call, "reasoning_tokens", 0) or 0
    if cost is None:
        bucket["unpriced_calls"] += 1
        return
    bucket["cost"] += cost
    for part in ("input", "cache_read", "cache_write", "output"):
        bucket[f"{part}_cost"] += (parts or {}).get(part, 0.0)


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
        cost = call_cost(call)
        parts = call_cost_parts(call) if cost is not None else None
        if cost is None:
            unpriced.add(call.model_name or "(unknown)")
        _add(total, call, cost, parts)
        _add(by_kind.setdefault(call.kind, _empty(call.kind)), call, cost, parts)
        model = call.model_name or "(unknown)"
        _add(by_model.setdefault(model, _empty(model)), call, cost, parts)
        if call.kind == CHAT_REPLY_KIND:
            bot = (call.session.bot_name if call.session else "") or "(no bot)"
            _add(by_bot.setdefault(bot, _empty(bot)), call, cost, parts)
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
