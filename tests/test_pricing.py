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
