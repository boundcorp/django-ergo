"""Model providers: which engines and models a deployment's chats may use.

A ``providers.yaml`` at the top of a bot path lists them::

    default: openai/gpt-6-luna         # chats with no model picked, bots with no engine
    providers:
      openai:
        type: openai                   # engine type: openai or claude
        api_key_env: OPENAI_API_KEY    # read at runtime, never stored
        config: {reasoning_effort: medium}   # engine config shared by its models
        models: [gpt-6-luna, gpt-6.1-sol]
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
        models: [gpt-6.1-sol, gpt-6-astra, gpt-6-luna, gpt-5.6-terra]

A model is named ``provider/model``. A bot can use one with
``engine: {model: openai/gpt-6.1-sol}``, and a chat can switch to any enabled
model whose provider's key is set (the chat model picker). ``tiers``,
``agents`` and ``routing`` (plus an optional ``routing.md``) let a chat or
bot ask for ``auto/<tier>`` instead, picked per turn from what's left on
each subscription (``bots.routing``). Small, medium, large and xlarge have
defaults from the listed subscription models (``DEFAULT_TIER_MODELS``); YAML
can replace them or add tiers. ``low`` and ``high`` are aliases of small and
large.

``transport: cli`` runs models on the subscription a CLI on this machine is
logged in with: Claude models through the Claude Code CLI
(``conversation.engines.claude_code``), OpenAI models through the Codex CLI
on a ChatGPT login (``conversation.engines.codex_cli``). It needs no key,
and is available when the CLI is installed.

``private_pools`` lists API-billed models that only named bots may use (see
``conversation.inference_policy`` and docs/bots.md, "Private inference")::

    private_pools:
      personal:
        enabled: false                 # nothing can use it until true
        provider: fireworks            # fixed endpoint, key from FIREWORKS_API_KEY
        allowed_bots: [personal]       # the only bots that may select it
        default: flash                 # what ``personal`` alone means
        daily_usd_cap: 5               # optional: refuse sends past this (UTC day)
        models:                        # alias -> exact model ID
          flash: accounts/fireworks/models/deepseek-v4p1-flash-1017
          heavy: accounts/fireworks/models/kimi-k3

A bot selects a member as ``personal`` or ``personal/<alias>``. Private
models never appear in ``models()``, ``find()``, tiers, agents or the default,
and no public provider, tier, agent or default may name one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from types import MappingProxyType

import yaml

from django_ergo.bots.routing import DEFAULT_EFFORT
from django_ergo.bots.routing import AgentChoice
from django_ergo.bots.routing import Routing
from django_ergo.bots.routing import RoutingRules
from django_ergo.bots.routing import is_auto
from django_ergo.bots.routing import tier_of
from django_ergo.conversation.inference_policy import PRIVATE_PROVIDERS
from django_ergo.conversation.inference_policy import RESERVED_POOL_NAMES
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.inference_policy import PrivatePool
from django_ergo.conversation.inference_policy import is_private_host
from django_ergo.conversation.inference_policy import is_protected_env
from django_ergo.conversation.inference_policy import names_private_model
from django_ergo.conversation.inference_policy import url_host

PROVIDERS_FILE = "providers.yaml"
ROUTING_FILE = "routing.md"
ENGINE_TYPES = ("openai", "claude")
TRANSPORTS = {"openai": ("api", "cli"), "claude": ("api", "cli")}
PRIVATE_POOLS_KEY = "private_pools"
POOL_KEYS = frozenset(
    {"enabled", "provider", "allowed_bots", "default", "models", "daily_usd_cap"}
)

# Preference order is by model family, then providers.yaml's provider order.
# Only exact catalog models on configured CLI subscriptions become candidates.
DEFAULT_TIER_MODELS = {
    "small": (
        ("claude", "claude-haiku-5-5"),
        ("openai", "gpt-6-luna"),
        ("claude", "claude-haiku-4-5"),
    ),
    "medium": (
        ("claude", "claude-sonnet-5-5"),
        ("openai", "gpt-6.1-sol"),
        ("claude", "claude-haiku-5-5"),
        ("openai", "gpt-6-luna"),
    ),
    "large": (
        ("claude", "claude-opus-5-5"),
        ("openai", "gpt-6-sol"),
        ("claude", "claude-sonnet-5-5"),
        ("openai", "gpt-6.1-sol"),
    ),
    "xlarge": (
        ("claude", "claude-fable-5-1"),
        ("openai", "gpt-6-astra"),
        ("claude", "claude-opus-5-5"),
        ("openai", "gpt-6-sol"),
    ),
}


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
    # Private pools by name: never in models(), find(), tiers or the default.
    private_pools: dict[str, PrivatePool] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.providers)

    def models(self) -> list[Model]:
        """Every public model (private pool members are never listed here)."""
        return [m for p in self.providers.values() for m in p.models.values()]

    def pool_names(self) -> frozenset[str]:
        """Selector namespaces that are private, configured or reserved."""
        return frozenset((*RESERVED_POOL_NAMES, *self.private_pools))

    def is_private(self, ref) -> bool:
        """``ref`` names a private pool (``personal``, ``personal/heavy``) or a
        private model, so only an allowlisted bot may ever use it."""
        ref = str(ref or "").strip()
        if not ref:
            return False
        if ref.partition("/")[0] in self.pool_names() or names_private_model(ref):
            return True
        return any(
            model_id in ref
            for pool in self.private_pools.values()
            for model_id in pool.model_ids
        )

    def check_public(self, ref, where: str = "This") -> str:
        """``ref``, or PolicyError when it's private."""
        if self.is_private(ref):
            msg = f"{where} can't use the private model {ref!r}"
            raise PolicyError(msg)
        return ref

    def private(self, ref: str) -> tuple[PrivatePool, str, str]:
        """(pool, alias, exact model ID) for ``personal`` or ``personal/<alias>``.
        PolicyError when the pool isn't configured or has no such member. This
        only resolves; it doesn't authorize (see ``PrivatePool.check_bot``)."""
        head, _, alias = str(ref or "").partition("/")
        pool = self.private_pools.get(head)
        if pool is None:
            msg = (
                f"Private pool {head!r} isn't configured in {PROVIDERS_FILE} ({PRIVATE_POOLS_KEY})"
                if head in self.pool_names()
                else f"{ref!r} isn't a private pool selector"
            )
            raise PolicyError(msg)
        alias, model_id = pool.resolve(alias)
        return pool, alias, model_id

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
                    config={
                        **dict(entry.get("config") or {}),
                        **(
                            {"context_window": int(entry["context_window"])}
                            if entry.get("context_window") is not None
                            else {}
                        ),
                    },
                )
                provider.models[model.name] = model
            providers[provider.name] = provider
        for key in data:
            if str(key).startswith("private") and key != PRIVATE_POOLS_KEY:
                msg = (
                    f"Unknown key {key!r}; private models go under {PRIVATE_POOLS_KEY}"
                )
                raise ProvidersError(msg)
        result = cls(providers=providers, default=str(data.get("default") or ""))
        result.private_pools = _private_pools(data.get(PRIVATE_POOLS_KEY), providers)
        result.routing = result._routing(data)
        if result.default and not result.knows(result.default):
            msg = f"default {result.default!r} isn't one of the listed models"
            raise ProvidersError(msg)
        result._check_public()
        return result

    def _check_public(self) -> None:
        """No public provider, model, default, tier or agent names a private one."""
        for provider in self.providers.values():
            self._check_public_provider(provider)
        if self.is_private(self.default):
            msg = f"default {self.default!r} is private; the default must be a public model"
            raise ProvidersError(msg)
        for tier, refs in self.routing.tiers.items():
            for ref in refs:
                if self.is_private(ref):
                    msg = f"tiers.{tier}: {ref!r} is private; tiers are public only"
                    raise ProvidersError(msg)
        for tier, choices in self.routing.agents.items():
            for choice in choices:
                if self.is_private(choice.model) or self.is_private(choice.provider):
                    msg = f"agents.{tier}: {choice.model!r} is private; agents are public only"
                    raise ProvidersError(msg)

    def _check_public_provider(self, provider: Provider) -> None:
        where = f"Provider {provider.name!r}"
        if provider.name in self.pool_names() or names_private_model(provider.name):
            msg = f"{where}: the name is reserved for {PRIVATE_POOLS_KEY}"
            raise ProvidersError(msg)
        if is_protected_env(provider.api_key_env):
            msg = f"{where}: {provider.api_key_env} is reserved for {PRIVATE_POOLS_KEY}"
            raise ProvidersError(msg)
        for model in provider.models.values():
            if self.is_private(model.name):
                msg = f"{where}: {model.name!r} is a private model; list it under {PRIVATE_POOLS_KEY}"
                raise ProvidersError(msg)
        configs = (provider.config, *(m.config for m in provider.models.values()))
        for config in configs:
            for key in ("base_url", "api_base", "endpoint"):
                if is_private_host(url_host(config.get(key))):
                    msg = f"{where}: {key} points at a private inference endpoint"
                    raise ProvidersError(msg)

    def _default_routing(self) -> Routing:
        """Defaults never add a provider, model or API-billed candidate."""
        routing = Routing()
        for tier, catalog in DEFAULT_TIER_MODELS.items():
            routing.tiers[tier] = []
            routing.agents[tier] = []
            for kind, name in catalog:
                for provider in self.providers.values():
                    if (
                        provider.transport != "cli"
                        or provider.type != kind
                        or name not in provider.models
                    ):
                        continue
                    routing.tiers[tier].append(provider.models[name].id)
                    routing.agents[tier].append(
                        AgentChoice(
                            agent="claude" if kind == "claude" else "codex",
                            model=name,
                            provider=provider.name,
                            effort=DEFAULT_EFFORT,
                        )
                    )
        return routing

    def _routing(self, data: dict) -> Routing:
        defaults = self._default_routing()
        tiers = defaults.tiers
        for name, refs in (data.get("tiers") or {}).items():
            self._check_tier(name)
            tier = tier_of(name)
            tiers[tier] = [str(r) for r in refs or []]
            for ref in tiers[tier]:
                if self.find(ref) is None:
                    msg = f"tiers.{tier}: {ref!r} isn't one of the listed models"
                    raise ProvidersError(msg)
        agents = defaults.agents
        for name, listed in (data.get("agents") or {}).items():
            self._check_tier(name)
            tier = tier_of(name)
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
        if not isinstance(tier, str) or not tier.strip() or "/" in tier:
            msg = f"Tier {tier!r} must be a non-empty single path segment"
            raise ProvidersError(msg)

    def knows(self, ref: str) -> bool:
        """A listed model, or ``auto/<tier>`` for a tier that's set up."""
        if is_auto(ref):
            return bool(self.routing.tiers.get(tier_of(ref)))
        return self.find(ref) is not None


def _private_pools(raw, providers: dict[str, Provider]) -> dict[str, PrivatePool]:
    """Parse ``private_pools`` strictly: unknown keys, bad types, duplicate or
    clashing names and IDs are errors, never ignored."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = f"{PRIVATE_POOLS_KEY} must be a mapping of pool name to pool"
        raise ProvidersError(msg)
    pools: dict[str, PrivatePool] = {}
    seen: dict[str, str] = {}  # model ID -> pool/alias
    for name, spec in raw.items():
        pools[name] = _private_pool(name, spec, providers, seen)
    return pools


def _private_pool(
    name, spec, providers: dict[str, Provider], seen: dict[str, str]
) -> PrivatePool:
    where = f"{PRIVATE_POOLS_KEY}.{name}"
    if not isinstance(name, str) or not name.strip() or "/" in name:
        msg = f"{where}: the pool name must be a non-empty single path segment"
        raise ProvidersError(msg)
    if name in providers or name == "auto":
        msg = f"{where}: the name clashes with a provider or auto"
        raise ProvidersError(msg)
    if not isinstance(spec, dict):
        msg = f"{where} must be a mapping"
        raise ProvidersError(msg)
    if unknown := sorted(set(spec) - POOL_KEYS):
        msg = f"{where}: unknown keys {', '.join(map(str, unknown))} (allowed: {', '.join(sorted(POOL_KEYS))})"
        raise ProvidersError(msg)
    enabled = spec.get("enabled", False)
    if not isinstance(enabled, bool):
        msg = f"{where}.enabled must be true or false"
        raise ProvidersError(msg)
    provider = spec.get("provider")
    if provider not in PRIVATE_PROVIDERS:
        msg = f"{where}.provider must be one of {', '.join(PRIVATE_PROVIDERS)}"
        raise ProvidersError(msg)
    allowed = spec.get("allowed_bots") or []
    if not isinstance(allowed, list) or not all(
        isinstance(b, str) and b.strip() for b in allowed
    ):
        msg = f"{where}.allowed_bots must be a list of bot names"
        raise ProvidersError(msg)
    models = _pool_models(where, spec.get("models"), seen)
    default = spec.get("default") or ""
    if default and default not in models:
        msg = f"{where}.default {default!r} isn't one of its models"
        raise ProvidersError(msg)
    return PrivatePool(
        name=name,
        provider=provider,
        enabled=enabled,
        allowed_bots=frozenset(allowed),
        models=MappingProxyType(models),
        default=str(default),
        daily_usd_cap=_cap(spec.get("daily_usd_cap"), where),
    )


def _pool_models(where: str, models, seen: dict[str, str]) -> dict[str, str]:
    if not isinstance(models, dict) or not models:
        msg = f"{where}.models must map aliases to exact model IDs"
        raise ProvidersError(msg)
    for alias, model_id in models.items():
        if not isinstance(alias, str) or not alias.strip() or "/" in alias:
            msg = f"{where}.models: alias {alias!r} must be a single path segment"
            raise ProvidersError(msg)
        if (
            not isinstance(model_id, str)
            or not model_id
            or any(c.isspace() for c in model_id)
        ):
            msg = f"{where}.models.{alias}: the model ID must be a non-empty string"
            raise ProvidersError(msg)
        if model_id in seen:
            msg = f"{where}.models.{alias}: {model_id!r} is already {seen[model_id]}"
            raise ProvidersError(msg)
        seen[model_id] = f"{where.rpartition('.')[2]}/{alias}"
    return dict(models)


def _cap(value, where: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        msg = f"{where}.daily_usd_cap must be a number of US dollars"
        raise ProvidersError(msg)
    try:
        cap = Decimal(str(value))
    except InvalidOperation as exc:
        msg = f"{where}.daily_usd_cap must be a number of US dollars"
        raise ProvidersError(msg) from exc
    if not cap.is_finite() or cap <= 0:
        msg = f"{where}.daily_usd_cap must be more than 0"
        raise ProvidersError(msg)
    return cap


def _check_duplicate_keys(node, path: str) -> None:
    """Duplicate mapping keys under ``private_pools`` are errors (YAML would
    silently keep the last one)."""
    if isinstance(node, yaml.MappingNode):
        keys = set()
        for key_node, value_node in node.value:
            key = getattr(key_node, "value", None)
            if key in keys:
                msg = f"{path}: duplicate key {key!r}"
                raise ProvidersError(msg)
            keys.add(key)
            _check_duplicate_keys(value_node, f"{path}.{key}")
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _check_duplicate_keys(item, path)


def parse_providers(text: str) -> Providers:
    """A providers.yaml's text as Providers (strict about ``private_pools``)."""
    root = yaml.compose(text)
    if isinstance(root, yaml.MappingNode):
        for key_node, value_node in root.value:
            if getattr(key_node, "value", None) == PRIVATE_POOLS_KEY:
                _check_duplicate_keys(value_node, PRIVATE_POOLS_KEY)
    return Providers.from_dict(yaml.safe_load(text) or {})


def load_providers(paths: list[Path]) -> Providers:
    """The first ``providers.yaml`` found at the top of the given paths."""
    for path in paths:
        file = Path(path) / PROVIDERS_FILE
        if file.is_file():
            providers = parse_providers(file.read_text())
            text = Path(path) / ROUTING_FILE
            if text.is_file():
                providers.routing.text = text.read_text().strip()
            return providers
    return Providers()
