"""Spend authority for metered (API-billed) pool routes, checked before every
outbound request.

``bots.pools`` selects a route; when the route bills per token it attaches a
:class:`MeteredGuard` to the engine it builds, and the engine sends every HTTP
request through :meth:`MeteredGuard.call`:

- before sending it re-reads the pool's runtime budget and expiry and the
  provider's on/off switch, clamps the request's output to the pool's
  ceiling, asks the engine's :class:`SpendAdapter` for a proven bound on the
  request (or refuses a request class it can't bound: remote images, audio,
  files, server-side tools, several choices), reserves the worst-case cost of
  that bound at the route's exact tariff (``pricing.ExactPrice``) under a
  lock, and refuses (:class:`SpendDenied`, no HTTP) when the budget would be
  passed, has expired, isn't set, or the price is unknown;
- afterwards it prices the adapter's parsed usage exactly, or keeps the
  reservation when the outcome or usage is unknown, partial or malformed.

One guarded call is one HTTP attempt: engines turn their SDK's own retries
off under a guard, so a retry is another call with its own reservation.

Attempts are ``InferenceRequest`` rows with ``pool`` ``pool:<name>`` and
``billing_mode`` ``metered``. A configured API key is never authority: a
guard with ``deny_reason`` refuses every send (used for a metered route that
was not selected through a pool).

Engine adapter contract
-----------------------

An engine can carry a metered pool route only if its class sets
``spend_adapter`` to an object implementing :class:`SpendAdapter`; a metered
route on an engine without one is refused before it is built
(``bots.pools.route_problem``). The OpenAI Chat Completions and Anthropic
Messages engines use :class:`OpenAIChatAdapter` and
:class:`AnthropicMessagesAdapter`. A new engine (e.g. Gemini) implements:

- ``output_key``: the request key of its output-token limit (informational);
- ``bound(kwargs, ceiling) -> RequestBound``: clamp the request's output
  limit to ``ceiling`` IN PLACE (set it when absent) and return an upper
  bound on its prompt tokens and output tokens and whether it can write
  cache; raise :class:`UnsupportedRequest` for anything it can't bound
  (modalities, server-side tool fees, several candidates, opaque options);
- ``usage(response) -> Usage | None``: the billed usage of a response, or
  None when it is missing, partial, negative or inconsistent (the
  reservation is then kept), or when it reports fees the tariff can't price.

and calls ``await self.spend_guard.call(kwargs, self.spend_adapter, send)``
around each single HTTP attempt, with its client's automatic retries off.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Protocol

from asgiref.sync import sync_to_async
from django.db import connection
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.inference_policy import SpendCapError

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from django_ergo.pricing import ExactPrice

LEDGER_PREFIX = "pool:"
# Per message, on top of the request's byte count (role and turn markers).
MESSAGE_OVERHEAD_TOKENS = 16


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


class UnsupportedRequest(ValueError):  # noqa: N818
    """A request a spend adapter can't bound; it is refused before sending."""


@dataclass(frozen=True)
class RequestBound:
    """Upper bounds for one request: prompt tokens (every input part,
    including tools and overhead), output tokens (reasoning included), and
    whether it can write the provider's prompt cache."""

    input_tokens: int
    output_tokens: int
    cache_write: bool = False


@dataclass(frozen=True)
class Usage:
    """One response's complete billed usage. ``uncached`` excludes ``cached``
    and ``cache_write``; ``output`` includes reasoning."""

    uncached: int
    cached: int
    cache_write: int
    output: int

    @property
    def prompt(self) -> int:
        return self.uncached + self.cached + self.cache_write


class SpendAdapter(Protocol):
    """What an engine provides to send under a :class:`MeteredGuard`."""

    output_key: str

    def bound(self, kwargs: dict, ceiling: int) -> RequestBound: ...

    def usage(self, response) -> Usage | None: ...


def _count(value) -> int | None:
    """A billed token count: a non-negative int, else None (malformed)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _clamp(kwargs: dict, key: str, ceiling: int) -> int:
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
        msg = "the pool has no positive output-token ceiling"
        raise UnsupportedRequest(msg)
    wanted = kwargs.get(key)
    if wanted is None:
        limit = ceiling
    elif isinstance(wanted, bool) or not isinstance(wanted, int) or wanted <= 0:
        msg = f"{key} must be a positive integer"
        raise UnsupportedRequest(msg)
    else:
        limit = min(wanted, ceiling)
    kwargs[key] = limit
    return limit


def _input_bound(body: dict, messages) -> int:
    from django_ergo.conversation.budget import input_upper_bound

    # A token covers at least one byte of text, so the JSON byte count bounds
    # text input; the fixed overhead covers role markers and tool preambles.
    count = len(messages) if isinstance(messages, list) else 0
    return input_upper_bound(body) + MESSAGE_OVERHEAD_TOKENS * count


def _unsupported_keys(kwargs: dict, allowed: frozenset[str]) -> None:
    if extra := sorted(set(kwargs) - allowed):
        msg = (
            f"request options {', '.join(extra)} can change what is billed and "
            "aren't supported under a spend guard"
        )
        raise UnsupportedRequest(msg)


class OpenAIChatAdapter:
    """OpenAI Chat Completions (``chat.completions.create``)."""

    output_key = "max_completion_tokens"
    allowed = frozenset(
        {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "max_completion_tokens",
            "max_tokens",
            "temperature",
            "top_p",
            "reasoning_effort",
            "response_format",
            "stop",
            "seed",
            "user",
            "metadata",
            "store",
            "stream",
            "stream_options",
            "prompt_cache_key",
            "verbosity",
        }
    )
    content_types = frozenset({"text", "refusal"})

    def bound(self, kwargs: dict, ceiling: int) -> RequestBound:
        _unsupported_keys(kwargs, self.allowed)
        if "max_tokens" in kwargs:
            # Older name of the same limit: keep only one, clamped.
            legacy = kwargs.pop("max_tokens")
            kwargs.setdefault(self.output_key, legacy)
        for tool in kwargs.get("tools") or []:
            if not isinstance(tool, dict) or tool.get("type", "function") != "function":
                msg = "only function tools can be sent under a spend guard (server-side tools bill fees)"
                raise UnsupportedRequest(msg)
        messages = kwargs.get("messages") or []
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for part in content:
                    kind = part.get("type") if isinstance(part, dict) else None
                    if kind not in self.content_types:
                        msg = (
                            f"{kind or 'non-text'} input has no exact token bound; "
                            "a metered pool route only sends text"
                        )
                        raise UnsupportedRequest(msg)
        output = _clamp(kwargs, self.output_key, ceiling)
        body = {
            k: kwargs.get(k)
            for k in ("messages", "tools", "tool_choice", "response_format")
        }
        return RequestBound(_input_bound(body, messages), output, cache_write=False)

    def usage(self, response) -> Usage | None:  # noqa: C901, PLR0911
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        prompt = _count(getattr(usage, "prompt_tokens", None))
        output = _count(getattr(usage, "completion_tokens", None))
        if prompt is None or output is None:
            return None
        total = getattr(usage, "total_tokens", None)
        if total is not None and _count(total) != prompt + output:
            return None
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            raw = getattr(details, "cached_tokens", None)
            if raw is not None:
                cached = _count(raw)
                if cached is None or cached > prompt:
                    return None
            for name in ("audio_tokens", "image_tokens"):
                extra = getattr(details, name, None)
                if extra not in (None, 0):
                    return None  # billed at rates the tariff doesn't have
        out_details = getattr(usage, "completion_tokens_details", None)
        if out_details is not None:
            reasoning = getattr(out_details, "reasoning_tokens", None)
            if reasoning is not None and (
                _count(reasoning) is None or reasoning > output
            ):
                return None
            if getattr(out_details, "audio_tokens", None) not in (None, 0):
                return None
        return Usage(
            uncached=prompt - cached, cached=cached, cache_write=0, output=output
        )


class AnthropicMessagesAdapter:
    """Anthropic Messages (``messages.create``)."""

    output_key = "max_tokens"
    allowed = frozenset(
        {
            "model",
            "messages",
            "system",
            "tools",
            "tool_choice",
            "max_tokens",
            "temperature",
            "top_p",
            "top_k",
            "stop_sequences",
            "metadata",
            "thinking",
            "stream",
        }
    )
    block_types = frozenset(
        {"text", "tool_use", "tool_result", "thinking", "redacted_thinking"}
    )

    def _check_blocks(self, blocks) -> bool:
        """Raise for a block it can't bound; True when one asks for caching."""
        caches = False
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind not in self.block_types:
                msg = (
                    f"{kind or 'unknown'} content has no exact token bound; a "
                    "metered pool route only sends text and tool calls"
                )
                raise UnsupportedRequest(msg)
            caches = caches or "cache_control" in block
            if kind == "tool_result":
                caches = self._check_blocks(block.get("content")) or caches
        return caches

    def bound(self, kwargs: dict, ceiling: int) -> RequestBound:
        _unsupported_keys(kwargs, self.allowed)
        caches = False
        for tool in kwargs.get("tools") or []:
            if not isinstance(tool, dict) or tool.get("type", "custom") != "custom":
                msg = "only client tools can be sent under a spend guard (server-side tools bill fees)"
                raise UnsupportedRequest(msg)
            caches = caches or "cache_control" in tool
        caches = self._check_blocks(kwargs.get("system")) or caches
        messages = kwargs.get("messages") or []
        for message in messages:
            if isinstance(message, dict):
                caches = self._check_blocks(message.get("content")) or caches
        output = _clamp(kwargs, self.output_key, ceiling)
        thinking = kwargs.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            budget = thinking.get("budget_tokens")
            if not isinstance(budget, int) or budget >= output:
                msg = (
                    f"the thinking budget ({budget}) doesn't fit in the pool's "
                    f"output ceiling ({output})"
                )
                raise UnsupportedRequest(msg)
        body = {
            k: kwargs.get(k) for k in ("messages", "system", "tools", "tool_choice")
        }
        return RequestBound(_input_bound(body, messages), output, cache_write=caches)

    def usage(self, response) -> Usage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        uncached = _count(getattr(usage, "input_tokens", None))
        output = _count(getattr(usage, "output_tokens", None))
        if uncached is None or output is None:
            return None
        parts = []
        for name in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            raw = getattr(usage, name, None)
            value = 0 if raw is None else _count(raw)
            if value is None:
                return None
            parts.append(value)
        server = getattr(usage, "server_tool_use", None)
        if server is not None and any(
            getattr(server, name, 0) not in (None, 0)
            for name in ("web_search_requests", "web_fetch_requests")
        ):
            return None  # tool fees the tariff doesn't price
        return Usage(
            uncached=uncached, cached=parts[0], cache_write=parts[1], output=output
        )


ADAPTERS = {
    OpenAIChatAdapter.output_key: OpenAIChatAdapter(),
    AnthropicMessagesAdapter.output_key: AnthropicMessagesAdapter(),
}


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


def priced(usage: Usage | None, price: ExactPrice | None) -> dict:
    """Ledger fields for a finished attempt: its exact cost, or ``unknown``
    (the reservation is kept) without complete usage or an exact price."""
    if usage is None:
        return {"cost_usd": None, "cost_status": "unknown"}
    cost = (
        price.exact_cost(usage.uncached, usage.cached, usage.cache_write, usage.output)
        if price is not None
        else None
    )
    return {
        "prompt_tokens": usage.prompt,
        "cached_tokens": usage.cached,
        "output_tokens": usage.output,
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

    async def call(
        self,
        kwargs: dict,
        adapter: SpendAdapter | str,
        send: Callable[[], Awaitable],
    ):
        """Reserve, send ONE attempt, then price it (or keep the reservation).
        ``adapter`` is the engine's :class:`SpendAdapter` (or, for the two
        built-in engines, its output-token key)."""
        from django_ergo.conversation.models import InferenceOutcome
        from django_ergo.conversation.structured import current_call

        if self.deny_reason:
            raise SpendDenied(self.deny_reason)
        if isinstance(adapter, str):
            adapter = ADAPTERS[adapter]
        sent_model = str(kwargs.get("model") or "")
        if sent_model != self.model:
            msg = (
                f"Pool {self.pool!r} authorized {self.provider}/{self.model}, not "
                f"{sent_model or 'an unnamed model'!r}; refusing to send"
            )
            raise SpendDenied(msg)
        try:
            bound = adapter.bound(kwargs, self.max_output_tokens)
        except UnsupportedRequest as exc:
            msg = f"Pool {self.pool!r} can't bound this request's cost: {exc}; refusing to send"
            raise SpendDenied(msg) from exc
        amount = (
            self.price.worst_case(
                bound.input_tokens, bound.output_tokens, cache_write=bound.cache_write
            )
            if self.price is not None
            else None
        )
        if amount is None and self.price is not None:
            msg = (
                f"{self.provider}/{self.model} can write prompt cache but its price "
                "has no cache_write rate; refusing to send"
            )
            raise SpendDenied(msg)
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
            # Whether it reached the provider is unknown: keep the reservation.
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
        try:
            usage = adapter.usage(response)
        except Exception:  # noqa: BLE001 - unparseable usage keeps the hold
            usage = None
        await sync_to_async(_finish, thread_sensitive=True)(
            pk,
            outcome=InferenceOutcome.OK,
            upstream_request_id=str(request_id or "")[:200],
            **priced(usage, self.price),
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
