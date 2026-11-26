"""Spend authority for metered (API-billed) pool routes, checked before every
outbound request.

``bots.pools`` selects a route; when the route bills per token it attaches a
:class:`MeteredGuard` to the engine it builds, and the engine sends every HTTP
request through :meth:`MeteredGuard.call`:

- before sending it re-reads the grant (revoked, validity window, grantee,
  routes, budget), the pool's and the provider's on/off switches, clamps the request's output to the pool's
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

Spend authority is one :class:`~django_ergo.conversation.models.SpendGrant`
revision (``bots.pools``) of a
:class:`~django_ergo.conversation.models.SpendEnvelope`. Attempts are
``InferenceRequest`` rows with ``pool`` ``grant:<revision id>`` and
``billing_mode`` ``metered``; the budget is checked against the envelope's
cumulative total over all its revisions (revoked ones included, so uncertain
holds stay counted), under the envelope's advisory lock. Under that lock
every send also asks the caller's :class:`CurrentAuthority` for the
authority as it is now (current providers.yaml owner, budget, window,
routes and ceiling) and uses the intersection with the revision it was
built or queued with: narrowing only. A config removal or ownership change
therefore refuses guards that were already built. An envelope issued from
an owner's config envelope (``parent``) is also held to the owner's current
authority: its reservations take the parent's lock first, and the current
terms carry a family cap (``Terms.cap_envelope``/``cap_usd``), so the owner
and every grantee together stay within the owner's budget. A configured API key is
never authority: a guard with ``deny_reason`` refuses every send (used for a
metered route that was not selected through a pool).

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

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Protocol

from asgiref.sync import sync_to_async
from django.db import connection
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.inference_policy import SpendCapError

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Awaitable
    from collections.abc import Callable
    from decimal import Decimal

    from django_ergo.pricing import ExactPrice

LEDGER_PREFIX = "grant:"
# Per message, on top of the request's byte count (role and turn markers).
MESSAGE_OVERHEAD_TOKENS = 16


class SpendDenied(SpendCapError):  # noqa: N818
    """A metered request refused before it was sent (budget, expiry, price,
    disabled provider or no authority). Never retried, never a fallback."""


def ledger_key(grant_id) -> str:
    """The ledger key (``InferenceRequest.pool``) of the attempts one grant
    revision authorized."""
    return f"{LEDGER_PREFIX}{grant_id}"[:64]


def envelope_lock_key(envelope_id) -> str:
    """The advisory lock every reservation on one envelope takes."""
    return f"envelope:{envelope_id}"


def _envelope_of(grant_id):
    from django_ergo.conversation.models import SpendGrant

    return (
        SpendGrant.objects.filter(pk=grant_id)
        .values_list("envelope_id", flat=True)
        .first()
    )


def _locks_of(envelope_id) -> list[str]:
    """The advisory locks a reservation on ``envelope_id`` takes, in order:
    its family root's (the owner's config envelope an issued one is drawn
    from: every reservation in the family serializes on it), then its own.
    ``parent`` is set once, at creation, so it can be read before locking."""
    from django_ergo.conversation.models import SpendEnvelope

    parent = (
        SpendEnvelope.objects.filter(pk=envelope_id)
        .values_list("parent_id", flat=True)
        .first()
    )
    if parent is None:
        return [envelope_lock_key(envelope_id)]
    return [envelope_lock_key(parent), envelope_lock_key(envelope_id)]


def spent_and_reserved(grant_id, *, period=None, now=None) -> tuple[Decimal, Decimal]:
    """(priced spend, outstanding reservations) of the envelope ``grant_id``
    is a revision of: every revision's attempts, all time, revoked revisions
    included (their uncertain holds stay counted). Runs the ORM."""
    from django_ergo.conversation.models import SpendGrant

    envelope = _envelope_of(grant_id)
    revisions = (
        SpendGrant.objects.filter(envelope_id=envelope).values_list("pk", flat=True)
        if envelope is not None
        else [grant_id]
    )
    if period is None:
        period = SpendGrant.objects.get(pk=grant_id).budget_period
    return _totals(revisions, period=period, now=now)


def family_spent_and_reserved(
    envelope_id, *, period=None, now=None
) -> tuple[Decimal, Decimal]:
    """(priced spend, outstanding reservations) of the owner's config
    envelope ``envelope_id`` and every envelope issued from it: what the
    owner's budget caps. Runs the ORM."""
    from django.db.models import Q

    from django_ergo.conversation.models import SpendGrant

    revisions = SpendGrant.objects.filter(
        Q(envelope_id=envelope_id) | Q(envelope__parent_id=envelope_id)
    ).values_list("pk", flat=True)
    if period is None:
        current = (
            SpendGrant.objects.filter(
                envelope_id=envelope_id, superseded_at__isnull=True
            )
            .order_by("-created_at")
            .first()
        )
        period = current.budget_period if current else "cumulative"
    return _totals(revisions, period=period, now=now)


def _totals(revisions, *, period="cumulative", now=None) -> tuple[Decimal, Decimal]:
    from django_ergo.conversation.models import InferenceRequest
    from django_ergo.conversation.monthly_budgets import totals

    return totals(
        InferenceRequest.objects.filter(pool__in=[ledger_key(pk) for pk in revisions]),
        period=period,
        now=now,
    )


def lock(key: str) -> None:
    """Take a transaction-scoped advisory lock (PostgreSQL; a no-op elsewhere)."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [key])


@dataclass(frozen=True)
class Terms:
    """What spend authority allows: one revision's terms, or the
    intersection of several (``narrowed``)."""

    routes: frozenset[str]
    budget_usd: Decimal
    per_launch_usd: Decimal | None
    max_output_tokens: int
    valid_from: dt.datetime
    expires_at: dt.datetime
    # The family cap: the owner's config envelope (``cap_envelope``) whose
    # current budget (``cap_usd``) bounds the cumulative spend of it and
    # every envelope issued from it. None: no family cap (a root without
    # children is bounded by ``budget_usd`` alone).
    cap_envelope: object = None
    cap_usd: Decimal | None = None
    # The exact prices of the routes in the current config ({ref: price or
    # None}); a send prices with these, not with the guard's copy from when
    # it was built. None: not re-read (the guard's price applies).
    prices: dict | None = None
    budget_limits: dict = field(default_factory=dict)
    cap_period: str = "cumulative"
    account_identities: dict = field(default_factory=dict)

    @property
    def limits(self):
        return self.budget_limits or {"cumulative": self.budget_usd}

    @classmethod
    def of(cls, grant) -> Terms:
        return cls(
            routes=frozenset(grant.routes or ()),
            budget_usd=grant.budget_usd,
            per_launch_usd=grant.per_launch_usd,
            max_output_tokens=int(grant.max_output_tokens),
            valid_from=grant.valid_from,
            expires_at=grant.expires_at,
            budget_limits={grant.budget_period: grant.budget_usd},
        )

    def narrowed(self, other: Terms) -> Terms:
        """Both at once: never wider than either."""
        per_launch = (
            None
            if self.per_launch_usd is None or other.per_launch_usd is None
            else min(self.per_launch_usd, other.per_launch_usd)
        )
        caps = [t.cap_usd for t in (self, other) if t.cap_usd is not None]
        envelopes = {t.cap_envelope for t in (self, other)} - {None}
        if len(envelopes) > 1:
            msg = "spend terms of two families can't be intersected"
            raise ValueError(msg)
        limits = dict(self.limits)
        for period, budget in other.limits.items():
            limits[period] = min(limits.get(period, budget), budget)
        return Terms(
            routes=self.routes & other.routes,
            budget_usd=min(self.budget_usd, other.budget_usd),
            per_launch_usd=per_launch,
            max_output_tokens=min(self.max_output_tokens, other.max_output_tokens),
            valid_from=max(self.valid_from, other.valid_from),
            expires_at=min(self.expires_at, other.expires_at),
            cap_envelope=next(iter(envelopes), None),
            cap_usd=min(caps) if caps else None,
            prices=other.prices if other.prices is not None else self.prices,
            budget_limits=limits,
            cap_period=other.cap_period
            if other.cap_envelope is not None
            else self.cap_period,
            account_identities=other.account_identities or self.account_identities,
        )


class CurrentAuthority(Protocol):
    """Re-reads spend authority as it is now (``bots.pools`` provides it).

    Called under the envelope's lock with the revision a send was built or
    queued with; returns the current authority's :class:`Terms`, or why
    there is none any more (and may revoke revisions, which stay revoked
    even though the send is refused). ``refresh()``, when present, is called
    first, outside the lock (e.g. to reload changed config files)."""

    def __call__(self, grant) -> Terms | str: ...


def _authority(grant, user_id, current) -> tuple[Terms | None, str]:  # noqa: PLR0911
    """(the terms a send may use, "") or (None, why not). Under the lock."""
    if grant is None or grant.revoked_at is not None:
        return None, "the spend grant was revoked"
    if not user_id or grant.grantee_id != user_id:
        return None, "the spend grant belongs to another user"
    if (
        grant.envelope.grantee_id != user_id
        or grant.envelope.owner_id != grant.owner_id
    ):
        return None, "the spend grant belongs to another user"
    if current is None:
        return None, "the spend grant's current authority can't be re-read"
    found = current(grant)
    if isinstance(found, str):
        return None, found
    terms = Terms.of(grant).narrowed(found)
    now = timezone.now()  # after re-reading: a revision made just now counts
    if now < terms.valid_from:
        return None, f"the spend grant starts {terms.valid_from:%Y-%m-%d %H:%M} UTC"
    if now >= terms.expires_at:
        return None, f"the spend grant expired {terms.expires_at:%Y-%m-%d %H:%M} UTC"
    if pool_switched_off(grant.pool):
        return None, f"pool {grant.pool!r} is switched off"
    return terms, ""


def authorize(*, grant_id, user_id, current: CurrentAuthority | None) -> Terms:
    """The terms a send on ``grant_id`` may use now (under the envelope's
    lock; revocations it makes are kept), or SpendDenied. Runs the ORM."""
    from django_ergo.conversation.models import SpendGrant

    envelope = _envelope_of(grant_id)
    if envelope is None:
        msg = "the spend grant was revoked; refusing to send"
        raise SpendDenied(msg)
    with transaction.atomic():
        for key in _locks_of(envelope):
            lock(key)
        grant = (
            SpendGrant.objects.select_related("envelope").filter(pk=grant_id).first()
        )
        terms, problem = _authority(grant, user_id, current)
    if problem:
        msg = f"{problem}; refusing to send"
        raise SpendDenied(msg)
    return terms


def pool_switched_off(name: str) -> bool:
    from django_ergo.conversation.models import ModelPoolState

    return ModelPoolState.objects.filter(name=name, enabled=False).exists()


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

    @staticmethod
    def _check_cache_control(block: dict) -> None:
        """Only the default 5-minute ephemeral cache is priced (``cache_write``);
        a 1-hour write bills a higher rate, so it is refused."""
        control = block.get("cache_control")
        if control is None:
            return
        if (
            not isinstance(control, dict)
            or control.get("type") != "ephemeral"
            or control.get("ttl", "5m") != "5m"
        ):
            msg = (
                "only the default 5-minute prompt cache is priced; "
                f"cache_control {control!r} isn't"
            )
            raise UnsupportedRequest(msg)

    def _check_blocks(self, blocks) -> bool:
        """Raise for a block it can't bound; True when one asks for caching."""
        caches = False
        for block in blocks if isinstance(blocks, list) else ():
            if not isinstance(block, dict):
                msg = f"a {type(block).__name__} content block can't be bounded; send dicts"
                raise UnsupportedRequest(msg)
            self._check_cache_control(block)
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
            self._check_cache_control(tool)
            caches = caches or "cache_control" in tool
        caches = self._check_blocks(kwargs.get("system")) or caches
        messages = kwargs.get("messages") or []
        for message in messages:
            if not isinstance(message, dict):
                msg = f"a {type(message).__name__} message can't be bounded; send dicts"
                raise UnsupportedRequest(msg)
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
        creation = getattr(usage, "cache_creation", None)
        if creation is not None and getattr(
            creation, "ephemeral_1h_input_tokens", 0
        ) not in (None, 0):
            return None  # 1-hour writes bill a rate the tariff doesn't have
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


def reserve(  # noqa: PLR0913, PLR0912, PLR0915, C901
    *,
    grant_id,
    provider: str,
    model: str,
    amount: Decimal | None,
    user_id,
    fields: dict,
    current: CurrentAuthority | None = None,
    output_tokens: int | None = None,
    launch: bool = False,
    price: ExactPrice | None = None,
) -> object:
    """Reserve ``amount`` against the envelope of revision ``grant_id`` and
    record the attempt; SpendDenied (nothing recorded) when it can't. Under
    the envelope's lock it re-reads the revision (revoked, grantee), asks
    ``current`` for the authority as it is now and intersects the two
    (routes, window, budget, output ceiling, per-launch amount), checks the
    pool's and the provider's switches, and compares ``amount`` with the
    envelope's cumulative spend and holds. Returns the row's pk."""
    from django_ergo.conversation.models import InferenceRequest
    from django_ergo.conversation.models import SpendGrant

    ref = f"{provider}/{model}"
    if amount is None:
        msg = (
            f"{ref} has no exact price, so its cost can't be bounded; refusing to send"
        )
        raise SpendDenied(msg)
    if grant_id is None:
        msg = f"{ref} bills per token and no spend grant covers it; refusing to send"
        raise SpendDenied(msg)
    envelope = _envelope_of(grant_id)
    if envelope is None:
        msg = f"{ref}: the spend grant was revoked; refusing to send"
        raise SpendDenied(msg)
    msg = ""
    locks = _locks_of(envelope)
    from django_ergo.conversation import monthly_budgets as monthly

    captured_identity = getattr(current, "identity", "")
    captured_family = (
        monthly.family_of(captured_identity) if captured_identity else provider
    )
    with transaction.atomic():
        monthly.lock_family(captured_family)
        for key in locks:
            lock(key)
        grant = (
            SpendGrant.objects.select_related("envelope").filter(pk=grant_id).first()
        )
        terms, problem = _authority(grant, user_id, current)
        if not problem:
            if ref not in terms.routes:
                problem = f"the spend grant doesn't cover {ref}"
            elif output_tokens is not None and output_tokens > terms.max_output_tokens:
                problem = f"the output ceiling is now {terms.max_output_tokens} tokens"
            elif launch and "calendar_month_utc" in terms.limits:
                problem = (
                    "recurring API funding does not authorize paid coding launches"
                )
            elif launch and (
                terms.per_launch_usd is None or amount > terms.per_launch_usd
            ):
                problem = "the launch amount is over the grant's per-launch limit"
            elif provider_switched_off(provider):
                problem = f"provider {provider!r} is switched off"
            elif (
                price is not None
                and terms.prices is not None
                and terms.prices.get(ref) != price
            ):
                problem = "its price changed while the request was being bounded"
            elif terms.cap_envelope is not None and (
                envelope_lock_key(terms.cap_envelope) not in locks
                or terms.cap_usd is None
            ):
                problem = "the owner's spend authority can't be checked"
        if problem:
            # Leave the block normally: revocations made while re-reading stay.
            msg = f"{ref}: {problem}; refusing to send"
        else:
            identity = terms.account_identities.get(ref, captured_identity)
            if identity and monthly.family_of(identity) != captured_family:
                msg = "Provider/account changed while acquiring budget locks"
                raise SpendDenied(msg)
            reservation_time = timezone.now()
            monthly_fields = monthly.reservation_context(
                identity,
                amount,
                required=monthly.MONTHLY in terms.limits,
                now=reservation_time,
            )
            limits = [
                (
                    period,
                    budget,
                    *spent_and_reserved(grant_id, period=period, now=reservation_time),
                )
                for period, budget in terms.limits.items()
            ]
            exceeded = next(
                (
                    (budget, spent, held)
                    for _, budget, spent, held in limits
                    if spent + held + amount > budget
                ),
                None,
            )
            budget, spent, held = exceeded or (
                terms.budget_usd,
                limits[0][2],
                limits[0][3],
            )
            if terms.cap_envelope is not None:
                family_spent, family_held = family_spent_and_reserved(
                    terms.cap_envelope, period=terms.cap_period, now=reservation_time
                )
            if exceeded:
                msg = (
                    f"Pool {grant.pool!r} grant is at its budget: ${spent:.4f} spent "
                    f"and ${held:.4f} reserved, this request could cost up to "
                    f"${amount:.4f}, budget ${budget}; refusing to send"
                )
            elif (
                terms.cap_envelope is not None
                and family_spent + family_held + amount > terms.cap_usd
            ):
                msg = (
                    f"Pool {grant.pool!r} owner's budget is used: ${family_spent:.4f} "
                    f"spent and ${family_held:.4f} reserved by the owner and every "
                    f"grant they issued, this request could cost up to "
                    f"${amount:.4f}, owner's budget ${terms.cap_usd}; refusing to send"
                )
            else:
                row = InferenceRequest.objects.create(
                    pool=ledger_key(grant_id),
                    provider=provider[:32],
                    model=model[:200],
                    billing_mode="metered",
                    reserved_usd=amount,
                    cost_status="pending",
                    **fields,
                    **monthly_fields,
                )
    if msg:
        raise SpendDenied(msg)
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
    from django_ergo.conversation.monthly_budgets import lock_family

    family = (
        InferenceRequest.objects.filter(pk=pk)
        .values_list("provider_family_id", flat=True)
        .first()
    )
    with transaction.atomic():
        if family:
            lock_family(family)
        # Settlement serializes with reservations on the original grant family too.
        row = InferenceRequest.objects.get(pk=pk)
        grant_id = (
            row.pool.removeprefix(LEDGER_PREFIX)
            if row.pool.startswith(LEDGER_PREFIX)
            else None
        )
        envelope = _envelope_of(grant_id) if grant_id else None
        if envelope:
            for key in _locks_of(envelope):
                lock(key)
        InferenceRequest.objects.filter(pk=pk).update(
            finished_at=timezone.now(), **fields
        )


def paid_dispatch_problem(*, legacy: bool = False) -> str:
    """Fail closed if the persistent latch cannot be read (e.g. old schema)."""
    from django.db import DatabaseError

    from django_ergo.conversation.models import PaidDispatchState

    try:
        state = PaidDispatchState.objects.filter(pk=1).first()
    except DatabaseError:
        return "Paid dispatch state cannot be verified; paid dispatch is paused"
    if state is not None:
        if state.paused:
            return "Paid dispatch is paused; all grants and holds are retained"
        if legacy and state.activated_at is not None:
            return "Pool enforcement was activated; removing pools cannot restore unguarded paid dispatch"
    return ""


async def check_unguarded_dispatch():
    """Legacy SDK sends are permitted only before first pool activation."""
    problem = await sync_to_async(paid_dispatch_problem, thread_sensitive=True)(
        legacy=True
    )
    if problem:
        raise SpendDenied(problem)


@dataclass
class MeteredGuard:
    """Spend authority for one engine on one metered route of one pool."""

    pool: str
    provider: str
    model: str
    grant_id: object = None  # the SpendGrant revision every send reserves under
    current: CurrentAuthority | None = None  # re-reads authority at each send
    price: ExactPrice | None = None
    max_output_tokens: int = 16_000
    bot: str = ""
    user_id: object = None
    session_id: object = None
    purpose: str = "chat"
    deny_reason: str = ""  # set: refuse every send with this reason
    attempts: int = 0
    transport_identity: str = ""

    def check_transport(self, identity: str) -> None:
        """Adapters call before each attempt with their captured non-secret identity."""
        if self.transport_identity and identity != self.transport_identity:
            msg = "Engine transport/account differs from the selected authority; rebuild the engine"
            raise SpendDenied(msg)

    async def call(  # noqa: C901, PLR0915
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
        if self.grant_id is None:
            msg = (
                f"{self.provider}/{self.model} bills per token and no spend grant "
                "covers it; refusing to send"
            )
            raise SpendDenied(msg)
        refresh = getattr(self.current, "refresh", None)
        if callable(refresh):
            await sync_to_async(refresh, thread_sensitive=True)()
        # The current authority's ceiling, before bounding (reserve re-checks).
        terms = await sync_to_async(authorize, thread_sensitive=True)(
            grant_id=self.grant_id, user_id=self.user_id, current=self.current
        )
        ceiling = min(self.max_output_tokens, terms.max_output_tokens)
        price = self.price
        if terms.prices is not None:
            # The price as providers.yaml has it now (a stale process's copy
            # may be lower, or name a price that was removed).
            price = terms.prices.get(f"{self.provider}/{self.model}")
            if price is None:
                msg = (
                    f"{self.provider}/{self.model} has no exact price in the current "
                    "providers.yaml; refusing to send"
                )
                raise SpendDenied(msg)
        bind_price = getattr(adapter, "with_price", None)
        if callable(bind_price):
            adapter = bind_price(price)
        try:
            bound = adapter.bound(kwargs, ceiling)
        except UnsupportedRequest as exc:
            msg = f"Pool {self.pool!r} can't bound this request's cost: {exc}; refusing to send"
            raise SpendDenied(msg) from exc
        amount = (
            price.worst_case(
                bound.input_tokens, bound.output_tokens, cache_write=bound.cache_write
            )
            if price is not None
            else None
        )
        if amount is None and price is not None:
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
            "rates": price.snapshot() if price else {},
        }
        pk = await sync_to_async(reserve, thread_sensitive=True)(
            grant_id=self.grant_id,
            provider=self.provider,
            model=self.model,
            amount=amount,
            user_id=self.user_id,
            fields=fields,
            current=self.current,
            output_tokens=bound.output_tokens,
            price=price,
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
            **priced(usage, price),
        )
        return response


def reserve_launch(  # noqa: PLR0913
    *,
    grant_id,
    pool: str,
    provider: str,
    model: str,
    amount: Decimal | None,
    bot: str = "",
    session_id=None,
    user_id=None,
    current: CurrentAuthority | None = None,
) -> object:
    """Reserve a metered coding-agent launch's per-launch cap against its
    grant's envelope (kept until reconciled: the agent's own usage isn't
    priced here). Runs the ORM."""
    if amount is None:
        msg = f"Pool {pool!r} has no per_launch_usd, so a metered coding launch can't be bounded; refusing"
        raise SpendDenied(msg)
    return reserve(
        grant_id=grant_id,
        provider=provider,
        model=model,
        amount=amount,
        user_id=user_id,
        current=current,
        launch=True,
        fields={
            "bot_name": bot[:100],
            "selector": f"{provider}/{model}"[:200],
            "purpose": "coding_launch",
            "session_id": session_id,
            "user_id": user_id,
        },
    )
