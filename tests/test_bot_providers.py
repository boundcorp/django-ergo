from __future__ import annotations

from types import SimpleNamespace

import pytest

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Providers
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import load_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot

PROVIDERS = """
default: openai/gpt-6-luna
providers:
  openai:
    type: openai
    api_key_env: TEST_OPENAI_KEY
    config: {reasoning_effort: medium}
    models: [gpt-6-luna, gpt-6-sol]
  anthropic:
    type: claude
    api_key_env: TEST_ANTHROPIC_KEY
    models:
      - {name: claude-opus-5-5, label: Opus 5.5, config: {max_tokens: 16000}}
"""


def providers(tmp_path) -> Providers:
    (tmp_path / "providers.yaml").write_text(PROVIDERS)
    return load_providers([tmp_path / "missing", tmp_path])


def bot_with(found: Providers, engine: dict | None = None) -> Bot:
    bot = Bot(BotDefinition.from_dict({"name": "kitchen", "engine": engine or {}}))
    registry = BotRegistry()
    registry.providers = found
    registry.add(bot)
    return bot


def test_providers_yaml_lists_models_and_builds_their_engines(tmp_path, monkeypatch):
    found = providers(tmp_path)
    assert [m.id for m in found.models()] == [
        "openai/gpt-6-luna",
        "openai/gpt-6-sol",
        "anthropic/claude-opus-5-5",
    ]
    assert found.engine("anthropic/claude-opus-5-5") == (
        "claude",
        {"max_tokens": 16000, "model": "claude-opus-5-5"},
        "TEST_ANTHROPIC_KEY",
    )
    monkeypatch.delenv("TEST_ANTHROPIC_KEY", raising=False)
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    assert found.providers["openai"].available
    assert not found.providers["anthropic"].available
    with pytest.raises(ProvidersError, match="Unknown model"):
        found.engine("openai/gpt-2")
    with pytest.raises(ProvidersError, match="isn't one of the listed models"):
        Providers.from_dict({"default": "x/y"})
    with pytest.raises(ProvidersError, match="type must be one of"):
        Providers.from_dict({"providers": {"gemini": {"models": ["g"]}}})
    assert not load_providers([tmp_path / "nowhere"])


def test_bots_and_chats_pick_models_from_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    found = providers(tmp_path)

    # A bot with no engine uses the default; one can name a provider/model.
    spec = bot_with(found).engine_spec()
    assert (spec.engine_type, spec.config["model"], spec.config["api_key"]) == (
        "openai",
        "gpt-6-luna",
        "sk-test",
    )
    sol = bot_with(found, {"config": {"model": "openai/gpt-6-sol", "max_tokens": 900}})
    spec = sol.engine_spec()
    assert spec.config == {
        "reasoning_effort": "medium",
        "model": "gpt-6-sol",
        "max_tokens": 900,
        "api_key": "sk-test",
    }

    # A chat's pick wins, when it runs on the chat's engine.
    bot = bot_with(found)
    chat = SimpleNamespace(engine_type="openai", metadata={"model": "openai/gpt-6-sol"})
    assert bot.engine_spec(chat).config["model"] == "gpt-6-sol"
    chat.metadata["model"] = "anthropic/claude-opus-5-5"  # another engine: ignored
    assert bot.engine_spec(chat).config["model"] == "gpt-6-luna"
    chat.metadata["model"] = "openai/gone"  # no longer listed: ignored
    assert bot.engine_spec(chat).config["model"] == "gpt-6-luna"

    # The old engine form still works without providers.
    plain = bot_with(Providers(), {"type": "openai", "config": {"model": "gpt-6-luna"}})
    assert plain.engine_spec().config == {"model": "gpt-6-luna"}

    monkeypatch.delenv("TEST_OPENAI_KEY")
    with pytest.raises(RuntimeError, match="TEST_OPENAI_KEY"):
        bot.engine_spec()
