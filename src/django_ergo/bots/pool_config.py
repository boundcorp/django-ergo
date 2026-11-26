"""The ``pools:``, ``auto_rollover`` and ``privacy:`` parts of providers.yaml.

Pools are named, ordered groups of routes (a provider's models, or one
exact ``provider/model``) that chats, browser tasks and coding agents select
from. They are not billing classes: a pool may mix subscription (CLI) and
metered (API) routes. Billing, price, transport and account are route
metadata on the provider (``bots.providers``)::

    auto_rollover: false              # global gate; default off
    pools:
      - name: subscriptions
        label: Subscriptions
        routes: [claude, chatgpt, xai-oauth]   # provider names or provider/model
      - name: paid-api
        label: Paid API
        owner: lee                    # username; required with a metered route
        routes: [openai, anthropic/claude-opus-5-5]
        budget_usd: 50                # the owner's own spend grant: its ceiling
        expires: 2026-12-31           # required with budget_usd: grants end
        per_launch_usd: 5             # reserved per metered coding-agent launch
        rollover_out: false           # default: true only for subscription-only pools
        allow_auto_entry: false       # default: false when any route is metered
        tiers: {medium: [openai/gpt-6.1-sol]}   # optional, else the global tiers in the pool
        agents:                                  # optional coding candidates
          medium: [{agent: codex, model: gpt-6.1-sol, provider: openai}]

Pools are user-level groupings. A pool without ``owner`` is a deployment
pool anyone may use, and can only hold subscription routes. A pool with an
``owner`` is visible to that user and to users the owner has granted spend
on it; its metered routes send only under a :class:`SpendGrant
<django_ergo.conversation.models.SpendGrant>` (an immutable, time-bounded
ceiling for one grantee and an exact set of routes). ``budget_usd`` and
``expires`` here are the owner's own grant; the owner issues grants to
others at runtime (``bots.pools.issue_grant``). A preference never mints
authority.

Without a ``pools:`` section the deployment keeps today's behavior: one
synthesized pool, ``subscription``, holding every provider, with rollover
off and no budget enforcement (``Pool.legacy``).

``privacy:`` declares data policies that intersect with pool eligibility and
are inherited by everything a restricted origin starts (``bots.pools``).
Every ``private_pools`` entry is also a privacy policy of the same name::

    privacy:
      personal:
        bots: [personal]              # default: the private pool's allowed_bots
        allow: [personal]             # private pool names, providers or provider/model
        web_search: [brave]           # non-LLM search backends it may use
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING

from django_ergo.bots.routing import AgentChoice
from django_ergo.bots.routing import tier_of

if TYPE_CHECKING:
    from django_ergo.bots.providers import Provider

POOLS_KEY = "pools"
AUTO_ROLLOVER_KEY = "auto_rollover"
PRIVACY_KEY = "privacy"
LEGACY_POOL = "subscription"
SUBSCRIPTION = "subscription"
METERED = "metered"
BILLING_KINDS = (SUBSCRIPTION, METERED)
POOL_KEYS = frozenset(
    {
        "name",
        "label",
        "owner",
        "routes",
        "enabled",
        "rollover_out",
        "allow_auto_entry",
        "budget_usd",
        "expires",
        "per_launch_usd",
        "max_output_tokens",
        "tiers",
        "agents",
    }
)
PRIVACY_KEYS = frozenset({"bots", "allow", "web_search", "label"})
NON_LLM_SEARCH = ("brave",)
DEFAULT_MAX_OUTPUT_TOKENS = 16_000
MAX_POOL_NAME = 40


class PoolConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pool:
    """One configured (or synthesized) pool. Runtime state (order, toggles,
    budget changes) lives in the database and is merged by ``bots.pools``."""

    name: str
    label: str = ""
    owner: str = ""  # username; "" = a deployment pool (subscription routes only)
    routes: tuple[str, ...] = ()  # provider, provider/*, or provider/model
    enabled: bool = True
    rollover_out: bool = True
    allow_auto_entry: bool = True
    budget_usd: Decimal | None = None
    expires_at: dt.datetime | None = None
    per_launch_usd: Decimal | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    tiers: dict[str, list[str]] = field(default_factory=dict)
    agents: dict[str, list[AgentChoice]] = field(default_factory=dict)
    metered: bool = False  # any route bills per token
    legacy: bool = False  # synthesized from a providers.yaml without pools

    def contains(self, provider: str, model: str = "") -> bool:
        """Whether ``provider`` (and ``model``, when given) is one of its routes."""
        for selector in self.routes:
            head, _, tail = selector.partition("/")
            if head != provider:
                continue
            if tail in ("", "*") or not model or tail == model:
                return True
        return False

    def contains_ref(self, ref: str) -> bool:
        provider, _, model = str(ref or "").partition("/")
        return bool(provider) and bool(model) and self.contains(provider, model)

    @property
    def title(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class PrivacyPolicy:
    """Where a restricted origin's data may go. Inherited, never widened."""

    name: str
    bots: frozenset[str] = frozenset()
    allow: frozenset[str] = frozenset()  # private pool names, providers, provider/model
    web_search: tuple[str, ...] = ()
    label: str = ""

    def permits(self, ref: str, private_pools: frozenset[str] = frozenset()) -> bool:
        """Whether a model selector may be used under this policy."""
        ref = str(ref or "").strip()
        head, _, tail = ref.partition("/")
        if not head:
            return False
        if head in private_pools:
            return head in self.allow
        return any(
            selector in (head, f"{head}/*", f"{head}/{tail}") for selector in self.allow
        )

    def permits_search(self, backend: str) -> bool:
        return backend in self.web_search and backend in NON_LLM_SEARCH


@dataclass
class PoolsConfig:
    pools: list[Pool] = field(default_factory=list)
    auto_rollover: bool = False
    privacy: dict[str, PrivacyPolicy] = field(default_factory=dict)

    @property
    def legacy(self) -> bool:
        """No ``pools:`` section (or no providers.yaml at all): today's behavior."""
        return not self.pools or (len(self.pools) == 1 and self.pools[0].legacy)

    def get(self, name: str) -> Pool | None:
        return next((p for p in self.pools if p.name == name), None)


def _bool(value, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        msg = f"{where} must be true or false"
        raise PoolConfigError(msg)
    return value


def _usd(value, where: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        msg = f"{where} must be a number of US dollars"
        raise PoolConfigError(msg)
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        msg = f"{where} must be a number of US dollars"
        raise PoolConfigError(msg) from exc
    if not amount.is_finite() or amount < 0:
        msg = f"{where} must be 0 or more"
        raise PoolConfigError(msg)
    return amount


def _expiry(value, where: str) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        # A date means through the end of that day (UTC).
        moment = dt.datetime.combine(value, dt.time.max)
    else:
        try:
            moment = dt.datetime.fromisoformat(str(value))
        except ValueError as exc:
            msg = f"{where} must be a date or an ISO date-time"
            raise PoolConfigError(msg) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment


def billing_of(provider: Provider) -> str:
    return provider.billing


def parse_pools(data: dict, providers: dict[str, Provider]) -> PoolsConfig:
    """``pools``, ``auto_rollover`` and ``privacy`` from providers.yaml data."""
    auto_rollover = _bool(data.get(AUTO_ROLLOVER_KEY), AUTO_ROLLOVER_KEY, False)  # noqa: FBT003
    raw = data.get(POOLS_KEY)
    if raw is None:
        pools = [legacy_pool(providers)]
    else:
        if not isinstance(raw, list) or not raw:
            msg = f"{POOLS_KEY} must be a non-empty list of pools, in preference order"
            raise PoolConfigError(msg)
        pools = []
        for index, spec in enumerate(raw):
            pool = _pool(spec, index, providers)
            if any(p.name == pool.name for p in pools):
                msg = f"{POOLS_KEY}: duplicate pool name {pool.name!r}"
                raise PoolConfigError(msg)
            pools.append(pool)
    return PoolsConfig(pools=pools, auto_rollover=auto_rollover)


def legacy_pool(providers: dict[str, Provider]) -> Pool:
    """Today's behavior as one pool: every provider, global tiers, no rollover."""
    return Pool(
        name=LEGACY_POOL,
        label="Subscription",
        routes=tuple(sorted(providers)),
        rollover_out=False,
        allow_auto_entry=False,
        metered=any(p.billing == METERED for p in providers.values()),
        legacy=True,
    )


def _pool(spec, index: int, providers: dict[str, Provider]) -> Pool:  # noqa: PLR0915, C901, PLR0912
    where = f"{POOLS_KEY}[{index}]"
    if not isinstance(spec, dict):
        msg = f"{where} must be a mapping"
        raise PoolConfigError(msg)
    if unknown := sorted(set(spec) - POOL_KEYS):
        msg = f"{where}: unknown keys {', '.join(map(str, unknown))} (allowed: {', '.join(sorted(POOL_KEYS))})"
        raise PoolConfigError(msg)
    name = spec.get("name")
    if (
        not isinstance(name, str)
        or not name.strip()
        or "/" in name
        or len(name) > MAX_POOL_NAME
        or name == "auto"
    ):
        msg = f"{where}.name must be a single path segment of up to {MAX_POOL_NAME} characters (not auto)"
        raise PoolConfigError(msg)
    where = f"{POOLS_KEY}.{name}"
    routes = spec.get("routes")
    if not isinstance(routes, list) or not routes:
        msg = f"{where}.routes must list providers or provider/model routes"
        raise PoolConfigError(msg)
    selectors = []
    for route in routes:
        if not isinstance(route, str) or not route.strip():
            msg = f"{where}.routes: {route!r} isn't a provider or provider/model"
            raise PoolConfigError(msg)
        head, _, tail = route.partition("/")
        provider = providers.get(head)
        if provider is None:
            msg = f"{where}.routes: {head!r} isn't a provider in providers.yaml"
            raise PoolConfigError(msg)
        if (
            tail not in ("", "*")
            and not provider.worker_only
            and tail not in provider.models
        ):
            msg = f"{where}.routes: {route!r} isn't one of {head}'s models"
            raise PoolConfigError(msg)
        selectors.append(route)
    members = [providers[s.partition("/")[0]] for s in selectors]
    metered = any(p.billing == METERED for p in members)
    budget = _usd(spec.get("budget_usd"), f"{where}.budget_usd")
    per_launch = _usd(spec.get("per_launch_usd"), f"{where}.per_launch_usd")
    max_output = spec.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if (
        isinstance(max_output, bool)
        or not isinstance(max_output, int)
        or max_output <= 0
    ):
        msg = f"{where}.max_output_tokens must be a positive integer"
        raise PoolConfigError(msg)
    owner = spec.get("owner") or ""
    if not isinstance(owner, str):
        msg = f"{where}.owner must be a username"
        raise PoolConfigError(msg)
    if metered and not owner.strip():
        msg = (
            f"{where} has metered routes, so it needs an owner (the user whose "
            "spend authority it is)"
        )
        raise PoolConfigError(msg)
    expires = _expiry(spec.get("expires"), f"{where}.expires")
    if budget is not None and expires is None:
        msg = f"{where}.budget_usd needs expires: spend authority is time-bounded"
        raise PoolConfigError(msg)
    if per_launch is not None and budget is not None and per_launch > budget:
        msg = f"{where}.per_launch_usd can't be more than budget_usd"
        raise PoolConfigError(msg)
    pool = Pool(
        name=name,
        label=str(spec.get("label") or ""),
        owner=owner.strip(),
        routes=tuple(selectors),
        enabled=_bool(spec.get("enabled"), f"{where}.enabled", True),  # noqa: FBT003
        rollover_out=_bool(
            spec.get("rollover_out"), f"{where}.rollover_out", not metered
        ),
        allow_auto_entry=_bool(
            spec.get("allow_auto_entry"), f"{where}.allow_auto_entry", not metered
        ),
        budget_usd=budget,
        expires_at=expires,
        per_launch_usd=per_launch,
        max_output_tokens=max_output,
        metered=metered,
    )
    tiers = spec.get("tiers") or {}
    if not isinstance(tiers, dict):
        msg = f"{where}.tiers must map tier names to provider/model lists"
        raise PoolConfigError(msg)
    for tier, refs in tiers.items():
        if not isinstance(refs, list):
            msg = f"{where}.tiers.{tier} must be a list"
            raise PoolConfigError(msg)
        for ref in refs:
            provider_name, _, model = str(ref).partition("/")
            provider = providers.get(provider_name)
            if provider is None or model not in provider.models:
                msg = f"{where}.tiers.{tier}: {ref!r} isn't a listed model"
                raise PoolConfigError(msg)
            if not pool.contains(provider_name, model):
                msg = f"{where}.tiers.{tier}: {ref!r} isn't one of the pool's routes"
                raise PoolConfigError(msg)
        pool.tiers[tier_of(tier)] = [str(r) for r in refs]
    agents = spec.get("agents") or {}
    if not isinstance(agents, dict):
        msg = f"{where}.agents must map tier names to agent choices"
        raise PoolConfigError(msg)
    for tier, listed in agents.items():
        try:
            choices = [AgentChoice.from_dict(c) for c in listed or []]
        except (KeyError, TypeError) as exc:
            msg = f"{where}.agents.{tier}: each entry needs agent and provider ({exc})"
            raise PoolConfigError(msg) from exc
        for choice in choices:
            provider = providers.get(choice.provider)
            if provider is None or not pool.contains(
                choice.provider,
                choice.model.rpartition("/")[2]
                if provider.worker_only
                else choice.model,
            ):
                msg = f"{where}.agents.{tier}: {choice.provider}/{choice.model} isn't one of the pool's routes"
                raise PoolConfigError(msg)
            if choice.agent not in {"claude", "codex", "omp"}:
                msg = f"{where}.agents.{tier}: agent {choice.agent!r} isn't claude, codex or omp"
                raise PoolConfigError(msg)
        pool.agents[tier_of(tier)] = choices
    return pool


def parse_privacy(raw, private_pools: dict) -> dict[str, PrivacyPolicy]:  # noqa: C901
    """Explicit ``privacy:`` policies, plus one per private pool."""
    policies: dict[str, PrivacyPolicy] = {}
    if raw is not None and not isinstance(raw, dict):
        msg = f"{PRIVACY_KEY} must map policy names to policies"
        raise PoolConfigError(msg)
    for name, spec in (raw or {}).items():
        where = f"{PRIVACY_KEY}.{name}"
        if not isinstance(name, str) or not name.strip() or "/" in name:
            msg = f"{where}: the name must be a single path segment"
            raise PoolConfigError(msg)
        if not isinstance(spec, dict):
            msg = f"{where} must be a mapping"
            raise PoolConfigError(msg)
        if unknown := sorted(set(spec) - PRIVACY_KEYS):
            msg = f"{where}: unknown keys {', '.join(map(str, unknown))} (allowed: {', '.join(sorted(PRIVACY_KEYS))})"
            raise PoolConfigError(msg)
        pool = private_pools.get(name)
        bots = spec.get("bots")
        if bots is None:
            bots = sorted(pool.allowed_bots) if pool else []
        allow = spec.get("allow")
        if allow is None:
            allow = [name] if pool else []
        search = spec.get("web_search") or []
        for key, value in (("bots", bots), ("allow", allow), ("web_search", search)):
            if not isinstance(value, list) or not all(
                isinstance(v, str) and v.strip() for v in value
            ):
                msg = f"{where}.{key} must be a list of names"
                raise PoolConfigError(msg)
        if bad := sorted(set(search) - set(NON_LLM_SEARCH)):
            msg = f"{where}.web_search: {', '.join(bad)} isn't a non-LLM search backend ({', '.join(NON_LLM_SEARCH)})"
            raise PoolConfigError(msg)
        policies[name] = PrivacyPolicy(
            name=name,
            bots=frozenset(bots),
            allow=frozenset(allow),
            web_search=tuple(search),
            label=str(spec.get("label") or ""),
        )
    for name, pool in private_pools.items():
        policies.setdefault(
            name,
            PrivacyPolicy(
                name=name, bots=frozenset(pool.allowed_bots), allow=frozenset({name})
            ),
        )
    return policies
