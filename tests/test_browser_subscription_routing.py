from types import SimpleNamespace

import pytest

from django_ergo.bots import routing
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Provider
from django_ergo.bots.providers import parse_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.conversation.inference_policy import PolicyError


@pytest.fixture
def browser(monkeypatch):
    providers = parse_providers("""
providers:
  openai:
    type: openai
    models: [gpt-6.1-terra]
  chatgpt:
    type: openai
    transport: cli
    models: [gpt-6.1-terra]
  claude:
    type: claude
    transport: cli
    models: [sonnet]
tiers:
  small: [openai/gpt-6.1-terra, chatgpt/gpt-6.1-terra, claude/sonnet]
private_pools:
  personal:
    enabled: true
    provider: fireworks
    allowed_bots: [personal]
    default: flash
    models:
      flash: accounts/fireworks/models/flash
      heavy: accounts/fireworks/models/heavy
""")
    monkeypatch.setattr(Provider, "available", property(lambda self: True))
    monkeypatch.setattr(routing, "current_usage", dict)
    monkeypatch.setattr(routing, "active_rules", lambda _: routing.RoutingRules())

    def make(name="devbox", **config):
        bot = Bot(
            BotDefinition.from_dict(
                {"name": name, "plugins": [{"name": "browser", **config}]}
            )
        )
        registry = BotRegistry()
        registry.providers = providers
        registry.add(bot)
        return bot.plugin("browser")

    return make


def test_public_browser_uses_subscription_and_resolves_terra_aliases(browser):
    plugin = browser()
    for selector in (
        "",
        "auto/small",
        "gpt-6.1-terra",
        "openai/gpt-6.1-terra",
        "chatgpt/gpt-6.1-terra",
    ):
        ref = plugin.task_model(None, selector)
        assert ref == "chatgpt/gpt-6.1-terra"
        spec = plugin.bot.engine_spec(None, ref)
        assert spec.transport_type == "cli"
        assert spec.config["model"] == "gpt-6.1-terra"
    assert plugin.bot.engine_spec(None, "openai/gpt-6.1-terra").transport_type == "api"


def test_browser_routes_around_exhaustion_and_never_falls_back_to_api(
    browser, monkeypatch
):
    plugin = browser()
    monkeypatch.setattr(routing, "current_usage", lambda: {"chatgpt": {"weekly": 100}})
    assert plugin.task_model(None, "auto/small") == "claude/sonnet"
    with pytest.raises(ValueError, match="No safe subscription"):
        plugin.task_model(None, "openai/gpt-6.1-terra")
    monkeypatch.setattr(
        routing,
        "current_usage",
        lambda: {"chatgpt": {"weekly": 100}, "claude": {"weekly": 100}},
    )
    with pytest.raises(ValueError, match="No safe subscription"):
        plugin.task_model(None, "auto/small")
    for selector in ("unknown", "openai/unknown", "auto/unknown"):
        with pytest.raises(ValueError, match="Unknown|No safe subscription"):
            plugin.task_model(None, selector)


def test_personal_browser_uses_pinned_chat_model(browser):
    plugin = browser("personal", driver_model="personal/flash")
    session = SimpleNamespace(model="personal/heavy")
    assert plugin.task_model(session) == "personal/heavy"
    assert plugin.task_model(session, "personal/heavy") == "personal/heavy"
    with pytest.raises(PolicyError, match="chat's private model"):
        plugin.task_model(session, "personal/flash")
    with pytest.raises(PolicyError):
        plugin.task_model(session, "openai/gpt-6.1-terra")
