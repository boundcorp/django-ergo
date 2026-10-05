"""Model providers: which engines and models a deployment's chats may use.

A ``providers.yaml`` at the top of a bot path lists them::

    default: openai/gpt-6-luna         # chats with no model picked, bots with no engine
    providers:
      openai:
        type: openai                   # engine type: openai or claude
        api_key_env: OPENAI_API_KEY    # read at runtime, never stored
        config: {reasoning_effort: medium}   # engine config shared by its models
        models: [gpt-6-luna, gpt-6-sol]
      anthropic:
        type: claude
        api_key_env: ANTHROPIC_API_KEY
        models:
          - claude-sonnet-5-5
          - {name: claude-opus-5-5, label: Opus 5.5, config: {max_tokens: 16000}}
      subscription:
        type: claude
        transport: cli                 # the Claude Code CLI logged in on this machine
        models: [claude-sonnet-5-5, claude-opus-5-5]
      chatgpt:
        type: openai
        transport: cli                 # the Codex CLI logged in with ChatGPT
        config: {effort: medium}
        models: [gpt-6-sol, gpt-6-luna]

A model is named ``provider/model``. A bot can use one with
``engine: {model: openai/gpt-6-sol}``, and a chat can switch to any enabled
model whose provider's key is set (the chat model picker). ``tiers``,
``agents`` and ``routing`` (plus an optional ``routing.md``) let a chat or
bot ask for ``auto/low``, ``auto/medium`` or ``auto/high`` instead, picked
per turn from what's left on each subscription (``bots.routing``).

``transport: cli`` runs models on the subscription a CLI on this machine is
logged in with: Claude models through the Claude Code CLI
(``conversation.engines.claude_code``), OpenAI models through the Codex CLI
on a ChatGPT login (``conversation.engines.codex_cli``). It needs no key,
and is available when the CLI is installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import yaml

from django_ergo.bots.routing import TIERS
from django_ergo.bots.routing import AgentChoice
from django_ergo.bots.routing import Routing
from django_ergo.bots.routing import RoutingRules
from django_ergo.bots.routing import is_auto
from django_ergo.bots.routing import tier_of

PROVIDERS_FILE = "providers.yaml"
ROUTING_FILE = "routing.md"
ENGINE_TYPES = ("openai", "claude")
TRANSPORTS = {"openai": ("api", "cli"), "claude": ("api", "cli")}


class ProvidersError(ValueError):
    pass


@dataclass
class Model:
    provider: str
    name: str
    label: str = ""
    config: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.name}"


@dataclass
class Provider:
    name: str
    type: str
    api_key_env: str = ""
    transport: str = "api"
    config: dict = field(default_factory=dict)
    models: dict[str, Model] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """Its key is set (or it needs none); for the CLI, the CLI is installed."""
        if self.transport == "cli" and self.type == "openai":
            from django_ergo.conversation.engines.codex_cli import codex_installed

            return codex_installed(self.config.get("command", ""))
        if self.transport == "cli":
            from django_ergo.conversation.engines.claude_code import claude_installed

            return claude_installed(self.config.get("command", ""))
        return not self.api_key_env or bool(os.environ.get(self.api_key_env))


@dataclass
class Providers:
    providers: dict[str, Provider] = field(default_factory=dict)
    default: str = ""
    routing: Routing = field(default_factory=Routing)

    def __bool__(self) -> bool:
        return bool(self.providers)

    def models(self) -> list[Model]:
        return [m for p in self.providers.values() for m in p.models.values()]

    def find(self, ref: str) -> tuple[Provider, Model] | None:
        """The provider and model for ``provider/model``, or None."""
        provider_name, _, model_name = ref.partition("/")
        provider = self.providers.get(provider_name)
        model = provider.models.get(model_name) if provider else None
        return (provider, model) if model else None

    def engine(self, ref: str) -> tuple[str, dict, str]:
        """Engine type, engine config and API key env var for a model."""
        found = self.find(ref)
        if found is None:
            msg = f"Unknown model {ref!r}; providers.yaml lists {', '.join(m.id for m in self.models()) or 'none'}"
            raise ProvidersError(msg)
        provider, model = found
        config = {**provider.config, **model.config, "model": model.name}
        return provider.type, config, provider.api_key_env

    @classmethod
    def from_dict(cls, data: dict) -> Providers:
        if not isinstance(data, dict):
            msg = f"{PROVIDERS_FILE} must be a mapping"
            raise ProvidersError(msg)
        providers = {}
        for name, given in (data.get("providers") or {}).items():
            spec = given or {}
            kind = spec.get("type") or name
            if kind not in ENGINE_TYPES:
                msg = (
                    f"Provider {name!r}: type must be one of {', '.join(ENGINE_TYPES)}"
                )
                raise ProvidersError(msg)
            transport = str(spec.get("transport") or "api")
            if transport not in TRANSPORTS[kind]:
                msg = f"Provider {name!r}: transport {transport!r} doesn't work with {kind}"
                raise ProvidersError(msg)
            provider = Provider(
                name=str(name),
                type=kind,
                transport=transport,
                api_key_env=str(spec.get("api_key_env") or ""),
                config=dict(spec.get("config") or {}),
            )
            for listed in spec.get("models") or []:
                entry = listed if isinstance(listed, dict) else {"name": listed}
                model = Model(
                    provider=provider.name,
                    name=str(entry["name"]),
                    label=str(entry.get("label") or ""),
                    config=dict(entry.get("config") or {}),
                )
                provider.models[model.name] = model
            providers[provider.name] = provider
        result = cls(providers=providers, default=str(data.get("default") or ""))
        result.routing = result._routing(data)
        if result.default and not result.knows(result.default):
            msg = f"default {result.default!r} isn't one of the listed models"
            raise ProvidersError(msg)
        return result

    def _routing(self, data: dict) -> Routing:
        tiers = {}
        for tier, refs in (data.get("tiers") or {}).items():
            self._check_tier(tier)
            tiers[tier] = [str(r) for r in refs or []]
            for ref in tiers[tier]:
                if self.find(ref) is None:
                    msg = f"tiers.{tier}: {ref!r} isn't one of the listed models"
                    raise ProvidersError(msg)
        agents = {}
        for tier, listed in (data.get("agents") or {}).items():
            self._check_tier(tier)
            try:
                agents[tier] = [AgentChoice.from_dict(c) for c in listed or []]
            except (KeyError, TypeError) as exc:
                msg = f"agents.{tier}: each entry needs agent and provider ({exc})"
                raise ProvidersError(msg) from exc
            for choice in agents[tier]:
                provider = self.providers.get(choice.provider)
                if provider is None or provider.transport != "cli":
                    msg = (
                        f"agents.{tier}: provider {choice.provider!r} must be a "
                        "listed subscription (transport: cli)"
                    )
                    raise ProvidersError(msg)
        try:
            rules = RoutingRules.model_validate(data.get("routing") or {})
        except ValueError as exc:
            msg = f"routing: {exc}"
            raise ProvidersError(msg) from exc
        return Routing(tiers=tiers, agents=agents, rules=rules)

    @staticmethod
    def _check_tier(tier) -> None:
        if tier not in TIERS:
            msg = f"Tier {tier!r} must be one of {', '.join(TIERS)}"
            raise ProvidersError(msg)

    def knows(self, ref: str) -> bool:
        """A listed model, or ``auto/<tier>`` for a tier that's set up."""
        if is_auto(ref):
            return tier_of(ref) in self.routing.tiers
        return self.find(ref) is not None


def load_providers(paths: list[Path]) -> Providers:
    """The first ``providers.yaml`` found at the top of the given paths."""
    for path in paths:
        file = Path(path) / PROVIDERS_FILE
        if file.is_file():
            providers = Providers.from_dict(yaml.safe_load(file.read_text()) or {})
            text = Path(path) / ROUTING_FILE
            if text.is_file():
                providers.routing.text = text.read_text().strip()
            return providers
    return Providers()
