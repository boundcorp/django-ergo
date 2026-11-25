"""Spend authority for metered (API-billed) pool routes, checked before every
outbound request.

``bots.pools`` selects a route; when the route bills per token it attaches a
:class:`MeteredGuard` to the engine it builds. The OpenAI and Claude API
engines (and any engine that honours ``spend_guard``) call
:meth:`MeteredGuard.call` around each HTTP request:

- before sending it re-reads the pool's runtime budget and expiry and the
  provider's on/off switch, reserves the request's worst-case cost (every
  request byte as an uncached input token plus the full output limit) under
  a lock, and refuses (:class:`SpendDenied`, no HTTP) when the budget would
  be passed, has expired, isn't set, or the price or output limit is unknown;
- afterwards it prices the reported usage exactly, or keeps the reservation
  of a request that may have reached the provider.

Attempts are ``InferenceRequest`` rows with ``pool`` ``pool:<name>`` and
``billing_mode`` ``metered``. A configured API key is never authority: a
guard with ``deny_reason`` refuses every send (used for a metered route that
was not selected through a pool).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.db import connection
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.inference_policy import SpendCapError

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from django_ergo.pricing import ExactPrice

MILLION = Decimal(1_000_000)
LEDGER_PREFIX = "pool:"
# Anthropic bills cache writes at 1.25x input; count the extra so a priced
# attempt is never cheaper than what was billed.
CACHE_WRITE_EXTRA = Decimal("0.25")


class SpendDenied(SpendCapError):  # noqa: N818
    """A metered request refused before it was sent (budget, expiry, price,
    disabled provider or no authority). Never retried, never a fallback."""


def ledger_pool(name: str) -> str:
    return f"{LEDGER_PREFIX}{name}"[:64]


def spent_and_reserved(name: str) -> tuple[Decimal, Decimal]:
    """(priced spend, outstanding reservations) of a pool, all time. Runs the ORM."""
    from django_ergo.conversation.models import InferenceOutcome
    from django_ergo.conversation.models import InferenceRequest

    spent = Decimal(0)
    reserved = Decimal(0)
    rows = InferenceRequest.objects.filter(pool=ledger_pool(name))
    for outcome, status, cost, held in rows.values_list(
        "outcome", "cost_status", "cost_usd", "reserved_usd"
    ):
        if outcome == InferenceOutcome.NOT_SENT:
            continue
        if status == "priced" and cost is not None:
            spent += cost
        else:
            reserved += held or Decimal(0)
    return spent, reserved


def _lock(name: str) -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", [ledger_pool(name)]
            )


def current_limits(
    name: str, budget: Decimal | None, expires_at: dt.datetime | None
) -> tuple[Decimal | None, dt.datetime | None, bool]:
    """(budget, expiry, enabled) with the pool's runtime overrides applied."""
    from django_ergo.conversation.models import ModelPoolState

    state = ModelPoolState.objects.filter(name=name).first()
    if state is None:
        return budget, expires_at, True
    if state.budget_override:
        budget = state.budget_usd
    if state.expires_override:
        expires_at = state.expires_at
    return budget, expires_at, state.enabled is not False


def provider_switched_off(provider: str) -> bool:
    from django_ergo.conversation.models import ProviderToggle

    return ProviderToggle.objects.filter(name=provider, enabled=False).exists()


def worst_case(
    body: dict, limit: int | None, price: ExactPrice | None
) -> Decimal | None:
    if (
        price is None
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
    ):
        return None
    # A token covers at least one byte, so the byte count bounds input tokens.
    input_bound = len(json.dumps(body, default=str).encode())
    return (input_bound * price.input + limit * price.output) / MILLION


def reserve(  # noqa: PLR0913
    *,
    pool: str,
    provider: str,
    model: str,
    amount: Decimal | None,
    budget: Decimal | None,
    expires_at: dt.datetime | None,
    fields: dict,
) -> object:
    """Reserve ``amount`` against ``pool``'s budget and record the attempt;
    SpendDenied (nothing recorded) when it can't. Returns the row's pk."""
    from django_ergo.conversation.models import InferenceRequest

    if amount is None:
        msg = (
            f"{provider}/{model} has no exact price or the request has no output "
            f"limit, so pool {pool!r} can't bound its cost; refusing to send"
        )
        raise SpendDenied(msg)
    with transaction.atomic():
        _lock(pool)
        budget, expires_at, enabled = current_limits(pool, budget, expires_at)
        if not enabled:
            msg = f"Pool {pool!r} is switched off; refusing to send"
            raise SpendDenied(msg)
        if provider_switched_off(provider):
            msg = f"Provider {provider!r} is switched off; refusing to send"
            raise SpendDenied(msg)
        if expires_at is not None and timezone.now() >= expires_at:
            msg = f"Pool {pool!r}'s spend authority expired {expires_at:%Y-%m-%d %H:%M} UTC; refusing to send"
            raise SpendDenied(msg)
        if budget is None:
            msg = (
                f"Pool {pool!r} has metered routes but no budget_usd; refusing to send"
            )
            raise SpendDenied(msg)
        spent, held = spent_and_reserved(pool)
        if spent + held + amount > budget:
            msg = (
                f"Pool {pool!r} is at its budget: ${spent:.4f} spent and ${held:.4f} "
                f"reserved, this request could cost up to ${amount:.4f}, budget "
                f"${budget}; refusing to send"
            )
            raise SpendDenied(msg)
        row = InferenceRequest.objects.create(
            pool=ledger_pool(pool),
            provider=provider[:32],
            model=model[:200],
            billing_mode="metered",
            reserved_usd=amount,
            cost_status="pending",
            **fields,
        )
    return row.pk


def _usage_cost(usage, price: ExactPrice | None) -> dict:
    if usage is None:
        return {"cost_usd": None, "cost_status": "unknown"}

    def count(name):
        value = getattr(usage, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    if hasattr(usage, "cache_read_input_tokens"):  # Anthropic
        cached = count("cache_read_input_tokens")
        written = count("cache_creation_input_tokens")
        prompt = count("input_tokens") + cached + written
        output = count("output_tokens")
    else:  # OpenAI-compatible
        from django_ergo.conversation.engines.openai_api import usage_parts

        parts = usage_parts(usage)
        cached = parts["cache_read_input_tokens"] or 0
        written = parts["cache_creation_input_tokens"] or 0
        prompt = (parts["input_tokens"] or 0) + cached + written
        output = parts["output_tokens"] or 0
    cost = None
    if price is not None:
        cost = price.cost(prompt, cached, output) + (
            written * price.input * CACHE_WRITE_EXTRA / MILLION
        )
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "output_tokens": output,
        "cost_usd": cost,
        "cost_status": "priced" if cost is not None else "unknown",
    }


def _finish(pk, **fields) -> None:
    from django_ergo.conversation.models import InferenceRequest

    InferenceRequest.objects.filter(pk=pk).update(finished_at=timezone.now(), **fields)


@dataclass
class MeteredGuard:
    """Spend authority for one engine on one metered route of one pool."""

    pool: str
    provider: str
    model: str
    price: ExactPrice | None = None
    budget_usd: Decimal | None = None
    expires_at: dt.datetime | None = None
    max_output_tokens: int = 16_000
    bot: str = ""
    user_id: object = None
    session_id: object = None
    purpose: str = "chat"
    deny_reason: str = ""  # set: refuse every send with this reason
    attempts: int = 0

    async def call(self, kwargs: dict, output_key: str, send: Callable[[], Awaitable]):
        """Reserve, send, then price (or keep the reservation)."""
        from django_ergo.conversation.models import InferenceOutcome
        from django_ergo.conversation.structured import current_call

        if self.deny_reason:
            raise SpendDenied(self.deny_reason)
        sent_model = str(kwargs.get("model") or "")
        if sent_model and sent_model != self.model:
            msg = (
                f"Pool {self.pool!r} authorized {self.provider}/{self.model}, not "
                f"{sent_model!r}; refusing to send"
            )
            raise SpendDenied(msg)
        limit = kwargs.get(output_key)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = self.max_output_tokens
            kwargs[output_key] = limit
        body = {
            k: kwargs.get(k) for k in ("messages", "system", "tools", "tool_choice")
        }
        amount = worst_case(body, limit, self.price)
        call = current_call.get()
        self.attempts += 1
        fields = {
            "structured_call_id": getattr(call, "pk", None),
            "session_id": self.session_id or getattr(call, "session_id", None),
            "user_id": self.user_id or getattr(call, "user_id", None),
            "bot_name": self.bot[:100],
            "selector": f"{self.provider}/{self.model}"[:200],
            "purpose": (getattr(call, "kind", "") or self.purpose)[:32],
            "attempt": self.attempts,
            "rates": self.price.snapshot() if self.price else {},
        }
        pk = await sync_to_async(reserve, thread_sensitive=True)(
            pool=self.pool,
            provider=self.provider,
            model=self.model,
            amount=amount,
            budget=self.budget_usd,
            expires_at=self.expires_at,
            fields=fields,
        )
        try:
            response = await send()
        except BaseException as exc:
            await sync_to_async(_finish, thread_sensitive=True)(
                pk,
                outcome=InferenceOutcome.ERROR,
                cost_usd=None,
                cost_status="unknown",
                error=f"{type(exc).__name__}"[:2000],
            )
            raise
        request_id = getattr(response, "_request_id", None) or getattr(
            response, "id", ""
        )
        await sync_to_async(_finish, thread_sensitive=True)(
            pk,
            outcome=InferenceOutcome.OK,
            upstream_request_id=str(request_id or "")[:200],
            **_usage_cost(getattr(response, "usage", None), self.price),
        )
        return response


def reserve_launch(  # noqa: PLR0913
    *,
    pool: str,
    provider: str,
    model: str,
    amount: Decimal | None,
    budget: Decimal | None,
    expires_at: dt.datetime | None,
    bot: str = "",
    session_id=None,
    user_id=None,
) -> object:
    """Reserve a metered coding-agent launch's per-launch cap (kept until
    reconciled: the agent's own usage isn't priced here). Runs the ORM."""
    if amount is None:
        msg = f"Pool {pool!r} has no per_launch_usd, so a metered coding launch can't be bounded; refusing"
        raise SpendDenied(msg)
    return reserve(
        pool=pool,
        provider=provider,
        model=model,
        amount=amount,
        budget=budget,
        expires_at=expires_at,
        fields={
            "bot_name": bot[:100],
            "selector": f"{provider}/{model}"[:200],
            "purpose": "coding_launch",
            "session_id": session_id,
            "user_id": user_id,
        },
    )
