from types import SimpleNamespace

import pytest

from django_ergo.pricing import call_cost
from django_ergo.pricing import price_for


def call(model, input_tokens=0, output_tokens=0, cache_write=0, cache_read=0):
    return SimpleNamespace(
        model_name=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


def test_longest_prefix_wins_and_dated_names_match():
    assert price_for("claude-opus-5-5").input == 4
    assert price_for("claude-opus-5").input == 5
    assert price_for("claude-opus-5-5-20260101").output == 20
    assert price_for("gpt-6-luna-2026-09-01").output == 0.5
    assert price_for("gpt-6-sol").input == 2
    assert price_for("mystery-model") is None
    assert price_for("") is None


def test_call_cost_counts_every_token_kind():
    cost = call_cost(call("claude-sonnet-5-5", 1_000_000, 100_000, 200_000, 1_000_000))
    assert cost == pytest.approx(2 + 1 + 0.5 + 0.2)
    assert call_cost(call("gpt-6-luna", 2_000_000, 1_000_000)) == pytest.approx(0.7)
    assert call_cost(call("mystery-model", 10)) is None


def test_settings_add_and_override_prices(settings):
    settings.DJANGO_ERGO = {"MODEL_PRICES": {"house-model": {"input": 1, "output": 2}}}
    from django_ergo.settings import api_settings

    api_settings.reload() if hasattr(api_settings, "reload") else None
    price = price_for("house-model")
    assert (price.input, price.output, price.cache_read) == (1, 2, 1)


def test_openai_usage_is_split_into_billed_parts():
    from django_ergo.conversation.engines.openai_api import usage_parts

    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=300,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=600, cache_write_tokens=100
        ),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=120),
    )
    assert usage_parts(usage) == {
        "input_tokens": 300,  # the uncached rest of the prompt
        "output_tokens": 300,  # reasoning included
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 600,
        "reasoning_tokens": 120,
    }
    # Responses API names, and no details at all.
    responses = SimpleNamespace(
        input_tokens=50,
        output_tokens=5,
        input_tokens_details=SimpleNamespace(cached_tokens=20, cache_write_tokens=None),
    )
    assert usage_parts(responses)["input_tokens"] == 30
    assert (
        usage_parts(SimpleNamespace(prompt_tokens=7, completion_tokens=3))[
            "input_tokens"
        ]
        == 7
    )
    assert usage_parts(None)["input_tokens"] is None


def test_requests_are_priced_one_by_one_with_the_long_context_surcharge():
    from django_ergo.pricing import add_request_cost
    from django_ergo.pricing import call_cost
    from django_ergo.pricing import call_cost_parts

    price = price_for("gpt-6-sol")
    small = price.parts(input_tokens=100_000, output_tokens=10_000)
    assert small == pytest.approx(
        {"input": 0.2, "cache_write": 0, "cache_read": 0, "output": 0.1}
    )
    # Over 272K input (cache reads count): 2x input and cache, 1.5x output, for the whole request.
    big = price.parts(
        input_tokens=100_000, cache_read_input_tokens=200_000, output_tokens=10_000
    )
    assert big == pytest.approx(
        {"input": 0.4, "cache_write": 0, "cache_read": 0.08, "output": 0.15}
    )

    c = call("gpt-6-sol", 0, 0)
    c.cost_usd, c.metadata = None, {}
    for usage in (
        SimpleNamespace(input_tokens=100_000, output_tokens=10_000),
        SimpleNamespace(
            input_tokens=100_000, cache_read_input_tokens=200_000, output_tokens=10_000
        ),
    ):
        add_request_cost(c, "gpt-6-sol", usage)
    assert call_cost(c) == pytest.approx(0.3 + 0.63)
    assert call_cost_parts(c) == pytest.approx(
        {"input": 0.6, "cache_write": 0, "cache_read": 0.08, "output": 0.25}
    )

    # An unpriced request makes the call unpriced; summed totals never get the surcharge.
    add_request_cost(c, "mystery-model", SimpleNamespace(input_tokens=1))
    assert c.cost_usd is None
    legacy = call("gpt-6-sol", 500_000, 0)
    legacy.cost_usd, legacy.metadata = None, {}
    assert call_cost(legacy) == pytest.approx(1.0)  # 500K at $2/M, no 2x
