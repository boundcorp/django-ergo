"""Model prices, to turn the token counts on calls into dollars.

Prices are USD per million tokens. A model name matches the longest key it
starts with, so dated names (``claude-sonnet-5-5-20260101``) find their
model. Add or override prices in settings::

    DJANGO_ERGO = {
        "MODEL_PRICES": {
            "my-model": {"input": 1.0, "output": 4.0},
        },
    }

``cache_write`` and ``cache_read`` default to the input price. OpenAI counts
cached tokens inside ``input_tokens`` and Ergo doesn't split them out, so
OpenAI costs are priced at the full input rate (a slight overestimate).
"""

from __future__ import annotations

from dataclasses import dataclass

# Sources (checked 2026-10-02):
#   https://platform.claude.com/docs/en/about-claude/pricing
#   https://developers.openai.com/api/docs/models/gpt-6-luna
#   https://developers.openai.com/api/docs/models/gpt-6-sol (prompts over 272K input tokens
#   cost 2x input/cache and 1.5x output; not modelled)
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
    "gpt-6-sol": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "gpt-6-luna": {
        "input": 0.10,
        "output": 0.50,
        "cache_write": 0.125,
        "cache_read": 0.01,
    },
}


@dataclass
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        return (
            (input_tokens or 0) * self.input
            + (output_tokens or 0) * self.output
            + (cache_creation_input_tokens or 0) * self.cache_write
            + (cache_read_input_tokens or 0) * self.cache_read
        ) / 1_000_000


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
    )


def call_cost(call) -> float | None:
    """What a StructuredCall cost in USD, or None for an unpriced model."""
    price = price_for(call.model_name)
    if price is None:
        return None
    return price.cost(
        call.input_tokens,
        call.output_tokens,
        call.cache_creation_input_tokens,
        call.cache_read_input_tokens,
    )
