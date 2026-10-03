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


def test_openai_usage_is_split_the_way_it_is_billed():
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
        "cache_read_input_tokens": 600,
        "cache_creation_input_tokens": 100,  # gpt-6 bills cache writes at 1.25x input
        "output_tokens": 300,  # reasoning included
        "reasoning_tokens": 120,
    }
    responses_api = SimpleNamespace(
        input_tokens=50,
        output_tokens=5,
        input_tokens_details={"cached_tokens": 20, "cache_write_tokens": None},
    )
    assert usage_parts(responses_api)["input_tokens"] == 30
    assert usage_parts(None)["input_tokens"] is None


def test_requests_are_priced_one_by_one_with_the_long_context_surcharge():
    from django_ergo.pricing import add_request_cost
    from django_ergo.pricing import call_cost_parts

    price = price_for("gpt-6-sol")
    assert price.parts(input_tokens=100_000, output_tokens=10_000) == pytest.approx(
        {"input": 0.2, "cache_write": 0, "cache_read": 0, "output": 0.1}
    )
    # Over 272K input in one request (cache reads count): 2x input and cache, 1.5x output.
    assert price.parts(
        input_tokens=100_000, cache_read_input_tokens=200_000, output_tokens=10_000
    ) == pytest.approx(
        {"input": 0.4, "cache_write": 0, "cache_read": 0.08, "output": 0.15}
    )

    c = SimpleNamespace(**vars(call("gpt-6-sol")), cost_usd=None, metadata={})
    add_request_cost(
        c, "gpt-6-sol", SimpleNamespace(input_tokens=100_000, output_tokens=10_000)
    )
    add_request_cost(
        c,
        "gpt-6-sol",
        SimpleNamespace(
            input_tokens=100_000, cache_read_input_tokens=200_000, output_tokens=10_000
        ),
    )
    assert call_cost(c) == pytest.approx(0.3 + 0.63)
    assert call_cost_parts(c) == pytest.approx(
        {"input": 0.6, "cache_write": 0, "cache_read": 0.08, "output": 0.25}
    )
    # A stored message that wasn't a model request (a pre-seeded tool call: no model, no
    # tokens) costs nothing and doesn't make the call unpriced.
    add_request_cost(c, "", SimpleNamespace(input_tokens=0, output_tokens=None))
    assert call_cost(c) == pytest.approx(0.3 + 0.63)
    # A request on an unpriced model drops the running cost; later requests don't restart it.
    add_request_cost(c, "mystery-model", SimpleNamespace(input_tokens=1))
    add_request_cost(c, "gpt-6-sol", SimpleNamespace(input_tokens=1_000))
    assert c.cost_usd is None
    # Summed totals (older calls) never get the surcharge.
    assert call_cost(call("gpt-6-sol", 500_000)) == pytest.approx(1.0)
