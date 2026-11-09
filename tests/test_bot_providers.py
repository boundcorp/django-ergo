from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

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
        "provider": "openai",
        "api_key": "sk-test",
    }

    # A chat's pick wins, on any engine.
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    bot = bot_with(found)
    chat = SimpleNamespace(engine_type="openai", model="openai/gpt-6-sol")
    assert bot.engine_spec(chat).config["model"] == "gpt-6-sol"
    chat.model = "anthropic/claude-opus-5-5"
    spec = bot.engine_spec(chat)
    assert (spec.engine_type, spec.config["model"]) == ("claude", "claude-opus-5-5")
    chat.model = "openai/gone"  # no longer listed: ignored
    assert bot.engine_spec(chat).config["model"] == "gpt-6-luna"

    # The old engine form still works without providers.
    plain = bot_with(Providers(), {"type": "openai", "config": {"model": "gpt-6-luna"}})
    assert plain.engine_spec().config == {"model": "gpt-6-luna"}

    monkeypatch.delenv("TEST_OPENAI_KEY")
    with pytest.raises(RuntimeError, match="TEST_OPENAI_KEY"):
        bot.engine_spec()


def test_chats_follow_the_bot_to_another_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    found = Providers.from_dict(
        {**yaml.safe_load(PROVIDERS), "default": "anthropic/claude-opus-5-5"}
    )
    bot = bot_with(found)
    assert bot.engine_spec().engine_type == "claude"  # new chats

    # Messages are engine-neutral: a chat last run on OpenAI follows the default.
    old = SimpleNamespace(engine_type="openai", model="")
    spec = bot.engine_spec(old)
    assert (spec.engine_type, spec.config["model"]) == ("claude", "claude-opus-5-5")
    picked = SimpleNamespace(engine_type="claude", model="openai/gpt-6-sol")
    assert bot.engine_spec(picked).config["model"] == "gpt-6-sol"


def test_model_context_window_suffix_and_override(tmp_path):
    providers = Providers.from_dict(
        {
            "providers": {
                "subscription": {
                    "type": "claude",
                    "transport": "cli",
                    "models": [
                        "claude-opus-5-5[1m]",
                        {"name": "small", "context_window": 64000},
                    ],
                }
            }
        }
    )
    first = bot_with(
        providers, {"config": {"model": "subscription/claude-opus-5-5[1m]"}}
    )
    engine = first.make_engine()
    assert engine.context_window == 1000000
    assert engine.model == "claude-opus-5-5[1m]"
    engine = first.make_engine(model="subscription/small")
    assert engine.context_window == 64000
    assert engine.model == "small"


@pytest.mark.django_db
def test_explicit_reroute_is_used_once_before_pace_routing_resumes(
    django_user_model, monkeypatch
):
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import RoutingSwitch

    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    config = yaml.safe_load(PROVIDERS)
    config["default"] = "auto/medium"
    config["tiers"] = {"medium": ["openai/gpt-6-sol", "anthropic/claude-opus-5-5"]}
    bot = bot_with(Providers.from_dict(config))
    monkeypatch.setattr("django_ergo.bots.runtime.ensure_compiled", lambda *args: None)
    monkeypatch.setattr(
        "django_ergo.bots.runtime.route_model",
        lambda providers, tier, current: "openai/gpt-6-sol",
    )
    user = django_user_model.objects.create(username="retry")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="auto/medium", engine_type="openai"
    )
    assert bot.route(chat) == "openai/gpt-6-sol"
    bot.reroute(chat, "anthropic/claude-opus-5-5", "retry after refusal")

    # The resume endpoint queues a new turn, which loads the session again.
    chat.refresh_from_db()
    assert bot.route(chat) == "anthropic/claude-opus-5-5"
    assert bot.engine_spec(chat).config["model"] == "claude-opus-5-5"
    chat.refresh_from_db()
    assert "retry_route" not in chat.metadata
    assert bot.route(chat) == "openai/gpt-6-sol"
    assert list(
        RoutingSwitch.objects.order_by("created_at").values_list("to_model", flat=True)
    ) == ["anthropic/claude-opus-5-5", "openai/gpt-6-sol"]


@pytest.mark.django_db
def test_retry_route_is_discarded_when_model_changes_or_becomes_unavailable(
    django_user_model, monkeypatch
):
    from django_ergo.conversation.models import ConversationSession

    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-ant")
    config = yaml.safe_load(PROVIDERS)
    config["default"] = "auto/medium"
    config["tiers"] = {"medium": ["openai/gpt-6-sol", "anthropic/claude-opus-5-5"]}
    bot = bot_with(Providers.from_dict(config))
    monkeypatch.setattr("django_ergo.bots.runtime.ensure_compiled", lambda *args: None)
    monkeypatch.setattr(
        "django_ergo.bots.runtime.route_model",
        lambda providers, tier, current: "openai/gpt-6-sol",
    )
    user = django_user_model.objects.create(username="retry-change")
    chat = ConversationSession.objects.create(
        user=user, bot_name="kitchen", model="auto/medium", engine_type="openai"
    )
    bot.route(chat)
    bot.reroute(chat, "anthropic/claude-opus-5-5", "retry")
    bot.pick_model(chat, "openai/gpt-6-sol")
    bot.pick_model(chat, "auto/medium")
    chat.refresh_from_db()
    assert bot.route(chat) == "openai/gpt-6-sol"

    bot.reroute(chat, "anthropic/claude-opus-5-5", "retry again")
    monkeypatch.delenv("TEST_ANTHROPIC_KEY")
    chat.refresh_from_db()
    assert bot.route(chat) == "openai/gpt-6-sol"
    chat.refresh_from_db()
    assert "retry_route" not in chat.metadata
