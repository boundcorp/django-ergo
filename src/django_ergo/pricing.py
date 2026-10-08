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

    def cost(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        *,
        one_request: bool = True,
    ) -> float:
        return sum(
            self.parts(
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
                one_request=one_request,
            ).values()
        )


def price_table() -> dict[str, dict[str, float]]:
    from django_ergo.settings import api_settings

    return {**DEFAULT_PRICES, **(api_settings.MODEL_PRICES or {})}


def price_for(model: str) -> Price | None:
    """The price of ``model``, or None when it isn't in the table."""
    if not model:
        return None
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
