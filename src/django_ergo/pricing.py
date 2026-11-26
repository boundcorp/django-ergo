"""Model prices, to turn the token counts on calls into dollars.

Prices are USD per million tokens. A model name matches the longest key it
starts with, so dated names (``claude-sonnet-5-5-20260101``) find their
model. Add or override prices in settings::

    DJANGO_ERGO = {
        "MODEL_PRICES": {
            "my-model": {"input": 1.0, "output": 4.0},
        },
    }

``cache_write`` and ``cache_read`` default to the input price. Every call
records uncached input, cache writes, cache reads and output separately, so
each is billed at its own rate. Anthropic reports them that way; OpenAI counts
cached tokens and cache writes inside ``prompt_tokens``, so its engine splits
them out (``engines.openai_api.usage_parts``; calls recorded before that are
priced at the full input rate). Output includes reasoning, which is recorded
apart too (``reasoning_tokens``) but billed as output.

``long_context`` is a per-request surcharge: once one request's input (uncached
+ cache reads + cache writes) passes ``threshold`` tokens, that whole request
costs ``input`` times the input and cache rates and ``output`` times the output
rate. So calls are priced request by request as they run (``add_request_cost``,
``StructuredCall.cost_usd`` and ``metadata["cost_parts"]``); calls from before
that are priced from their totals, without the surcharge.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Sources (checked 2026-10-02):
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/models/gpt-6-luna
#   https://developers.openai.com/api/docs/models/gpt-6-sol
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {
        "input": 10,
        "output": 50,
        "cache_write": 12.5,
        "cache_read": 0.25,
    },
    "claude-mythos-5-1": {
        "input": 10,
        "output": 50,
        "cache_write": 12.5,
        "cache_read": 0.25,
    },
    "claude-fable-5": {"input": 10, "output": 50, "cache_write": 12.5, "cache_read": 1},
    "claude-mythos-5": {
        "input": 10,
        "output": 50,
        "cache_write": 12.5,
        "cache_read": 1,
    },
    "claude-opus-5-5": {"input": 4, "output": 20, "cache_write": 5, "cache_read": 0.2},
    "claude-opus-5": {"input": 5, "output": 25, "cache_write": 6.25, "cache_read": 0.5},
    "claude-opus-4-8": {
        "input": 5,
        "output": 25,
        "cache_write": 6.25,
        "cache_read": 0.5,
    },
    "claude-opus-4-7": {
        "input": 5,
        "output": 25,
        "cache_write": 6.25,
        "cache_read": 0.5,
    },
    "claude-opus-4-6": {
        "input": 5,
        "output": 25,
        "cache_write": 6.25,
        "cache_read": 0.5,
    },
    "claude-opus-4-5": {
        "input": 5,
        "output": 25,
        "cache_write": 6.25,
        "cache_read": 0.5,
    },
    "claude-opus-4-1": {
        "input": 15,
        "output": 75,
        "cache_write": 18.75,
        "cache_read": 1.5,
    },
    "claude-opus-4": {
        "input": 15,
        "output": 75,
        "cache_write": 18.75,
        "cache_read": 1.5,
    },
    "claude-sonnet-5-5": {
        "input": 2,
        "output": 10,
        "cache_write": 2.5,
        "cache_read": 0.2,
    },
    "claude-sonnet-5": {
        "input": 2,
        "output": 10,
        "cache_write": 2.5,
        "cache_read": 0.2,
    },
    "claude-sonnet-4-6": {
        "input": 3,
        "output": 15,
        "cache_write": 3.75,
        "cache_read": 0.3,
    },
    "claude-sonnet-4-5": {
        "input": 3,
        "output": 15,
        "cache_write": 3.75,
        "cache_read": 0.3,
    },
    "claude-sonnet-4": {
        "input": 3,
        "output": 15,
        "cache_write": 3.75,
        "cache_read": 0.3,
    },
    "claude-haiku-5-5": {
        "long_context": {"threshold": 100_000, "input": 5, "output": 5},
        "input": 0.10,
        "output": 0.50,
        "cache_write": 0.125,
        "cache_read": 0.01,
    },
    "claude-haiku-4-5": {
        "input": 1,
        "output": 5,
        "cache_write": 1.25,
        "cache_read": 0.1,
    },
    "claude-3-5-haiku": {
        "input": 0.8,
        "output": 4,
        "cache_write": 1,
        "cache_read": 0.08,
    },
    "gpt-6-luna": {
        "long_context": {"threshold": 272_000, "input": 2, "output": 1.5},
        "input": 0.10,
        "output": 0.50,
        "cache_write": 0.125,
        "cache_read": 0.01,
    },
    # Google Gemini API, Standard paid tier, USD per 1M tokens. Every row below
    # was read verbatim from https://aistudio.google.com/docs/pricing (the page
    # ai.google.dev/gemini-api/docs/pricing redirects to) on 2026-11-25; the page
    # carries no "last updated" date. Model status is from
    # https://aistudio.google.com/docs/models and /docs/deprecations, same day.
    # Output includes thinking tokens; cache_read is the cached-input rate
    # (text/image/video; audio is priced higher and isn't modeled here).
    # long_context multiplies a request's rates once its prompt passes
    # threshold: the page lists a ">200k tokens" tier for 2.5 Pro and 3.1 Pro.
    # Not listed because the page gives a price that changes on a date, which
    # this table can't express: gemini-4-carbon / gemini-4-argon (doubles
    # 2026-12-01), gemini-3.8-flash / gemini-3.6-flash (doubles 2027-01-01).
    # gemini-3-pro-preview is not here: Google lists it as shut down
    # (2026-03-09, replaced by gemini-3.1-pro-preview).
    # gemini-2.5-pro and gemini-2.5-flash are deprecated: from 2026-12-01 their
    # requests route to (and bill as) gemini-3.8-flash and gemini-3.5-flash-lite.
    "gemini-2.5-pro": {
        "long_context": {"threshold": 200_000, "input": 2, "output": 1.5},
        "input": 1.25,
        "output": 10.00,
        "cache_read": 0.125,
    },
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_read": 0.01},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-3.1-pro-preview": {
        "long_context": {"threshold": 200_000, "input": 2, "output": 1.5},
        "input": 2.00,
        "output": 12.00,
        "cache_read": 0.20,
    },
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cache_read": 0.025},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "cache_read": 0.05},
    "gemini-4-neon": {"input": 3.00, "output": 15.00, "cache_read": 0.15},
    "gemini-4-hydrogen": {"input": 1.50, "output": 7.50, "cache_read": 0.075},
    "gpt-6-sol": {
        "long_context": {"threshold": 272_000, "input": 2, "output": 1.5},
        "input": 2.00,
        "output": 10.00,
        "cache_write": 2.50,
        "cache_read": 0.20,
    },
}


@dataclass
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float
    long_context: dict | None = None

    def parts(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        *,
        one_request: bool = True,
    ) -> dict[str, float]:
        """Cost by part (USD) of ONE request, or (one_request=False) of summed totals,
        where per-request surcharges can't be told apart and don't apply."""
        uncached = input_tokens or 0
        written = cache_creation_input_tokens or 0
        cached = cache_read_input_tokens or 0
        in_rate = out_rate = 1.0
        surcharge = (self.long_context or {}) if one_request else {}
        if surcharge and uncached + written + cached > surcharge.get("threshold", 0):
            in_rate = float(surcharge.get("input", 1))
            out_rate = float(surcharge.get("output", 1))
        return {
            "input": uncached * self.input * in_rate / 1_000_000,
            "cache_write": written * self.cache_write * in_rate / 1_000_000,
            "cache_read": cached * self.cache_read * in_rate / 1_000_000,
            "output": (output_tokens or 0) * self.output * out_rate / 1_000_000,
        }


def price_table() -> dict[str, dict[str, float]]:
    from django_ergo.settings import api_settings

    return {**DEFAULT_PRICES, **(api_settings.MODEL_PRICES or {})}


def price_for(model: str) -> Price | None:
    """The price of ``model``, or None when it isn't in the table. Private
    (Fireworks-hosted) model IDs only match :data:`EXACT_API_PRICES` exactly."""
    if not model:
        return None
    from django_ergo.conversation.inference_policy import names_private_model

    if names_private_model(model):
        exact = next(
            (exact_price(p, m) for p, m in EXACT_API_PRICES if m == model), None
        )
        if exact is None:
            return None
        return Price(
            input=float(exact.input),
            output=float(exact.output),
            cache_write=float(exact.input),
            cache_read=float(exact.cached),
        )
    model = model.removesuffix("[1m]")
    table = price_table()
    matches = [key for key in table if model == key or model.startswith(key)]
    if not matches:
        return None
    row = table[max(matches, key=len)]
    base = float(row["input"])
    return Price(
        input=base,
        output=float(row["output"]),
        cache_write=float(row.get("cache_write", base)),
        cache_read=float(row.get("cache_read", base)),
        long_context=row.get("long_context"),
    )


def call_cost_parts(call) -> dict[str, float] | None:
    """A StructuredCall's cost by part (input, cache_write, cache_read, output): what was
    recorded request by request, else (older calls) priced from its totals. None if
    unpriced."""
    if getattr(call, "kind", None) == "voice_transcription":
        recorded = (getattr(call, "metadata", None) or {}).get("cost_parts")
        if recorded is None or getattr(call, "cost_usd", None) is None:
            return None
        return {k: float(v) for k, v in recorded.items()}
    if getattr(getattr(call, "session", None), "transport_type", None) == "cli":
        return dict.fromkeys(("input", "cache_write", "cache_read", "output"), 0.0)
    recorded = (getattr(call, "metadata", None) or {}).get("cost_parts")
    if recorded and getattr(call, "cost_usd", None) is not None:
        return {k: float(v) for k, v in recorded.items()}
    price = price_for(call.model_name)
    if price is None:
        return None
    return price.parts(
        call.input_tokens,
        call.output_tokens,
        call.cache_creation_input_tokens,
        call.cache_read_input_tokens,
        one_request=False,
    )


def call_cost(call) -> float | None:
    """What a StructuredCall cost in USD, or None for an unpriced model."""
    parts = call_cost_parts(call)
    return None if parts is None else sum(parts.values())


def add_request_cost(call, model: str, usage) -> None:
    """Price one model request and add it to a StructuredCall (``cost_usd`` and
    ``metadata["cost_parts"]``). ``usage`` has Ergo's token fields (a Completion or a
    message row). Once a request is unpriced, the call stays unpriced."""
    from decimal import Decimal

    if getattr(getattr(call, "session", None), "transport_type", None) == "cli":
        return

    fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    if not any(getattr(usage, f, 0) for f in fields):
        return  # not a model request (e.g. a pre-seeded tool call stored as a message)
    price = price_for(model)
    if price is None or getattr(call, "_ergo_unpriced", False):
        call.cost_usd = None
        call._ergo_unpriced = True  # noqa: SLF001 — in memory only, for this run of the call
        return
    parts = price.parts(
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
    )
    meta = dict(call.metadata or {})
    totals = dict(meta.get("cost_parts") or {})
    for name, value in parts.items():
        totals[name] = round(totals.get(name, 0.0) + value, 8)
    meta["cost_parts"] = totals
    call.metadata = meta
    call.cost_usd = (call.cost_usd or Decimal(0)) + Decimal(
        str(round(sum(parts.values()), 8))
    )


# API-billed private pool models (bots.providers ``private_pools``), USD per
# million tokens, keyed by (provider, exact model ID). Matched exactly, never by
# prefix, so a model ID that isn't listed is unpriced ("unknown"), not priced
# as some other family. Fireworks standard serverless, from the public model
# pages and https://docs.fireworks.ai/serverless/pricing (checked 2026-10-28;
# GLM 5.3 Flash checked 2026-10-29).
EXACT_PRICES_SOURCE = (
    "fireworks.ai standard serverless pricing, checked 2026-10-28 "
    "(GLM 5.3 Flash 2026-10-29)"
)
EXACT_API_PRICES: dict[tuple[str, str], dict[str, str]] = {
    ("fireworks", "accounts/fireworks/models/deepseek-v4p1-flash-1017"): {
        "input": "0.30",
        "cached": "0.006",
        "output": "1.20",
    },
    ("fireworks", "accounts/fireworks/models/deepseek-v4p1-flash"): {
        "input": "0.30",
        "cached": "0.006",
        "output": "1.20",
    },
    ("fireworks", "accounts/fireworks/models/glm-5p3"): {
        "input": "1.40",
        "cached": "0.26",
        "output": "4.40",
    },
    ("fireworks", "accounts/fireworks/models/glm-5p3-flash"): {
        "input": "0.15",
        "cached": "0.03",
        "output": "0.50",
    },
    ("fireworks", "accounts/fireworks/models/kimi-k3"): {
        "input": "3.00",
        "cached": "0.30",
        "output": "15.00",
    },
}


@dataclass(frozen=True)
class LongContextRates:
    """Rates that apply to the WHOLE request once its prompt (uncached,
    cached and cache-write input together) is over ``threshold`` tokens."""

    threshold: int
    input: Decimal
    cached: Decimal
    output: Decimal
    cache_write: Decimal | None = None


@dataclass(frozen=True)
class ExactPrice:
    """An exact tariff: USD per million uncached input, cached input and
    output tokens (reasoning is part of output), plus, when the provider bills
    them, cache writes (``cache_write``) and a long-context tier
    (``long_context``). This is the price schema metered pool routes use
    (providers.yaml ``price:``) and the one any engine adapter prices with
    (``conversation.spend_guard``)."""

    provider: str
    model: str
    input: Decimal
    cached: Decimal
    output: Decimal
    source: str = EXACT_PRICES_SOURCE
    cache_write: Decimal | None = None
    long_context: LongContextRates | None = None

    def snapshot(self) -> dict[str, str]:
        out = {
            "provider": self.provider,
            "model": self.model,
            "input": str(self.input),
            "cached": str(self.cached),
            "output": str(self.output),
            "unit": "USD per 1M tokens",
            "source": self.source,
        }
        if self.cache_write is not None:
            out["cache_write"] = str(self.cache_write)
        if self.long_context is not None:
            lc = self.long_context
            out["long_context"] = (
                f"over {lc.threshold}: input {lc.input}, cached {lc.cached}, "
                f"output {lc.output}, cache_write {lc.cache_write}"
            )
        return out

    def cost(
        self, prompt_tokens: int, cached_tokens: int, output_tokens: int
    ) -> Decimal:
        """USD for one request: ``prompt_tokens`` includes ``cached_tokens``."""
        cached = max(0, int(cached_tokens))
        uncached = max(0, int(prompt_tokens) - cached)
        return (
            uncached * self.input
            + cached * self.cached
            + max(0, int(output_tokens)) * self.output
        ) / Decimal(1_000_000)

    def rates(
        self, prompt_tokens: int
    ) -> tuple[Decimal, Decimal, Decimal | None, Decimal]:
        """(input, cached, cache_write, output) rates for a request whose
        whole prompt is ``prompt_tokens``."""
        lc = self.long_context
        if lc is not None and prompt_tokens > lc.threshold:
            return lc.input, lc.cached, lc.cache_write, lc.output
        return self.input, self.cached, self.cache_write, self.output

    def exact_cost(
        self, uncached: int, cached: int, cache_write: int, output: int
    ) -> Decimal | None:
        """USD for one request's complete usage, or None when it can't be
        priced exactly (cache writes billed without a ``cache_write`` rate)."""
        prompt = uncached + cached + cache_write
        rate_in, rate_cached, rate_write, rate_out = self.rates(prompt)
        if cache_write and rate_write is None:
            return None
        return (
            uncached * rate_in
            + cached * rate_cached
            + cache_write * (rate_write or Decimal(0))
            + output * rate_out
        ) / Decimal(1_000_000)

    def worst_case(
        self, input_tokens: int, output_tokens: int, *, cache_write: bool
    ) -> Decimal | None:
        """The most a request can cost with at most ``input_tokens`` of prompt
        and ``output_tokens`` of output: every prompt token at the dearest
        input rate that can apply (the long-context tier once the bound passes
        its threshold, a cache write when the request can write cache). None
        when a cache write is possible but has no rate."""
        tiers = [(self.input, self.cache_write, self.output)]
        lc = self.long_context
        if lc is not None and input_tokens > lc.threshold:
            tiers.append((lc.input, lc.cache_write, lc.output))
        worst_in = Decimal(0)
        worst_out = Decimal(0)
        for rate_in, rate_write, rate_out in tiers:
            if cache_write:
                if rate_write is None:
                    return None
                rate_in = max(rate_in, rate_write)  # noqa: PLW2901
            worst_in = max(worst_in, rate_in)
            worst_out = max(worst_out, rate_out)
        return (input_tokens * worst_in + output_tokens * worst_out) / Decimal(
            1_000_000
        )


def exact_price(provider: str, model: str) -> ExactPrice | None:
    """The price of exactly ``model`` on ``provider``, or None."""
    row = EXACT_API_PRICES.get((provider, model))
    if row is None:
        return None
    return ExactPrice(
        provider=provider,
        model=model,
        input=Decimal(row["input"]),
        cached=Decimal(row["cached"]),
        output=Decimal(row["output"]),
    )
