from __future__ import annotations

import pytest

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Provider
from django_ergo.bots.providers import Providers
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.routing import AgentChoice
from django_ergo.bots.routing import pick_agent
from django_ergo.bots.routing import pick_model
from django_ergo.bots.runtime import Bot
from tests.test_bot_workers import workers  # noqa: F401 — fixture


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(Provider, "available", property(lambda self: True))
    return {
        "default": "auto/medium",
        "providers": {
            "anthropic": {
                "type": "claude",
                "transport": "cli",
                "models": [
                    "claude-sonnet-5-5",
                    "claude-opus-5-5",
                    "claude-fable-5-1",
                    "claude-haiku-4-5",
                ],
            },
            "openai-codex": {
                "type": "openai",
                "transport": "cli",
                "models": [
                    "gpt-6.1-sol",
                    "gpt-6-astra",
                    "gpt-6-luna",
                    "gpt-5.6-terra",
                ],
            },
        },
    }


def test_defaults_use_configured_subscription_catalog(catalog):
    providers = Providers.from_dict(catalog)
    medium = [
        "anthropic/claude-opus-5-5",
        "openai-codex/gpt-6.1-sol",
        "anthropic/claude-sonnet-5-5",
        "openai-codex/gpt-5.6-terra",
    ]
    assert providers.routing.tiers == {
        "low": [
            "anthropic/claude-sonnet-5-5",
            "openai-codex/gpt-6-luna",
            "anthropic/claude-haiku-4-5",
        ],
        "medium": medium,
        "high": [
            "anthropic/claude-fable-5-1",
            "openai-codex/gpt-6-astra",
            *medium,
        ],
    }
    for tier, refs in providers.routing.tiers.items():
        assert providers.knows(f"auto/{tier}")
        choices = providers.routing.agents[tier]
        assert [f"{c.provider}/{c.model}" for c in choices] == refs
        assert all(c.effort == tier for c in choices)
        assert all(
            c.agent == ("claude" if c.provider == "anthropic" else "codex")
            for c in choices
        )


@pytest.mark.django_db
def test_builtin_high_routes_above_medium(catalog):
    providers = Providers.from_dict(catalog)
    assert pick_model(providers, "low") == "anthropic/claude-sonnet-5-5"
    assert pick_model(providers, "medium") == "anthropic/claude-opus-5-5"
    assert pick_model(providers, "high") == "anthropic/claude-fable-5-1"
    assert pick_agent(providers, "high") == AgentChoice(
        "claude", "claude-fable-5-1", "anthropic", "high"
    )


def test_defaults_do_not_add_api_providers_or_unlisted_models(catalog):
    catalog.pop("default")
    catalog["providers"]["anthropic"]["transport"] = "api"
    catalog["providers"]["openai-codex"]["models"] = ["gpt-5.6-terra", "unknown"]
    providers = Providers.from_dict(catalog)
    assert providers.routing.tiers == {
        "low": [],
        "medium": ["openai-codex/gpt-5.6-terra"],
        "high": ["openai-codex/gpt-5.6-terra"],
    }
    assert not providers.knows("auto/low")
    assert providers.routing.agents["low"] == []
    assert providers.routing.agents["high"] == [
        AgentChoice("codex", "gpt-5.6-terra", "openai-codex", "high")
    ]
    assert set(providers.providers) == {"anthropic", "openai-codex"}
    assert providers.find("openai-codex/gpt-6-astra") is None


def test_api_only_catalog_has_no_default_candidates(catalog):
    catalog.pop("default")
    for provider in catalog["providers"].values():
        provider["transport"] = "api"
    providers = Providers.from_dict(catalog)
    for tier in ("low", "medium", "high"):
        assert providers.routing.tiers[tier] == []
        assert providers.routing.agents[tier] == []
        assert not providers.knows(f"auto/{tier}")


def test_same_model_uses_provider_declaration_order(catalog):
    catalog["providers"]["second-claude"] = catalog["providers"]["anthropic"]
    providers = Providers.from_dict(catalog)
    assert providers.routing.tiers["medium"][:3] == [
        "anthropic/claude-opus-5-5",
        "second-claude/claude-opus-5-5",
        "openai-codex/gpt-6.1-sol",
    ]


@pytest.mark.parametrize("tier", ["low", "medium", "high"])
def test_each_builtin_override_replaces_only_its_named_list(catalog, tier):
    original = Providers.from_dict(catalog)
    catalog["tiers"] = {tier: ["openai-codex/gpt-6-luna"]}
    catalog["agents"] = {
        tier: [
            {
                "agent": "codex",
                "model": "gpt-6.1-sol",
                "provider": "openai-codex",
                "effort": "low",
            }
        ]
    }
    providers = Providers.from_dict(catalog)
    assert providers.routing.tiers[tier] == ["openai-codex/gpt-6-luna"]
    assert providers.routing.agents[tier] == [
        AgentChoice("codex", "gpt-6.1-sol", "openai-codex", "low")
    ]
    for other in {"low", "medium", "high"} - {tier}:
        assert providers.routing.tiers[other] == original.routing.tiers[other]
        assert providers.routing.agents[other] == original.routing.agents[other]


def test_tier_and_agent_overrides_are_independent_and_preserve_explicit_api(catalog):
    original = Providers.from_dict(catalog)
    catalog["providers"]["api"] = {"type": "openai", "models": ["gpt-6-sol"]}
    catalog["tiers"] = {"high": ["api/gpt-6-sol"]}
    catalog["agents"] = {"low": []}
    providers = Providers.from_dict(catalog)
    assert providers.routing.tiers["high"] == ["api/gpt-6-sol"]
    assert providers.routing.agents["high"] == original.routing.agents["high"]
    assert providers.routing.tiers["low"] == original.routing.tiers["low"]
    assert providers.routing.agents["low"] == []


@pytest.mark.parametrize("section", ["tiers", "agents"])
@pytest.mark.parametrize("name", [None, 42, "", "   ", "research/deep"])
def test_invalid_tier_names_are_rejected(catalog, section, name):
    catalog[section] = {name: []}
    with pytest.raises(ProvidersError, match="non-empty single path segment"):
        Providers.from_dict(catalog)


@pytest.mark.django_db
def test_custom_chat_and_agent_tiers_route_and_resolve(catalog):
    catalog["default"] = "auto/research.v2"
    catalog["tiers"] = {"research.v2": ["openai-codex/gpt-6-astra"]}
    catalog["agents"] = {
        "research.v2": [
            {
                "agent": "codex",
                "model": "gpt-6-astra",
                "effort": "high",
                "provider": "openai-codex",
            }
        ]
    }
    providers = Providers.from_dict(catalog)
    assert pick_model(providers, "research.v2") == "openai-codex/gpt-6-astra"
    assert pick_agent(providers, "research.v2") == AgentChoice(
        "codex", "gpt-6-astra", "openai-codex", "high"
    )
    bot = Bot(
        BotDefinition.from_dict(
            {"name": "custom", "engine": {"model": "auto/research.v2"}}
        )
    )
    registry = BotRegistry()
    registry.providers = providers
    registry.add(bot)
    assert bot.session_model_ref("auto/research.v2") == "auto/research.v2"
    assert bot.resolve_ref("auto/research.v2", None) == "openai-codex/gpt-6-astra"
    spec = bot.engine_spec()
    assert spec.transport_type == "cli"
    assert spec.config["model"] == "gpt-6-astra"


@pytest.mark.django_db(transaction=True)
def test_custom_tier_can_start_agent_through_toolkit(
    catalog,
    tmp_path,
    workers,  # noqa: F811 - imported pytest fixture
):
    from tests.test_bot_agents import setup
    from tests.test_bot_agents import start

    bot, _, manager, toolkit = setup(tmp_path)
    catalog["agents"] = {
        "deep-research": [
            {
                "agent": "codex",
                "model": "gpt-6-astra",
                "effort": "high",
                "provider": "openai-codex",
            }
        ]
    }
    registry = BotRegistry()
    registry.providers = Providers.from_dict(catalog)
    registry.add(bot)
    start(
        toolkit,
        {
            "brief": "Research the design",
            "workspace": "site",
            "tier": "deep-research",
        },
    )
    assert len(manager.started) == 1
    spec = manager.started[0]
    assert (spec.agent, spec.model, spec.effort, spec.tier) == (
        "codex",
        "gpt-6-astra",
        "high",
        "deep-research",
    )
