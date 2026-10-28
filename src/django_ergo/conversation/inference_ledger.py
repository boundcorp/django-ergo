"""The private-inference ledger: one ``InferenceRequest`` row per HTTP attempt
to a private pool model, and the pool's optional daily spend cap.

``open_attempt`` runs before the request is sent. With ``daily_usd_cap`` set
on the pool it reserves the attempt's worst-case cost (every request byte
counted as an uncached input token, plus the full output budget) under a
lock, and refuses (:class:`~inference_policy.SpendCapError`, no HTTP) when
today's spend plus reservations (UTC day) would pass the cap, or when the
model has no exact price or the request no output limit. ``finish_attempt``
prices the reported usage exactly; ``fail_attempt`` keeps the reservation of
an attempt that may have reached the provider.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import connection
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.inference_policy import FIREWORKS_KEY_ENV
from django_ergo.conversation.inference_policy import SpendCapError
from django_ergo.pricing import ExactPrice
from django_ergo.pricing import exact_price

if TYPE_CHECKING:
    from django_ergo.conversation.inference_policy import PrivateGrant

MILLION = Decimal(1_000_000)
MAX_ERROR_CHARS = 2000


@dataclass
class LedgerEntry:
    pk: object
    price: ExactPrice | None


def redact(text: str) -> str:
    """``text`` without the private API key, should it ever appear in it."""
    key = os.environ.get(FIREWORKS_KEY_ENV, "")
    if key and len(key) >= 6:  # noqa: PLR2004 - never redact trivially short strings
        text = text.replace(key, "[redacted]")
    return text


def worst_case_usd(kwargs: dict, price: ExactPrice | None) -> Decimal | None:
    """An upper bound on what the request can cost, or None when it can't be
    bounded (no price, or no output limit)."""
    if price is None:
        return None
    limit = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return None
    body = {k: kwargs.get(k) for k in ("messages", "tools", "tool_choice")}
    # A token covers at least one byte, so the byte count bounds input tokens.
    input_bound = len(json.dumps(body, default=str).encode())
    return (input_bound * price.input + limit * price.output) / MILLION


def spent_today(pool: str) -> Decimal:
    """Today's (UTC) spend on ``pool``: priced attempts at their cost, attempts
    in flight or of unknown cost at their reservation."""
    from django_ergo.conversation.models import InferenceOutcome
    from django_ergo.conversation.models import InferenceRequest

    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = Decimal(0)
    rows = InferenceRequest.objects.filter(pool=pool, created_at__gte=start)
    for outcome, status, cost, reserved in rows.values_list(
        "outcome", "cost_status", "cost_usd", "reserved_usd"
    ):
        if outcome == InferenceOutcome.NOT_SENT:
            continue
        if status == "priced" and cost is not None:
            total += cost
        else:
            total += reserved or Decimal(0)
    return total


def _lock(pool: str) -> None:
    """Serialize cap checks for ``pool`` across processes (PostgreSQL)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", [f"ergo-pool:{pool}"]
            )


def _open(grant: PrivateGrant, kwargs: dict, attempt: int, call) -> LedgerEntry:
    from django_ergo.conversation.models import InferenceRequest

    pool = grant.pool
    price = exact_price(pool.provider, grant.model_id)
    reserved = worst_case_usd(kwargs, price)
    fields = {
        "structured_call_id": getattr(call, "pk", None),
        "session_id": grant.session_id or getattr(call, "session_id", None),
        "user_id": grant.user_id or getattr(call, "user_id", None),
        "bot_name": grant.bot,
        "selector": grant.selector,
        "pool": pool.name,
        "provider": pool.provider,
        "model": grant.model_id,
        "purpose": (getattr(call, "kind", "") or grant.purpose)[:32],
        "attempt": attempt,
        "rates": price.snapshot() if price else {},
        "reserved_usd": reserved,
        "cost_status": "pending",
    }
    cap = pool.daily_usd_cap
    if cap is None:
        return LedgerEntry(InferenceRequest.objects.create(**fields).pk, price)
    if price is None:
        msg = (
            f"{grant.model_id} has no exact price, so private pool {pool.name!r} "
            f"can't keep it under its daily_usd_cap; refusing to send"
        )
        raise SpendCapError(msg)
    if reserved is None:
        msg = "The request has no output token limit, so its cost can't be bounded; refusing to send"
        raise SpendCapError(msg)
    with transaction.atomic():
        _lock(pool.name)
        spent = spent_today(pool.name)
        if spent + reserved > cap:
            msg = (
                f"Private pool {pool.name!r} is at its daily cap: ${spent:.4f} spent or "
                f"reserved today (UTC), this request could cost up to ${reserved:.4f}, "
                f"cap ${cap}; refusing to send"
            )
            raise SpendCapError(msg)
        row = InferenceRequest.objects.create(**fields)
    return LedgerEntry(row.pk, price)


async def open_attempt(
    grant: PrivateGrant, kwargs: dict, *, attempt: int
) -> LedgerEntry:
    """Record (and, with a cap, reserve) an attempt before it is sent."""
    from django_ergo.conversation.structured import current_call

    call = current_call.get()
    return await sync_to_async(_open, thread_sensitive=True)(
        grant, kwargs, attempt, call
    )


def _usage_fields(response, price: ExactPrice | None) -> dict:
    from django_ergo.conversation.engines.openai_api import usage_parts

    usage = getattr(response, "usage", None)
    if usage is None:
        return {"cost_usd": None, "cost_status": "unknown"}
    parts = usage_parts(usage)
    cached = parts["cache_read_input_tokens"] or 0
    prompt = (
        (parts["input_tokens"] or 0)
        + cached
        + (parts["cache_creation_input_tokens"] or 0)
    )
    output = parts["output_tokens"] or 0
    cost = price.cost(prompt, cached, output) if price is not None else None
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": output,
        "reasoning_tokens": parts["reasoning_tokens"] or 0,
        "cost_usd": cost,
        "cost_status": "priced" if cost is not None else "unknown",
    }


async def finish_attempt(entry: LedgerEntry, response) -> None:
    from django_ergo.conversation.models import InferenceOutcome
    from django_ergo.conversation.models import InferenceRequest

    request_id = getattr(response, "_request_id", None) or getattr(response, "id", "")
    await InferenceRequest.objects.filter(pk=entry.pk).aupdate(
        outcome=InferenceOutcome.OK,
        upstream_request_id=str(request_id or "")[:200],
        finished_at=timezone.now(),
        **_usage_fields(response, entry.price),
    )


async def fail_attempt(entry: LedgerEntry, exc: BaseException, *, sent: bool) -> None:
    """An attempt that raised: refused before HTTP costs nothing; one that may
    have reached the provider keeps its reservation and an unknown cost."""
    from django_ergo.conversation.models import InferenceOutcome
    from django_ergo.conversation.models import InferenceRequest

    error = redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]
    await InferenceRequest.objects.filter(pk=entry.pk).aupdate(
        outcome=InferenceOutcome.ERROR if sent else InferenceOutcome.NOT_SENT,
        cost_usd=None if sent else Decimal(0),
        cost_status="unknown" if sent else "not_sent",
        error=error,
        finished_at=timezone.now(),
    )
