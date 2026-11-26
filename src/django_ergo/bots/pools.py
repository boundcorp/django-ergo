"""Model pools: the one policy every model consumer resolves through.

A deployment's providers.yaml lists ordered pools of routes
(``bots.pool_config``). Every consumer (bot chats and threads, titles,
browser tasks, coding agents, background work) asks this module for a route:

1. :func:`privacy_for` — the inherited privacy policy of the origin (the
   bot, its folder ancestors, the session's recorded origin and the turn
   being handled). It intersects with everything below and is never widened
   by a pool switch.
2. :func:`snapshot` — pool config merged with runtime state (Ergonaut's
   Routing page): preferred order, manual pool (per chat or user-wide),
   toggles, budgets and spend, the auto-rollover gate, provider switches.
3. :func:`select_route` / :func:`select_agent` — the active pool (the manual
   pick, else the first enabled pool in order), then the capability tier or
   pin *within* it, ranked by subscription pace (``routing.choose``). The
   result is a :class:`Decision` (recorded as a ``RouteDecision`` receipt) or
   a :class:`PoolRefusal` with a code and the next choices.

Rollover: only exhaustion (no compatible, authorized route with capacity)
moves to the next pool, and only when the global ``auto_rollover``, the
current pool's ``rollover_out`` and the next pool's ``allow_auto_entry`` all
permit. Disabled providers, missing keys, privacy, budget and expiry denials
are refusals, never rollover.

Metered routes need an explicit pool budget; each request reserves its
worst-case cost first (``conversation.spend_guard``). A configured API key is
never authority by itself.

Without a ``pools:`` section (``PoolsConfig.legacy``) everything behaves as it
did before pools: one synthesized pool holding every provider, global tiers,
no budget enforcement, no rollover.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from django.utils import timezone

from django_ergo.bots import routing
from django_ergo.bots.pool_config import METERED
from django_ergo.bots.pool_config import SUBSCRIPTION
from django_ergo.bots.pool_config import Pool
from django_ergo.bots.pool_config import PrivacyPolicy
from django_ergo.bots.pool_config import legacy_pool
from django_ergo.bots.routing import AgentChoice
from django_ergo.bots.routing import is_auto
from django_ergo.bots.routing import tier_of
from django_ergo.conversation.inference_policy import PolicyError

if TYPE_CHECKING:
    import datetime as dt

    from django_ergo.bots.providers import Provider
    from django_ergo.bots.providers import Providers
    from django_ergo.bots.runtime import Bot
    from django_ergo.conversation.spend_guard import MeteredGuard

logger = logging.getLogger(__name__)

# Consumers: what the route is for.
CHAT = "chat"
TITLE = "title"
COMPACTION = "compaction"
BROWSER = "browser"
CODING = "coding"
BACKGROUND = "background"
CONSUMERS = (CHAT, TITLE, COMPACTION, BROWSER, CODING, BACKGROUND)

# Refusal codes. Only EXHAUSTED may roll over.
EXHAUSTED = "pool_exhausted"
PIN_OUT_OF_POOL = "pin_out_of_pool"
NO_ROUTE = "no_route"
UNAVAILABLE = "unavailable"
PROVIDER_DISABLED = "provider_disabled"
POOL_DISABLED = "pool_disabled"
BUDGET_REQUIRED = "budget_required"
BUDGET_EXHAUSTED = "budget_exhausted"
BUDGET_EXPIRED = "budget_expired"
PRICE_UNKNOWN = "price_unknown"
PRIVACY_DENIED = "privacy_denied"
NO_CODING_ROUTE = "no_coding_route"
UNCONFINABLE = "unconfinable"
UNGUARDED = "unguarded"  # metered route on an engine that can't enforce spend

SESSION_POOL_KEY = "pool"  # session.metadata: this chat's manual pool
SESSION_ROUTE_KEY = "pool_route"  # session.metadata: the last decision (display)
SESSION_REFUSAL_KEY = "pool_refusal"  # session.metadata: the last refusal (UI)
ORIGIN_KEY = "privacy_origin"  # session/message/job metadata: inherited policies
DEPLOYMENT_KEY = "deployment"
# Agent CLI <-> provider engine type for metered coding confinement.
METERED_AGENT_FOR = {"claude": "claude", "openai": "codex"}


class PoolRefusal(PolicyError, ValueError):  # noqa: N818
    """A typed, user-facing refusal to select a route. Never a fallback."""

    def __init__(  # noqa: PLR0913
        self,
        code: str,
        message: str,
        *,
        pool: str = "",
        next_choices: list[dict] | None = None,
        rejected: list[dict] | None = None,
        consumer: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.pool = pool
        self.next_choices = next_choices or []
        self.rejected = rejected or []
        self.consumer = consumer

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "pool": self.pool,
            "next_choices": self.next_choices,
            "consumer": self.consumer,
        }


# -- privacy --------------------------------------------------------------------

_origin: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "ergo_privacy_origin", default=()
)


@contextlib.contextmanager
def origin_scope(names, *, replace: bool = False):
    """Run with ``names`` (privacy policy names) added to the current origin
    (or, with ``replace``, as the whole origin: a turn, worker step or task
    that knows its own origin): every route selected inside is restricted by them."""
    merged = tuple(dict.fromkeys((*(() if replace else _origin.get()), *(names or ()))))
    token = _origin.set(merged)
    try:
        yield merged
    finally:
        _origin.reset(token)


def current_origin() -> tuple[str, ...]:
    return _origin.get()


def lineage(bot: Bot) -> list[str]:
    """The bot's name and its folder ancestors' names, nearest first."""
    names = [bot.name]
    registry = getattr(bot, "registry", None)
    seen = {bot.name}
    parent = getattr(bot, "parent_name", "")
    while parent and parent not in seen:
        names.append(parent)
        seen.add(parent)
        found = registry.bots.get(parent) if registry is not None else None
        parent = getattr(found, "parent_name", "") if found else ""
    return names


@dataclass(frozen=True)
class Privacy:
    """The effective (intersected) privacy policy of an origin."""

    policies: tuple[PrivacyPolicy, ...] = ()
    private_pools: frozenset[str] = frozenset()

    @property
    def restricted(self) -> bool:
        return bool(self.policies)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.policies)

    def permits(self, ref: str) -> bool:
        return all(p.permits(ref, self.private_pools) for p in self.policies)

    def permits_bot(self, names) -> bool:
        """Whether a bot (by lineage) is inside every policy's ``bots``."""
        names = set(names)
        return all(names & p.bots for p in self.policies)

    def permits_search(self, backend: str) -> bool:
        return all(p.permits_search(backend) for p in self.policies)

    def label(self) -> str:
        return ", ".join(p.label or p.name for p in self.policies)


def bot_policy_names(bot: Bot) -> list[str]:
    """Privacy policies covering ``bot`` or one of its folder ancestors."""
    policies = bot.providers.pools.privacy
    names = lineage(bot)
    return [n for n, p in sorted(policies.items()) if p.bots & set(names)]


def session_origin(session) -> list[str]:
    """Policies recorded on a session (inherited from its parent or sender)."""
    meta = getattr(session, "metadata", None) or {}
    value = meta.get(ORIGIN_KEY) or []
    return [str(v) for v in value] if isinstance(value, list) else []


def privacy_for(bot: Bot, session=None, origin=()) -> Privacy:
    """The intersection of every policy this work inherits: the bot's (and its
    ancestors'), the session's recorded origin, the turn being handled, and
    ``origin``. Unknown policy names fail closed (allow nothing)."""
    providers = bot.providers
    configured = providers.pools.privacy
    names = dict.fromkeys(
        (
            *bot_policy_names(bot),
            *session_origin(session),
            *current_origin(),
            *(origin or ()),
        )
    )
    policies = tuple(configured.get(name) or PrivacyPolicy(name=name) for name in names)
    return Privacy(policies=policies, private_pools=frozenset(providers.private_pools))


def check_delegation(bot_names, privacy: Privacy, *, what: str = "this") -> None:
    """PolicyError unless a recipient (by lineage) is inside the origin's policy."""
    if privacy.restricted and not privacy.permits_bot(bot_names):
        msg = (
            f"Privacy policy {privacy.label()} keeps this work's data with its "
            f"approved bots; {what} ({next(iter(bot_names), '?')}) isn't one of them"
        )
        raise PolicyError(msg)


# -- runtime state ----------------------------------------------------------------


def preference_key(user_id) -> str:
    return f"user:{user_id}" if user_id else DEPLOYMENT_KEY


def disabled_providers() -> set[str]:
    """Providers switched off at runtime (Ergonaut). Runs the ORM."""
    from django.db import DatabaseError

    from django_ergo.conversation.models import ProviderToggle

    try:
        return set(
            ProviderToggle.objects.filter(enabled=False).values_list("name", flat=True)
        )
    except DatabaseError:  # pragma: no cover - table missing before migrate
        return set()


def provider_enabled(provider: Provider, disabled: set[str] | None = None) -> bool:
    disabled = disabled_providers() if disabled is None else disabled
    return provider.enabled and provider.name not in disabled


@dataclass
class PoolView:
    """A pool with its runtime state merged in."""

    pool: Pool
    position: int
    enabled: bool
    rollover_out: bool
    allow_auto_entry: bool
    budget_usd: Decimal | None
    expires_at: dt.datetime | None
    per_launch_usd: Decimal | None
    spent_usd: Decimal = Decimal(0)
    reserved_usd: Decimal = Decimal(0)
    version: int = 0

    @property
    def name(self) -> str:
        return self.pool.name

    @property
    def title(self) -> str:
        return self.pool.title

    def problem(self, now=None) -> tuple[str, str] | None:
        """Why the pool can't be used at all (not exhaustion), or None."""
        if not self.enabled:
            return POOL_DISABLED, f"Pool {self.title!r} is switched off"
        if self.pool.legacy or not self.pool.metered:
            return None
        now = now or timezone.now()
        if self.expires_at is not None and now >= self.expires_at:
            return (
                BUDGET_EXPIRED,
                f"Pool {self.title!r}'s spend authority expired {self.expires_at:%Y-%m-%d %H:%M} UTC",
            )
        if self.budget_usd is None:
            return (
                BUDGET_REQUIRED,
                f"Pool {self.title!r} has metered routes and no budget; set budget_usd to use it",
            )
        if self.spent_usd + self.reserved_usd >= self.budget_usd:
            return (
                BUDGET_EXHAUSTED,
                f"Pool {self.title!r} has used its ${self.budget_usd} budget "
                f"(${self.spent_usd:.2f} spent, ${self.reserved_usd:.2f} reserved)",
            )
        return None


@dataclass
class Snapshot:
    providers: Providers
    pools: list[PoolView]
    auto_rollover: bool
    active_pool: str = ""  # manual pick ("" = automatic)
    active_scope: str = ""  # chat, user or ""
    disabled: set[str] = field(default_factory=set)
    user_version: int = 0

    @property
    def legacy(self) -> bool:
        return self.providers.pools.legacy

    def get(self, name: str) -> PoolView | None:
        return next((p for p in self.pools if p.name == name), None)


def _views(providers: Providers) -> list[PoolView]:
    from django.db import DatabaseError

    from django_ergo.conversation.models import ModelPoolState
    from django_ergo.conversation.spend_guard import spent_and_reserved

    try:
        states = {s.name: s for s in ModelPoolState.objects.all()}
    except DatabaseError:  # pragma: no cover
        states = {}
    views = []
    for index, pool in enumerate(providers.pools.pools):
        state = states.get(pool.name)
        view = PoolView(
            pool=pool,
            position=index,
            enabled=pool.enabled,
            rollover_out=pool.rollover_out,
            allow_auto_entry=pool.allow_auto_entry,
            budget_usd=pool.budget_usd,
            expires_at=pool.expires_at,
            per_launch_usd=pool.per_launch_usd,
        )
        if state is not None and not pool.legacy:
            view.version = state.version
            for name in ("enabled", "rollover_out", "allow_auto_entry"):
                if getattr(state, name) is not None:
                    setattr(view, name, getattr(state, name))
            if state.budget_override:
                view.budget_usd = state.budget_usd
            if state.expires_override:
                view.expires_at = state.expires_at
            if state.per_launch_usd is not None:
                view.per_launch_usd = state.per_launch_usd
        if pool.metered and not pool.legacy:
            view.spent_usd, view.reserved_usd = spent_and_reserved(pool.name)
        views.append(view)
    return views


def preference(user_id):
    from django_ergo.conversation.models import ModelPoolPreference

    return ModelPoolPreference.objects.filter(key=preference_key(user_id)).first()


def snapshot(providers: Providers, user_id=None, session=None) -> Snapshot:
    """Config plus runtime state for one user (and chat). Runs the ORM."""
    views = _views(providers)
    pref = None if providers.pools.legacy else preference(user_id)
    if pref is not None and pref.order:
        rank = {name: i for i, name in enumerate(pref.order)}
        views.sort(key=lambda v: (rank.get(v.name, len(rank) + v.position)))
    auto_rollover = providers.pools.auto_rollover
    if pref is not None and pref.auto_rollover is not None:
        auto_rollover = pref.auto_rollover
    active, scope = "", ""
    chat_pool = ((getattr(session, "metadata", None) or {}).get(SESSION_POOL_KEY)) or ""
    if not providers.pools.legacy:
        if chat_pool:
            active, scope = str(chat_pool), "chat"
        elif pref is not None and pref.active_pool:
            active, scope = pref.active_pool, "user"
    return Snapshot(
        providers=providers,
        pools=views,
        auto_rollover=bool(auto_rollover) and not providers.pools.legacy,
        active_pool=active,
        active_scope=scope,
        disabled=disabled_providers(),
        user_version=pref.version if pref is not None else 0,
    )


# -- eligibility -------------------------------------------------------------------


def _engine_registered(provider: Provider) -> bool:
    from django_ergo.conversation.engines import ENGINE_REGISTRY

    return (provider.type, provider.transport) in ENGINE_REGISTRY


def spend_enforced(provider: Provider) -> bool:
    """Whether the engine for ``provider`` implements the spend-guard adapter
    contract (``conversation.spend_guard.SpendAdapter``), so a metered route
    on it can be bounded and priced per attempt."""
    from django.utils.module_loading import import_string

    from django_ergo.conversation.engines import ENGINE_REGISTRY

    path = ENGINE_REGISTRY.get((provider.type, provider.transport))
    if path is None:
        return False
    try:
        engine = import_string(path) if isinstance(path, str) else path
    except ImportError:
        return False
    return getattr(engine, "spend_adapter", None) is not None


def unavailable_reason(provider: Provider) -> str:
    if provider.worker_only:
        return "omp CLI isn't installed (worker-only subscription)"
    if provider.transport == "cli":
        cli = "Codex" if provider.type == "openai" else "Claude Code"
        return f"{cli} CLI isn't installed"
    if provider.api_key_env:
        return f"{provider.api_key_env} isn't set"
    return f"{provider.name} isn't available"


def route_problem(  # noqa: C901, PLR0911, PLR0912
    snap: Snapshot,
    privacy: Privacy,
    consumer: str,
    provider_name: str,
    model: str,
) -> tuple[str, str] | None:
    """Why a route can't serve ``consumer`` now (never pace), or None."""
    providers = snap.providers
    ref = f"{provider_name}/{model}"
    provider = providers.providers.get(provider_name)
    if provider is None:
        return NO_ROUTE, f"{provider_name} isn't in providers.yaml"
    if not provider.enabled or provider_name in snap.disabled:
        return PROVIDER_DISABLED, f"{provider_name} is switched off"
    if providers.is_private(ref):
        return PRIVACY_DENIED, f"{ref} is a private model"
    if privacy.restricted and not privacy.permits(ref):
        return PRIVACY_DENIED, f"privacy policy {privacy.label()} doesn't allow {ref}"
    if consumer != CODING:
        if provider.worker_only:
            return NO_ROUTE, f"{provider_name} runs coding workers only"
        if model not in provider.models:
            return NO_ROUTE, f"{ref} isn't a listed model"
        if not _engine_registered(provider):
            return (
                UNAVAILABLE,
                f"no {provider.type} {provider.transport} engine is installed",
            )
        if consumer == BROWSER and snap.legacy and provider.transport != "cli":
            return NO_ROUTE, "browser tasks run on CLI subscriptions"
    if not provider.available:
        return UNAVAILABLE, unavailable_reason(provider)
    if provider.metered and not snap.legacy and consumer != CODING:
        if not spend_enforced(provider):
            return (
                UNGUARDED,
                f"{ref} bills per token but its {provider.type} {provider.transport} "
                "engine can't enforce a spend bound per request",
            )
        found = provider.models.get(model)
        if found is None or found.price is None:
            return PRICE_UNKNOWN, f"{ref} has no exact price in providers.yaml"
    return None


def tier_candidates(providers: Providers, pool: Pool, tier: str) -> list[str]:
    """``provider/model`` candidates for ``tier`` inside ``pool``, best first."""
    tier = tier_of(tier)
    if pool.legacy:
        return list(providers.routing.tiers.get(tier, []))
    if pool.tiers.get(tier):
        return list(pool.tiers[tier])
    listed = [r for r in providers.routing.tiers.get(tier, []) if pool.contains_ref(r)]
    if listed:
        return listed
    from django_ergo.bots.providers import DEFAULT_TIER_MODELS

    derived = []
    for kind, name in DEFAULT_TIER_MODELS.get(tier, ()):
        for provider in providers.providers.values():
            if (
                provider.type == kind
                and name in provider.models
                and pool.contains(provider.name, name)
            ):
                derived.append(f"{provider.name}/{name}")  # noqa: PERF401
    return list(dict.fromkeys(derived))


def agent_candidates(providers: Providers, pool: Pool, tier: str) -> list[AgentChoice]:
    """Coding-agent candidates for ``tier`` inside ``pool``."""
    tier = tier_of(tier)
    if pool.legacy:
        return list(providers.routing.agents.get(tier, []))
    if pool.agents.get(tier):
        return list(pool.agents[tier])

    def member(choice: AgentChoice) -> bool:
        provider = providers.providers.get(choice.provider)
        if provider is None:
            return False
        model = choice.model.partition("/")[2] if provider.worker_only else choice.model
        return pool.contains(choice.provider, model)

    listed = [c for c in providers.routing.agents.get(tier, []) if member(c)]
    if listed:
        return listed
    derived = []
    for ref in tier_candidates(providers, pool, tier):
        provider = providers.providers[ref.partition("/")[0]]
        agent = {"claude": "claude", "openai": "codex"}.get(provider.type)
        if agent:
            derived.append(
                AgentChoice(
                    agent=agent,
                    model=ref.partition("/")[2],
                    provider=provider.name,
                    effort=routing.DEFAULT_EFFORT,
                )
            )
    return derived


# -- decisions --------------------------------------------------------------------


@dataclass
class Decision:
    """A selected route (or coding agent) and why."""

    consumer: str
    pool: str
    pool_label: str = ""
    route: str = ""  # provider/model ("" for a coding agent's raw model)
    provider: str = ""
    model: str = ""
    billing: str = SUBSCRIPTION
    tier: str = ""
    mode: str = "auto"  # auto or manual (chat/user pick)
    origin: tuple[str, ...] = ()
    rejected: list[dict] = field(default_factory=list)
    rollover: list[str] = field(default_factory=list)
    legacy: bool = False
    choice: AgentChoice | None = None
    reason: str = ""
    receipt_id: str = ""
    reservation_id: object = None
    reserved_usd: Decimal | None = None
    # For metered routes: the spend envelope the guard enforces.
    budget_usd: Decimal | None = None
    expires_at: dt.datetime | None = None
    max_output_tokens: int = 16_000
    per_launch_usd: Decimal | None = None

    @property
    def metered(self) -> bool:
        return self.billing == METERED

    def summary(self) -> dict:
        return {
            "pool": self.pool,
            "pool_label": self.pool_label,
            "route": self.route,
            "billing": self.billing,
            "mode": self.mode,
            "tier": self.tier,
            "rollover": self.rollover,
            "decision": self.receipt_id,
        }

    def guard(
        self, bot: Bot, session=None, *, purpose: str = "chat"
    ) -> MeteredGuard | None:
        """The spend guard for an engine on this (metered) route."""
        if not self.metered or self.legacy:
            return None
        from django_ergo.conversation.spend_guard import MeteredGuard

        provider = bot.providers.providers.get(self.provider)
        found = provider.models.get(self.model) if provider else None
        return MeteredGuard(
            pool=self.pool,
            provider=self.provider,
            model=self.model,
            price=found.price if found else None,
            budget_usd=self.budget_usd,
            expires_at=self.expires_at,
            max_output_tokens=self.max_output_tokens,
            bot=bot.name,
            user_id=getattr(session, "user_id", None),
            session_id=getattr(session, "pk", None),
            purpose=purpose,
        )


def deny_guard(provider: str, model: str, reason: str) -> MeteredGuard:
    from django_ergo.conversation.spend_guard import MeteredGuard

    return MeteredGuard(pool="", provider=provider, model=model, deny_reason=reason)


def _record(  # noqa: PLR0913
    *,
    bot: Bot,
    session,
    user_id,
    consumer: str,
    requested: str,
    decision: Decision | None = None,
    refusal: PoolRefusal | None = None,
    origin=(),
    mode: str = "",
    rollover=(),
) -> str:
    """Store a RouteDecision receipt (never fails the selection)."""
    from django.db import DatabaseError

    from django_ergo.conversation.models import RouteDecision

    try:
        row = RouteDecision.objects.create(
            session_id=getattr(session, "pk", None),
            user_id=user_id,
            bot_name=bot.name[:100],
            consumer=consumer[:32],
            origin=",".join(origin)[:200],
            mode=(decision.mode if decision else mode)[:16],
            requested=requested[:200],
            tier=(decision.tier if decision else "")[:20],
            pool=(decision.pool if decision else getattr(refusal, "pool", ""))[:64],
            route=(decision.route if decision else "")[:200],
            billing=(decision.billing if decision else "")[:16],
            outcome="selected" if decision else "refused",
            code=getattr(refusal, "code", "")[:32],
            reason=(decision.reason if decision else str(refusal or ""))[:500],
            rejected=(
                decision.rejected if decision else getattr(refusal, "rejected", [])
            )[:50],
            rollover=list(decision.rollover if decision else rollover),
            reserved_usd=decision.reserved_usd if decision else None,
        )
    except DatabaseError:  # pragma: no cover - receipts are best effort
        logger.exception("Couldn't record a route decision")
        return ""
    return str(row.pk)


def next_choices(snap: Snapshot, privacy: Privacy, current: str) -> list[dict]:
    """Pools a person could switch to instead of ``current``."""
    out = []
    for view in snap.pools:
        if view.name == current:
            continue
        problem = view.problem()
        if privacy.restricted and not _pool_permitted(snap, view, privacy):
            continue
        out.append(
            {
                "name": view.name,
                "label": view.title,
                "status": problem[0] if problem else "eligible",
                "metered": view.pool.metered,
            }
        )
    return out


def _pool_permitted(snap: Snapshot, view: PoolView, privacy: Privacy) -> bool:
    providers = snap.providers
    for selector in view.pool.routes:
        head, _, tail = selector.partition("/")
        provider = providers.providers.get(head)
        if provider is None:
            continue
        models = [tail] if tail not in ("", "*") else list(provider.models) or [""]
        if any(privacy.permits(f"{head}/{m}") for m in models):
            return True
    return False


def _start(snap: Snapshot, privacy: Privacy, consumer: str) -> tuple[int, str]:
    """(index of the active pool, mode)."""
    if snap.active_pool:
        for index, view in enumerate(snap.pools):
            if view.name == snap.active_pool:
                return index, "manual"
        msg = f"The selected pool {snap.active_pool!r} isn't configured any more; pick another"
        raise PoolRefusal(
            POOL_DISABLED,
            msg,
            pool=snap.active_pool,
            next_choices=next_choices(snap, privacy, snap.active_pool),
            consumer=consumer,
        )
    for index, view in enumerate(snap.pools):
        if not view.enabled:
            continue
        if privacy.restricted and not _pool_permitted(snap, view, privacy):
            continue
        return index, "auto"
    msg = "No pool is switched on" + (
        f" that privacy policy {privacy.label()} allows" if privacy.restricted else ""
    )
    raise PoolRefusal(
        NO_ROUTE, msg, next_choices=next_choices(snap, privacy, ""), consumer=consumer
    )


def _next_index(snap: Snapshot, index: int, privacy: Privacy) -> int | None:
    for later in range(index + 1, len(snap.pools)):
        view = snap.pools[later]
        if not view.enabled:
            continue
        if privacy.restricted and not _pool_permitted(snap, view, privacy):
            continue
        return later
    return None


def _walk(snap: Snapshot, privacy: Privacy, consumer: str, attempt):  # noqa: C901
    """Try the active pool, rolling over on exhaustion only when all three
    gates permit. Returns (decision, mode)."""
    index, mode = _start(snap, privacy, consumer)
    passed: list[str] = []
    while True:
        view = snap.pools[index]
        if problem := view.problem():
            raise PoolRefusal(
                problem[0],
                problem[1],
                pool=view.name,
                next_choices=next_choices(snap, privacy, view.name),
                consumer=consumer,
            )
        try:
            decision = attempt(view)
        except PoolRefusal as refusal:
            refusal.pool = refusal.pool or view.name
            refusal.consumer = consumer
            refusal.next_choices = next_choices(snap, privacy, view.name)
            if refusal.code != EXHAUSTED:
                raise
            later = _next_index(snap, index, privacy)
            if later is None:
                raise
            target = snap.pools[later]
            closed = []
            if not snap.auto_rollover:
                closed.append("automatic rollover is off")
            if not view.rollover_out:
                closed.append(f"{view.title} doesn't roll over")
            if not target.allow_auto_entry:
                closed.append(f"{target.title} needs to be picked by hand")
            if target.problem() is not None:
                closed.append(target.problem()[1])
            if closed:
                msg = f"{refusal} Not moving to {target.title}: {'; '.join(closed)}."
                raise PoolRefusal(
                    EXHAUSTED,
                    msg,
                    pool=view.name,
                    next_choices=refusal.next_choices,
                    rejected=refusal.rejected,
                    consumer=consumer,
                ) from None
            passed.append(view.name)
            index = later
            continue
        decision.mode = mode
        decision.rollover = passed
        decision.pool = view.name
        decision.pool_label = view.title
        decision.legacy = view.pool.legacy
        if decision.metered and not decision.legacy:
            decision.budget_usd = view.budget_usd
            decision.expires_at = view.expires_at
            decision.max_output_tokens = view.pool.max_output_tokens
            decision.per_launch_usd = view.per_launch_usd
        return decision


def _choose(candidates, provider_of, model_of, current=None):
    return routing.choose(
        candidates,
        provider_of,
        routing.current_usage(),
        routing.active_rules(_ROUTING_HOLDER.get()),
        current if current in candidates else None,
        model_of=model_of,
    )


# routing.active_rules needs the Routing being resolved; a context var keeps the
# helpers small without threading it through every call.
_ROUTING_HOLDER: contextvars.ContextVar = contextvars.ContextVar("ergo_pool_routing")


def _chat_attempt(  # noqa: C901, PLR0913
    snap: Snapshot,
    privacy: Privacy,
    consumer: str,
    ref: str,
    current: str,
    exclude: frozenset[str],
):
    providers = snap.providers

    def attempt(view: PoolView) -> Decision:  # noqa: C901
        pool = view.pool
        if not is_auto(ref):
            provider_name, _, model = ref.partition("/")
            if not pool.contains(provider_name, model):
                msg = (
                    f"{ref} isn't in the active pool {view.title!r}; pick a model from "
                    "it or switch pools"
                )
                raise PoolRefusal(PIN_OUT_OF_POOL, msg, pool=view.name)
            if problem := route_problem(snap, privacy, consumer, provider_name, model):
                raise PoolRefusal(problem[0], f"{ref}: {problem[1]}", pool=view.name)
            provider = providers.providers[provider_name]
            return Decision(
                consumer=consumer,
                pool=view.name,
                route=ref,
                provider=provider_name,
                model=model,
                billing=provider.billing,
                reason="pinned model",
            )
        tier = tier_of(ref)
        listed = tier_candidates(providers, pool, tier)
        if not listed:
            msg = f"Pool {view.title!r} has no {tier!r} tier models"
            raise PoolRefusal(NO_ROUTE, msg, pool=view.name)
        rejected, eligible = [], []
        for candidate in listed:
            provider_name, _, model = candidate.partition("/")
            if candidate in exclude:
                rejected.append(
                    {"route": candidate, "code": "excluded", "reason": "failed"}
                )
                continue
            if problem := route_problem(snap, privacy, consumer, provider_name, model):
                rejected.append(
                    {"route": candidate, "code": problem[0], "reason": problem[1]}
                )
                continue
            eligible.append(candidate)
        if not eligible:
            codes = {r["code"] for r in rejected}
            code = codes.pop() if len(codes) == 1 else NO_ROUTE
            if code == "excluded":
                code = EXHAUSTED
            reasons = "; ".join(f"{r['route']}: {r['reason']}" for r in rejected)
            msg = f"No {tier!r} model in pool {view.title!r} can be used ({reasons})"
            raise PoolRefusal(code, msg, pool=view.name, rejected=rejected)
        picked = _choose(
            eligible,
            lambda r: r.partition("/")[0],
            lambda r: r.partition("/")[2],
            current,
        )
        if picked is None:
            usage = routing.current_usage()
            for candidate in eligible:
                rejected.append(  # noqa: PERF401
                    {
                        "route": candidate,
                        "code": "no_capacity",
                        "reason": routing.pace_block(
                            candidate.partition("/")[0],
                            usage,
                            candidate.partition("/")[2],
                        )
                        or "over its limits",
                    }
                )
            reasons = "; ".join(
                r["reason"] for r in rejected if r["code"] == "no_capacity"
            )
            msg = f"Pool {view.title!r} is exhausted for {tier!r} ({reasons})."
            raise PoolRefusal(EXHAUSTED, msg, pool=view.name, rejected=rejected)
        provider = providers.providers[picked.partition("/")[0]]
        return Decision(
            consumer=consumer,
            pool=view.name,
            route=picked,
            provider=provider.name,
            model=picked.partition("/")[2],
            billing=provider.billing,
            tier=tier,
            rejected=rejected,
            reason=routing.pick_reason(
                provider.name,
                routing.current_usage(),
                routing.active_rules(providers.routing),
                picked.partition("/")[2],
            )
            if provider.billing == SUBSCRIPTION
            else "metered route inside the active pool",
        )

    return attempt


def select_route(  # noqa: PLR0913
    bot: Bot,
    session=None,
    *,
    ref: str,
    consumer: str = CHAT,
    user_id=None,
    current: str = "",
    exclude=(),
    record: bool = True,
    origin=(),
) -> Decision:
    """The route ``consumer`` uses for ``ref`` (``auto/<tier>`` or a pin)
    under the bot's pool policy, or PoolRefusal. Runs the ORM.

    A private selector (``personal``) is resolved by the caller
    (``Bot.private_target``); this checks only that privacy permits it."""
    providers = bot.providers
    privacy = privacy_for(bot, session, origin)
    user_id = user_id if user_id is not None else getattr(session, "user_id", None)
    if providers.is_private(ref):
        if privacy.restricted and not privacy.permits(ref):
            msg = f"Privacy policy {privacy.label()} doesn't allow {ref}"
            raise PoolRefusal(PRIVACY_DENIED, msg, consumer=consumer)
        return Decision(
            consumer=consumer,
            pool=ref.partition("/")[0],
            pool_label=f"Private: {ref.partition('/')[0]}",
            route=ref,
            billing=METERED,
            origin=privacy.names,
            mode="private",
            legacy=True,  # the private pool's own grant and cap apply
            reason="private pool",
        )
    if privacy.restricted:
        msg = (
            f"Privacy policy {privacy.label()} doesn't allow {ref or 'the default model'}; "
            "this work can only use its approved private models"
        )
        refusal = PoolRefusal(PRIVACY_DENIED, msg, consumer=consumer)
        if record:
            _record(
                bot=bot,
                session=session,
                user_id=user_id,
                consumer=consumer,
                requested=ref,
                refusal=refusal,
                origin=privacy.names,
            )
        raise refusal
    snap = snapshot(providers, user_id, session)
    token = _ROUTING_HOLDER.set(providers.routing)
    try:
        decision = _walk(
            snap,
            privacy,
            consumer,
            _chat_attempt(snap, privacy, consumer, ref, current, frozenset(exclude)),
        )
    except PoolRefusal as refusal:
        if record:
            _record(
                bot=bot,
                session=session,
                user_id=user_id,
                consumer=consumer,
                requested=ref,
                refusal=refusal,
                origin=privacy.names,
                mode="manual" if snap.active_pool else "auto",
            )
        raise
    finally:
        _ROUTING_HOLDER.reset(token)
    decision.origin = privacy.names
    if record:
        decision.receipt_id = _record(
            bot=bot,
            session=session,
            user_id=user_id,
            consumer=consumer,
            requested=ref,
            decision=decision,
            origin=privacy.names,
        )
    return decision


# -- coding agents -----------------------------------------------------------------


def _agent_route(providers: Providers, choice: AgentChoice) -> tuple[str, str]:
    provider = providers.providers.get(choice.provider)
    if provider is not None and provider.worker_only:
        return choice.provider, choice.model.partition("/")[2]
    return choice.provider, choice.model


def _match_explicit(
    providers: Providers, pool: Pool, tier: str, agent: str, model: str
) -> list[AgentChoice]:
    """Pool routes an explicit coding model (CLI name or provider/model) names."""
    out = []
    head, sep, tail = model.partition("/")
    for provider in providers.providers.values():
        if (sep and head == provider.name) or (
            sep and provider.worker_only and head == provider.account
        ):
            name = tail
        elif sep:
            continue
        else:
            name = model
        if provider.worker_only:
            if not sep or not pool.contains(provider.name, name):
                continue
            choice = AgentChoice(
                agent="omp", model=f"{provider.account}/{name}", provider=provider.name
            )
        else:
            if name not in provider.models or not pool.contains(provider.name, name):
                continue
            mapped = {"claude": "claude", "openai": "codex"}.get(provider.type, "")
            if not mapped:
                continue
            choice = AgentChoice(agent=mapped, model=name, provider=provider.name)
        if agent and choice.agent != agent:
            continue
        out.append(choice)
    return out


def select_agent(  # noqa: C901, PLR0913, PLR0915
    bot: Bot,
    session=None,
    *,
    tier: str = "",
    model: str = "",
    agent: str = "",
    manager_agents: tuple[str, ...] = (),
    confinable: bool = False,
    spend_bounded: bool = False,
    providers: Providers | None = None,
    receipt: dict | None = None,
    record: bool = True,
) -> Decision | None:
    """The coding agent route for a launch in the active pool, or PoolRefusal.
    ``providers`` is a manager's restricted view (scoped Orca), intersected
    with the pool. Returns None in a legacy deployment for an explicit model
    (the caller keeps today's explicit-model handling). Runs the ORM."""
    bot_providers = bot.providers
    scoped = providers if providers is not None else bot_providers
    privacy = privacy_for(bot, session)
    user_id = getattr(session, "user_id", None)
    requested = (tier and f"agents/{tier_of(tier)}") or model or agent
    if privacy.restricted:
        msg = (
            f"Privacy policy {privacy.label()} has no approved coding route; coding "
            "agents can't run for this work"
        )
        refusal = PoolRefusal(NO_CODING_ROUTE, msg, consumer=CODING)
        if record:
            _record(
                bot=bot,
                session=session,
                user_id=user_id,
                consumer=CODING,
                requested=requested,
                refusal=refusal,
                origin=privacy.names,
            )
        raise refusal
    if bot_providers.pools.legacy:
        if not tier:
            if model:
                matches = _match_explicit(
                    bot_providers,
                    legacy_pool(bot_providers.providers),
                    "",
                    agent,
                    model,
                )
                off = disabled_providers()
                if matches and all(
                    not provider_enabled(bot_providers.providers[c.provider], off)
                    for c in matches
                ):
                    msg = f"Coding model {model!r}: its provider is switched off"
                    raise PoolRefusal(PROVIDER_DISABLED, msg, consumer=CODING)
            return None
        choice = routing.pick_agent(scoped, tier, manager_agents, receipt=receipt)
        legacy = legacy_pool(bot_providers.providers)
        decision = Decision(
            consumer=CODING,
            pool=legacy.name,
            pool_label=legacy.title,
            provider=choice.provider,
            model=choice.model,
            route=f"{choice.provider}/{choice.model}",
            billing=SUBSCRIPTION,
            tier=tier_of(tier),
            choice=choice,
            legacy=True,
            reason=(receipt or {}).get("reason", "subscription pace routing"),
        )
        if record:
            decision.receipt_id = _record(
                bot=bot,
                session=session,
                user_id=user_id,
                consumer=CODING,
                requested=requested,
                decision=decision,
            )
        return decision
    if not tier and not model:
        tier = "medium"  # an unnamed model can't be checked against the pool
    snap = snapshot(bot_providers, user_id, session)
    restricted_view = scoped is not bot_providers

    def admitted(choice: AgentChoice, the_tier: str) -> bool:
        if not restricted_view:
            return True
        return any(
            (a.agent, a.model, a.provider)
            == (choice.agent, choice.model, choice.provider)
            for a in scoped.routing.agents.get(the_tier, [])
        )

    def attempt(view: PoolView) -> Decision:  # noqa: PLR0915, C901, PLR0912
        pool = view.pool
        if tier:
            the_tier = tier_of(tier)
            listed = [
                c
                for c in agent_candidates(bot_providers, pool, the_tier)
                if admitted(c, the_tier)
            ]
            if not listed:
                msg = (
                    f"Pool {view.title!r} has no coding route for the {the_tier!r} tier"
                )
                raise PoolRefusal(NO_CODING_ROUTE, msg, pool=view.name)
        else:
            the_tier = ""
            listed = _match_explicit(bot_providers, pool, tier, agent, model)
            if not listed:
                msg = f"Coding model {model!r} isn't a route in the active pool {view.title!r}; pick one from it or switch pools"
                raise PoolRefusal(PIN_OUT_OF_POOL, msg, pool=view.name)
        rejected, metered, subscription = [], [], []
        for choice in listed:
            provider_name, route_model = _agent_route(bot_providers, choice)
            ref = f"{provider_name}/{route_model}"
            if manager_agents and choice.agent not in manager_agents:
                rejected.append(
                    {
                        "route": ref,
                        "code": NO_ROUTE,
                        "reason": f"this manager doesn't run {choice.agent}",
                    }
                )
                continue
            if agent and choice.agent != agent:
                rejected.append(
                    {"route": ref, "code": NO_ROUTE, "reason": f"not {agent}"}
                )
                continue
            if problem := route_problem(
                snap, privacy, CODING, provider_name, route_model
            ):
                rejected.append(
                    {"route": ref, "code": problem[0], "reason": problem[1]}
                )
                continue
            provider = bot_providers.providers[provider_name]
            if provider.metered:
                if choice.agent == "omp":
                    rejected.append(
                        {
                            "route": ref,
                            "code": UNCONFINABLE,
                            "reason": "omp can't be confined to one metered provider (its fallback can't be switched off)",
                        }
                    )
                    continue
                if METERED_AGENT_FOR.get(provider.type) != choice.agent:
                    rejected.append(
                        {
                            "route": ref,
                            "code": UNCONFINABLE,
                            "reason": f"{choice.agent} isn't {provider.type}'s own CLI",
                        }
                    )
                    continue
                if not (confinable and spend_bounded):
                    rejected.append(
                        {
                            "route": ref,
                            "code": UNCONFINABLE,
                            "reason": (
                                "metered coding is unsupported here: this agent "
                                "manager can't enforce a total spend bound for a CLI "
                                "run (and its provider/model confinement)"
                                if confinable
                                else "metered coding is unsupported here: this agent "
                                "manager can't confine a launch to one metered "
                                "provider and model, or bound its total spend"
                            ),
                        }
                    )
                    continue
                if not provider.api_key_env:
                    rejected.append(
                        {"route": ref, "code": UNAVAILABLE, "reason": "no api_key_env"}
                    )
                    continue
                if view.per_launch_usd is None:
                    rejected.append(
                        {
                            "route": ref,
                            "code": BUDGET_REQUIRED,
                            "reason": "pool has no per_launch_usd",
                        }
                    )
                    continue
                metered.append(choice)
            else:
                if (
                    provider.transport != "cli"
                    or routing.coding_provider(provider) not in routing.CODING_PROVIDERS
                ):
                    rejected.append(
                        {
                            "route": ref,
                            "code": NO_ROUTE,
                            "reason": "not a coding subscription",
                        }
                    )
                    continue
                subscription.append(choice)
        if subscription:
            errors: list[str] = []
            try:
                subscription = routing._eligible_coding_logins(  # noqa: SLF001
                    bot_providers, subscription, the_tier or "explicit", errors
                )
            except ValueError as exc:
                rejected.append(
                    {"route": "subscriptions", "code": UNAVAILABLE, "reason": str(exc)}
                )
                subscription = []
            else:
                rejected.extend(
                    {"route": "login", "code": UNAVAILABLE, "reason": e} for e in errors
                )
        eligible = subscription + metered
        if not eligible:
            codes = {r["code"] for r in rejected}
            code = codes.pop() if len(codes) == 1 else NO_CODING_ROUTE
            reasons = (
                "; ".join(f"{r['route']}: {r['reason']}" for r in rejected)
                or "none listed"
            )
            msg = f"No coding route in pool {view.title!r} can launch ({reasons})"
            raise PoolRefusal(code, msg, pool=view.name, rejected=rejected)
        usage = routing.current_usage()
        picked = routing.choose(
            eligible,
            lambda c: c.provider,
            usage,
            routing.active_rules(bot_providers.routing),
            model_of=lambda c: c.model,
        )
        if picked is None:
            reasons = "; ".join(
                dict.fromkeys(
                    routing.pace_block(c.provider, usage, c.model) for c in eligible
                )
            )
            msg = f"Pool {view.title!r} is exhausted for coding ({reasons})."
            raise PoolRefusal(EXHAUSTED, msg, pool=view.name, rejected=rejected)
        provider = bot_providers.providers[picked.provider]
        if receipt is not None:
            receipt.update(
                {
                    "source": "pool_policy",
                    "selected_at": time.time(),
                    "reason": routing.pick_reason(
                        picked.provider,
                        usage,
                        routing.active_rules(bot_providers.routing),
                        picked.model,
                    )
                    if not provider.metered
                    else "metered route inside the active pool",
                }
            )
        return Decision(
            consumer=CODING,
            pool=view.name,
            route=f"{picked.provider}/{_agent_route(bot_providers, picked)[1]}",
            provider=picked.provider,
            model=picked.model,
            billing=provider.billing,
            tier=the_tier,
            choice=picked,
            rejected=rejected,
            reason=(receipt or {}).get("reason", ""),
        )

    try:
        decision = _walk(snap, privacy, CODING, attempt)
    except PoolRefusal as refusal:
        if record:
            _record(
                bot=bot,
                session=session,
                user_id=user_id,
                consumer=CODING,
                requested=requested,
                refusal=refusal,
                mode="manual" if snap.active_pool else "auto",
            )
        raise
    if decision.metered:
        from django_ergo.conversation.spend_guard import reserve_launch

        decision.reservation_id = reserve_launch(
            pool=decision.pool,
            provider=decision.provider,
            model=decision.model,
            amount=decision.per_launch_usd,
            budget=decision.budget_usd,
            expires_at=decision.expires_at,
            bot=bot.name,
            session_id=getattr(session, "pk", None),
            user_id=user_id,
        )
        decision.reserved_usd = decision.per_launch_usd
    if record:
        decision.receipt_id = _record(
            bot=bot,
            session=session,
            user_id=user_id,
            consumer=CODING,
            requested=requested,
            decision=decision,
        )
    return decision


def revalidate_launch(bot: Bot, session, confined: dict) -> Decision:
    """Check a confined metered launch again before a resumed run (provider
    still on, pool still usable, privacy unchanged) and reserve its per-launch
    budget again; PoolRefusal otherwise. Runs the ORM."""
    from django_ergo.conversation.spend_guard import reserve_launch

    privacy = privacy_for(bot, session)
    if privacy.restricted:
        msg = f"Privacy policy {privacy.label()} has no approved coding route"
        raise PoolRefusal(NO_CODING_ROUTE, msg, consumer=CODING)
    snap = snapshot(bot.providers, getattr(session, "user_id", None), session)
    view = snap.get(str(confined.get("pool") or ""))
    if view is None:
        msg = f"Pool {confined.get('pool')!r} isn't configured any more"
        raise PoolRefusal(POOL_DISABLED, msg, consumer=CODING)
    if problem := view.problem():
        raise PoolRefusal(problem[0], problem[1], pool=view.name, consumer=CODING)
    provider = str(confined.get("provider") or "")
    model = str(confined.get("model") or "")
    if not view.pool.contains(provider, model):
        msg = f"{provider}/{model} isn't in pool {view.title!r} any more"
        raise PoolRefusal(PIN_OUT_OF_POOL, msg, pool=view.name, consumer=CODING)
    if problem := route_problem(snap, privacy, CODING, provider, model):
        raise PoolRefusal(
            problem[0],
            f"{provider}/{model}: {problem[1]}",
            pool=view.name,
            consumer=CODING,
        )
    decision = Decision(
        consumer=CODING,
        pool=view.name,
        pool_label=view.title,
        route=f"{provider}/{model}",
        provider=provider,
        model=model,
        billing=METERED,
        budget_usd=view.budget_usd,
        expires_at=view.expires_at,
        per_launch_usd=view.per_launch_usd,
        reason="resumed confined launch",
    )
    decision.reservation_id = reserve_launch(
        pool=view.name,
        provider=provider,
        model=model,
        amount=view.per_launch_usd,
        budget=view.budget_usd,
        expires_at=view.expires_at,
        bot=bot.name,
        session_id=getattr(session, "pk", None),
        user_id=getattr(session, "user_id", None),
    )
    decision.reserved_usd = view.per_launch_usd
    decision.receipt_id = _record(
        bot=bot,
        session=session,
        user_id=getattr(session, "user_id", None),
        consumer=CODING,
        requested=decision.route,
        decision=decision,
    )
    return decision


def confinement(bot: Bot, decision: Decision) -> dict | None:
    """What a manager must enforce to launch a metered coding route: exactly
    this provider's key, this model, no other provider credentials."""
    if not decision.metered or decision.legacy or decision.choice is None:
        return None
    provider = bot.providers.providers[decision.provider]
    return {
        "pool": decision.pool,
        "provider": provider.name,
        "engine": provider.type,
        "agent": decision.choice.agent,
        "model": decision.choice.model,
        "key_env": provider.api_key_env,
    }


# -- listing (UI and tools use the same eligibility) ---------------------------------


def listing(bot: Bot, session=None, user_id=None) -> dict:
    """Pools, their status and the chat models each offers this bot now,
    computed with the same rules selection uses. Runs the ORM."""
    providers = bot.providers
    privacy = privacy_for(bot, session)
    user_id = user_id if user_id is not None else getattr(session, "user_id", None)
    out: dict = {
        "legacy": providers.pools.legacy,
        "privacy": {
            "restricted": privacy.restricted,
            "policies": list(privacy.names),
            "label": privacy.label(),
        },
    }
    if privacy.restricted:
        pools = []
        for name, pool in sorted(providers.private_pools.items()):
            if not privacy.permits(name):
                continue
            pools.append(
                {
                    "name": name,
                    "label": f"Private: {name}",
                    "status": "eligible" if pool.enabled else POOL_DISABLED,
                    "locked": True,
                    "models": [
                        {
                            "id": f"{name}/{alias}",
                            "label": alias,
                            "default": alias == pool.default,
                        }
                        for alias in pool.models
                    ],
                }
            )
        out.update(
            {
                "pools": pools,
                "active": pools[0]["name"] if pools else "",
                "mode": "private",
            }
        )
        return out
    out.update(public_view(providers, privacy, snapshot(providers, user_id, session)))
    return out


def settings_view(providers: Providers, user_id=None) -> dict:
    """The pools page: every pool, provider and the user's choices, with no
    bot or chat in scope (no privacy restriction). Runs the ORM."""
    return {
        "legacy": providers.pools.legacy,
        **public_view(providers, Privacy(), snapshot(providers, user_id)),
    }


def public_view(providers: Providers, privacy: Privacy, snap: Snapshot) -> dict:
    rows = []
    for view in snap.pools:
        problem = view.problem()
        models = []
        for selector in view.pool.routes:
            head, _, tail = selector.partition("/")
            provider = providers.providers.get(head)
            if provider is None or provider.worker_only:
                continue
            names = [tail] if tail not in ("", "*") else list(provider.models)
            for name in names:
                issue = route_problem(snap, privacy, CHAT, head, name)
                model = provider.models.get(name)
                models.append(
                    {
                        "id": f"{head}/{name}",
                        "label": (model.label if model else "") or name,
                        "billing": provider.billing,
                        "available": issue is None,
                        "why": issue[1] if issue else "",
                    }
                )
        tiers = {}
        token = _ROUTING_HOLDER.set(providers.routing)
        try:
            for tier in sorted({*providers.routing.tiers, *view.pool.tiers}):
                refs = tier_candidates(providers, view.pool, tier)
                if refs:
                    tiers[tier] = refs
        finally:
            _ROUTING_HOLDER.reset(token)
        rows.append(
            {
                "name": view.name,
                "label": view.title,
                "position": view.position,
                "enabled": view.enabled,
                "status": problem[0] if problem else "eligible",
                "why": problem[1] if problem else "",
                "metered": view.pool.metered,
                "legacy": view.pool.legacy,
                "rollover_out": view.rollover_out,
                "allow_auto_entry": view.allow_auto_entry,
                "budget_usd": str(view.budget_usd)
                if view.budget_usd is not None
                else None,
                "spent_usd": str(view.spent_usd),
                "reserved_usd": str(view.reserved_usd),
                "expires_at": view.expires_at.isoformat() if view.expires_at else None,
                "per_launch_usd": str(view.per_launch_usd)
                if view.per_launch_usd is not None
                else None,
                "version": view.version,
                "routes": list(view.pool.routes),
                "models": models,
                "tiers": tiers,
            }
        )
    try:
        start_index, mode = _start(snap, privacy, CHAT)
        active = snap.pools[start_index].name
    except PoolRefusal:
        active, mode = "", "auto"
    return {
        "pools": rows,
        "active": active,
        "mode": mode,
        "scope": snap.active_scope,
        "selected": snap.active_pool,
        "auto_rollover": snap.auto_rollover,
        "user_version": snap.user_version,
        "providers": [
            {
                "name": p.name,
                "type": p.type,
                "transport": p.transport,
                "billing": p.billing,
                "enabled": provider_enabled(p, snap.disabled),
                "configured_enabled": p.enabled,
                "available": p.available,
            }
            for p in providers.providers.values()
        ],
        "reset_times": _reset_times(providers),
    }


def _reset_times(providers: Providers) -> dict:
    """{provider: {window: resets_at}} from the latest usage snapshots."""
    usage = routing.current_usage()
    windows = getattr(usage, "windows", {}) or {}
    out = {}
    for name in providers.providers:
        found = {
            window: info.get("resets_at")
            for window, info in (windows.get(name) or {}).items()
            if isinstance(info, dict) and info.get("resets_at")
        }
        if found:
            out[name] = found
    return out


def chat_models(bot: Bot, session=None, user_id=None) -> list[str]:
    """Model refs a chat may pick now: the active pool's usable models (all
    public models in a legacy deployment). Runs the ORM."""
    view = listing(bot, session, user_id)
    if view["privacy"]["restricted"]:
        return [m["id"] for p in view["pools"] for m in p["models"]]
    active = next((p for p in view["pools"] if p["name"] == view["active"]), None)
    if active is None:
        return []
    return [m["id"] for m in active["models"] if m["available"]]


# -- runtime selection state ------------------------------------------------------------


def set_chat_pool(session, pool: str, providers: Providers) -> None:
    """Pin a chat to ``pool`` ("" = follow the user's default). Runs the ORM."""
    if pool and providers.pools.get(pool) is None:
        msg = f"No pool named {pool!r}"
        raise ValueError(msg)
    if pool and providers.pools.legacy:
        msg = "Pools aren't configured (providers.yaml has no pools section)"
        raise ValueError(msg)
    meta = dict(session.metadata or {})
    if pool:
        meta[SESSION_POOL_KEY] = pool
    else:
        meta.pop(SESSION_POOL_KEY, None)
    meta.pop(SESSION_REFUSAL_KEY, None)
    meta.pop("routed_model", None)  # pick again inside the new pool
    session.metadata = meta
    session.save(update_fields=["metadata", "updated_at"])


def update_preference(  # noqa: PLR0913
    user_id,
    providers: Providers,
    *,
    version: int | None,
    active_pool: str | None = None,
    auto_rollover: bool | None = None,
    order: list[str] | None = None,
    clear_auto_rollover: bool = False,
):
    """Change a user's pool choices atomically; ValueError on a version
    conflict or an unknown pool. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import ModelPoolPreference

    names = [p.name for p in providers.pools.pools]
    if active_pool and active_pool not in names:
        msg = f"No pool named {active_pool!r}"
        raise ValueError(msg)
    if order is not None and sorted(order) != sorted(names):
        msg = "order must list every pool exactly once"
        raise ValueError(msg)
    with transaction.atomic():
        pref, _ = ModelPoolPreference.objects.select_for_update().get_or_create(
            key=preference_key(user_id), defaults={"user_id": user_id, "version": 0}
        )
        if version is not None and pref.version != version:
            msg = f"Pool settings changed elsewhere (version {pref.version}, not {version}); reload"
            raise VersionConflict(msg)
        if active_pool is not None:
            pref.active_pool = active_pool
        if auto_rollover is not None:
            pref.auto_rollover = auto_rollover
        if clear_auto_rollover:
            pref.auto_rollover = None
        if order is not None:
            pref.order = order
        pref.version += 1
        pref.save()
    return pref


class VersionConflict(ValueError):  # noqa: N818
    pass


def update_pool_state(
    name: str, providers: Providers, *, version: int | None, **changes
):
    """Change one pool's runtime overrides atomically. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import ModelPoolState

    pool = providers.pools.get(name)
    if pool is None or pool.legacy:
        msg = f"No configured pool named {name!r}"
        raise ValueError(msg)
    allowed = {
        "enabled",
        "rollover_out",
        "allow_auto_entry",
        "budget_usd",
        "expires_at",
        "per_launch_usd",
    }
    if unknown := set(changes) - allowed:
        msg = f"Unknown pool settings: {', '.join(sorted(unknown))}"
        raise ValueError(msg)
    with transaction.atomic():
        state, _ = ModelPoolState.objects.select_for_update().get_or_create(
            name=name, defaults={"version": 0}
        )
        if version is not None and state.version != version:
            msg = f"Pool {name!r} changed elsewhere (version {state.version}, not {version}); reload"
            raise VersionConflict(msg)
        for key, value in changes.items():
            if key == "budget_usd":
                state.budget_override = True
                state.budget_usd = None if value is None else Decimal(str(value))
                if state.budget_usd is not None and state.budget_usd < 0:
                    msg = "budget_usd must be 0 or more"
                    raise ValueError(msg)
            elif key == "expires_at":
                state.expires_override = True
                state.expires_at = value
            elif key == "per_launch_usd":
                state.per_launch_usd = None if value is None else Decimal(str(value))
            else:
                if value is not None and not isinstance(value, bool):
                    msg = f"{key} must be true, false or null"
                    raise ValueError(msg)
                setattr(state, key, value)
        state.version += 1
        state.save()
    return state


def set_provider_enabled(
    name: str, providers: Providers, enabled: bool, *, version: int | None = None
):
    from django.db import transaction

    from django_ergo.conversation.models import ProviderToggle

    if name not in providers.providers:
        msg = f"No provider named {name!r}"
        raise ValueError(msg)
    with transaction.atomic():
        toggle, _ = ProviderToggle.objects.select_for_update().get_or_create(
            name=name, defaults={"version": 0}
        )
        if version is not None and toggle.version != version:
            msg = f"Provider {name!r} changed elsewhere; reload"
            raise VersionConflict(msg)
        toggle.enabled = bool(enabled)
        toggle.version += 1
        toggle.save()
    return toggle


def restricted_view(providers: Providers, refs) -> Providers:
    """``providers`` with tiers narrowed to ``refs`` (helpers for legacy callers)."""
    allowed = set(refs)
    return replace(
        providers,
        routing=replace(
            providers.routing,
            tiers={
                t: [r for r in rs if r in allowed]
                for t, rs in providers.routing.tiers.items()
            },
        ),
    )
