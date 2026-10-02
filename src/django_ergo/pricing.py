"""Model prices, to turn the token counts on calls into dollars.

Prices are USD per million tokens. A model name matches the longest key it
starts with, so dated names (``claude-sonnet-5-5-20260101``) find their
model. Add or override prices in settings::

    DJANGO_ERGO = {
        "MODEL_PRICES": {
            "my-model": {"input": 1.0, "output": 4.0},
        },
    }

Token counts follow Claude's convention: ``input_tokens`` is the uncached input,
``cache_creation_input_tokens`` (cache writes) and ``cache_read_input_tokens``
(cached input) are counted apart, and ``output_tokens`` includes any reasoning.
OpenAI reports cached tokens inside its prompt tokens; its engine splits them
out (``engines.openai_api.usage_parts``). ``cache_write`` and ``cache_read``
default to the input price.

``long_context`` is a surcharge for big requests: once one request's input
(uncached + cache reads + cache writes) passes ``threshold`` tokens, the whole
request costs ``input`` times the input and cache rates and ``output`` times
the output rate. It applies per request, so calls are priced as they run
(``StructuredCall.cost_usd``).
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
        """The cost by part (USD) of ONE request, or (one_request=False) of summed
        totals, where per-request surcharges can't be told apart and don't apply."""
        input_tokens = input_tokens or 0
        written = cache_creation_input_tokens or 0
        cached = cache_read_input_tokens or 0
        in_rate = out_rate = 1.0
        surcharge = (self.long_context or {}) if one_request else {}
        if surcharge and input_tokens + written + cached > surcharge.get(
            "threshold", 0
        ):
            in_rate = float(surcharge.get("input", 1))
            out_rate = float(surcharge.get("output", 1))
        return {
            "input": input_tokens * self.input * in_rate / 1_000_000,
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
        """The cost of one request (or of summed totals, with one_request=False)."""
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


def call_cost(call) -> float | None:
    """What a StructuredCall cost in USD, or None for an unpriced model: the cost
    recorded request by request, else (older calls) priced from its totals."""
    if getattr(call, "cost_usd", None) is not None:
        return float(call.cost_usd)
    price = price_for(call.model_name)
    if price is None:
        return None
    return price.cost(
        call.input_tokens,
        call.output_tokens,
        call.cache_creation_input_tokens,
        call.cache_read_input_tokens,
        one_request=False,
    )


def request_parts(model: str, usage) -> dict[str, float] | None:
    """What one model request cost, by part; ``usage`` has Ergo's token fields (a
    Completion or a message row). None for an unpriced model."""
    price = price_for(model)
    if price is None:
        return None
    return price.parts(
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
        getattr(usage, "cache_creation_input_tokens", 0) or 0,
        getattr(usage, "cache_read_input_tokens", 0) or 0,
    )


def add_request_cost(call, model: str, usage) -> None:
    """Add one request's cost to a StructuredCall: ``cost_usd``, and its parts in
    ``metadata["cost_parts"]``. Once a request is unpriced the call stays unpriced
    (``cost_usd`` None; the Costs page then counts it as unpriced)."""
    from decimal import Decimal

    parts = request_parts(model, usage)
    if parts is None or getattr(call, "_ergo_unpriced", False):
        call.cost_usd = None
        call._ergo_unpriced = True  # noqa: SLF001 — in memory only, for this run of the call
        return
    meta = dict(call.metadata or {})
    totals = dict(meta.get("cost_parts") or {})
    for name, value in parts.items():
        totals[name] = round(totals.get(name, 0.0) + value, 8)
    meta["cost_parts"] = totals
    call.metadata = meta
    call.cost_usd = (call.cost_usd or Decimal(0)) + Decimal(
        str(round(sum(parts.values()), 8))
    )


def call_cost_parts(call) -> dict[str, float] | None:
    """A StructuredCall's cost by part (input, cache_write, cache_read, output)."""
    recorded = (call.metadata or {}).get("cost_parts")
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
