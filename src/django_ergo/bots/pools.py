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
import datetime as dt
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
WEB_SEARCH = "web_search"
VIDEO = "video"
CONSUMERS = (CHAT, TITLE, COMPACTION, BROWSER, CODING, BACKGROUND, WEB_SEARCH, VIDEO)

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
NOT_AUTHORIZED = "not_authorized"  # not this user's pool, or not in their grant
GRANT_NOT_YET_VALID = "grant_not_yet_valid"

SESSION_POOL_KEY = "pool"  # session.metadata: this chat's manual pool
SESSION_ROUTE_KEY = "pool_route"  # session.metadata: the last decision (display)
SESSION_REFUSAL_KEY = "pool_refusal"  # session.metadata: the last refusal (UI)
SESSION_VERSION_KEY = "pool_version"  # session.metadata: the override's revision
SESSION_ROLLED_KEY = "pool_rolled_from"  # session.metadata: set by a rollover
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

    def permits_memory(self, bank: str, *, reflect: bool = False) -> bool:
        """Whether every policy approves this Hindsight bank as a recipient
        (and, with ``reflect``, its LLM synthesis)."""
        return all(
            bank in (p.memory_reflect if reflect else p.memory) for p in self.policies
        )

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


def memory_refusal(bot: Bot, session, bank: str, *, reflect: bool = False) -> str:
    """Why this work may not send to (or read from) Hindsight bank ``bank``,
    or "": restricted work only uses banks its policy lists (``memory``, and
    ``memory_reflect`` for reflect); a bank a policy lists is that policy's,
    so other work neither writes nor reads it."""
    privacy = privacy_for(bot, session)
    if privacy.restricted:
        if not privacy.permits_memory(bank, reflect=reflect):
            what = "memory reflect" if reflect else "memory"
            return (
                f"privacy policy {privacy.label()} doesn't approve the {bank!r} "
                f"{what} bank for this work"
            )
        return ""
    owners = [
        name
        for name, policy in bot.providers.pools.privacy.items()
        if bank in policy.memory
    ]
    if owners:
        return f"memory bank {bank!r} belongs to privacy policy {', '.join(owners)}"
    return ""


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


def pool_refs(
    providers: Providers, pool: Pool, *, metered: bool | None = None
) -> list[str]:
    """The exact ``provider/model`` routes a pool holds (``metered`` narrows
    to metered or subscription routes). Coding-only (omp) routes excluded."""
    out = []
    for selector in pool.routes:
        head, _, tail = selector.partition("/")
        provider = providers.providers.get(head)
        if provider is None or provider.worker_only:
            continue
        if metered is not None and provider.metered != metered:
            continue
        names = [tail] if tail not in ("", "*") else list(provider.models)
        out.extend(f"{head}/{name}" for name in names)
    return list(dict.fromkeys(out))


def owner_ids(providers: Providers) -> dict[str, int | None]:
    """{owner username: user id or None (no such user)}. Runs the ORM."""
    from django.contrib.auth import get_user_model

    names = {p.owner for p in providers.pools.pools if p.owner}
    if not names:
        return {}
    model = get_user_model()
    found = dict(
        model.objects.filter(**{f"{model.USERNAME_FIELD}__in": names}).values_list(
            model.USERNAME_FIELD, "pk"
        )
    )
    return {name: found.get(name) for name in names}


# A queued job's captured execution policy (``capture_policy``): while it
# runs, selection uses the pool and grant it was queued under, never wider.
_job_policy: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ergo_pool_job_policy", default=None
)


@contextlib.contextmanager
def policy_scope(policy: dict | None):
    """Run queued work under the policy it captured when it was queued."""
    token = _job_policy.set(dict(policy) if policy else None)
    try:
        yield
    finally:
        _job_policy.reset(token)


def current_policy() -> dict | None:
    return _job_policy.get()


# -- spend authority: envelopes and their revisions --------------------------------
#
# A SpendEnvelope is one line of spend authority with one cumulative ledger
# and one lock; its SpendGrant rows are immutable revisions. The owner's own
# authority from providers.yaml is the envelope ``config:<pool>:<owner id>``:
# editing the config adds a revision (the previous one is superseded),
# removing the budget or the pool, or moving the pool to another owner,
# revokes every revision, and restoring it adds a revision to the SAME
# envelope, so no config change ever replenishes money.
#
# The envelope's identity is not its pool's name. A pool that has no
# envelope yet, but whose metered accounts (provider, key env, account
# names) overlap an existing config envelope's, continues that envelope
# (re-keyed to the new name) when that is unambiguous: one overlapping
# envelope, the same owner, and no current pool still using it. Anything
# else waits for an admin's explicit :func:`link_config_envelope`. Config
# sync funds a new envelope only for accounts no envelope has ever funded.
#
# A grant issued at runtime is its own envelope, drawn from the owner's
# config envelope (``parent``): every send on it is intersected with the
# owner's current authority (budget, expiry, routes, ceiling, per-launch,
# ownership, switches), and the owner's current budget caps the cumulative
# spend of the owner and every grant they issued on the pool.

CONFIG_SOURCE = "config"
# revoked_reason prefix of revocations made by following providers.yaml (any
# other revocation was a person's, and config sync never undoes it).
SYNC_REVOKED = "config sync: "
# Format of the config_key fingerprint (see _config_terms).
CONFIG_KEY_VERSION = "2"
# One advisory lock for creating, continuing and linking config envelopes.
CONFIG_ENVELOPES_LOCK = "config-envelopes"


class ConfigUnavailable(ValueError):  # noqa: N818
    """providers.yaml can't be read or validated now: metered authority
    fails closed (subscription routes don't depend on it)."""


def fresh_providers(providers: Providers) -> Providers:
    """``providers`` as providers.yaml is now (``providers.current_providers``),
    or ConfigUnavailable. Every spend decision goes through this, so a
    process holding an older load acts on the current file."""
    from django_ergo.bots.providers import ProvidersUnreadable
    from django_ergo.bots.providers import current_providers

    try:
        return current_providers(providers)
    except ProvidersUnreadable as exc:
        raise ConfigUnavailable(str(exc)) from exc


def config_envelope_key(pool: str, owner_id) -> str:
    return f"config:{pool}:{owner_id}"


# The key env each SDK reads when a provider names none.
SDK_KEY_ENVS = {
    "openai": ("OPENAI_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def provider_accounts(provider: Provider) -> list[str]:
    """What identifies a provider's account across renames: its name, the
    key env it reads (the SDK's default when it names none) and its account
    name. Any one shared means the same money."""
    out = {f"provider:{provider.name}"}
    envs = (
        (provider.api_key_env,)
        if provider.api_key_env
        else SDK_KEY_ENVS.get(provider.type, ())
    )
    out.update(f"env:{env}" for env in envs)
    if provider.account:
        out.add(f"account:{provider.account}")
    return sorted(out)


def pool_accounts(providers: Providers, pool: Pool) -> list[str]:
    """The metered accounts a pool's routes spend on (``provider_accounts``)."""
    out = set()
    for ref in pool_refs(providers, pool, metered=True):
        out.update(provider_accounts(providers.providers[ref.partition("/")[0]]))
    return sorted(out)


def _switched_off_accounts() -> set[str]:
    """The accounts of providers switched off at runtime: a switch follows
    the account even when providers.yaml renames the provider. Runs the ORM."""
    from django_ergo.conversation.models import ProviderToggle

    out = set()
    for name, accounts in ProviderToggle.objects.filter(enabled=False).values_list(
        "name", "accounts"
    ):
        out.add(f"provider:{name}")
        out.update(accounts or ())
    return out


def _config_terms(pool: Pool, providers: Providers, owner_id) -> dict | None:
    """The revision providers.yaml grants the pool's owner now (with its
    ``config_key`` fingerprint), or None when it grants nothing."""
    import hashlib
    import json

    if (
        pool.legacy
        or not pool.metered
        or pool.budget_usd is None
        or pool.expires_at is None
        or owner_id is None
    ):
        return None
    routes = pool_refs(providers, pool, metered=True)
    # The money terms leave out the pool's name (the envelope is the
    # identity); the whole terms include it: a rename is a new revision
    # carrying the new name.
    money = [
        str(pool.budget_usd),
        str(pool.per_launch_usd),
        pool.expires_at.isoformat(),
    ]
    whole = json.dumps([*money, pool.name, routes, pool.max_output_tokens])
    # "2:<money terms>:<all terms>".
    key = (
        f"{CONFIG_KEY_VERSION}:"
        + hashlib.sha256(json.dumps(money).encode()).hexdigest()[:29]
        + ":"
        + hashlib.sha256(whole.encode()).hexdigest()[:32]
    )
    return {
        "routes": routes,
        "budget_usd": pool.budget_usd,
        "per_launch_usd": pool.per_launch_usd,
        "max_output_tokens": pool.max_output_tokens,
        "expires_at": pool.expires_at,
        "config_key": key,
    }


def _current_revision(envelope_id):
    from django_ergo.conversation.models import SpendGrant

    return (
        SpendGrant.objects.filter(
            envelope_id=envelope_id, revoked_at__isnull=True, superseded_at__isnull=True
        )
        .order_by("-created_at")
        .first()
    )


def _revoke_envelope(envelope_id, reason: str, now) -> None:
    from django_ergo.conversation.models import SpendGrant

    SpendGrant.objects.filter(envelope_id=envelope_id, revoked_at__isnull=True).update(
        revoked_at=now, revoked_reason=reason[:200]
    )


def _envelope_problem(envelope, providers: Providers, owners: dict) -> str:
    """Why current providers.yaml no longer backs ``envelope``, or ""."""
    pool = providers.pools.get(envelope.pool)
    if pool is None or pool.legacy:
        return f"pool {envelope.pool!r} was removed from providers.yaml"
    owner_id = owners.get(pool.owner) if pool.owner else None
    if owner_id is None or owner_id != envelope.owner_id:
        return f"pool {envelope.pool!r} has another owner now"
    if envelope.source == CONFIG_SOURCE and (
        envelope.grantee_id != owner_id
        or _config_terms(pool, providers, owner_id) is None
    ):
        return (
            f"providers.yaml no longer sets a budget and expiry for {envelope.pool!r}"
        )
    if envelope.source == CONFIG_SOURCE:
        return _growth_problem(envelope, providers, pool)
    return ""


def _growth_problem(envelope, providers: Providers, pool: Pool) -> str:
    """Why a config envelope can't take on its pool's new accounts (other
    envelopes already fund them), or "". Runs the ORM when it grew."""
    growth = set(pool_accounts(providers, pool)) - set(envelope.accounts or ())
    others = [e for e in _overlapping(growth) if e.pk != envelope.pk]
    if not others:
        return ""
    names = ", ".join(sorted({repr(e.pool) for e in others}))
    return (
        f"pool {envelope.pool!r} now spends on accounts that already have "
        f"spend authority (pool {names}); an admin must approve them"
    )


def _settle(envelope, providers: Providers, owners: dict, now):
    """Bring one envelope in line with current providers.yaml, under its
    lock (the caller holds it): revoke every revision when the config no
    longer backs it, or add a revision when the owner's config terms
    changed. (current revision or None, why none). Runs the ORM."""
    from django_ergo.conversation.models import SpendGrant

    problem = _envelope_problem(envelope, providers, owners)
    if problem and envelope.source != CONFIG_SOURCE:
        # An issued grant is suspended, not revoked: config sync never
        # destroys a person's explicit grant (a process still on an older
        # config could otherwise revoke the current owner's grants for good).
        # It works again only if its issuer owns the pool again.
        return None, problem
    if problem:
        _revoke_envelope(envelope.pk, SYNC_REVOKED + problem, now)
        return None, problem
    current = _current_revision(envelope.pk)
    if envelope.source != CONFIG_SOURCE:
        return current, "" if current is not None else "the spend grant was revoked"
    pool = providers.pools.get(envelope.pool)
    terms = _config_terms(pool, providers, envelope.owner_id)
    accounts = sorted(
        set(envelope.accounts or ()) | set(pool_accounts(providers, pool))
    )
    if accounts != list(envelope.accounts or ()):
        # Only grows: a later pool on any account it funded is recognized.
        envelope.accounts = accounts
        envelope.save(update_fields=["accounts"])
    if current is not None and current.config_key == terms["config_key"]:
        return current, ""
    if current is None:
        newest = (
            SpendGrant.objects.filter(envelope_id=envelope.pk)
            .order_by("-created_at")
            .first()
        )
        if (
            newest is not None
            and newest.revoked_at is not None
            and not newest.revoked_reason.startswith(SYNC_REVOKED)
            and (
                envelope.restored_at is None or envelope.restored_at < newest.revoked_at
            )
        ):
            # A person revoked it: no config edit (even a narrowing one) and
            # no other link issues it again; only an admin's explicit
            # link_config_envelope(restore=True) does.
            return None, "the spend grant was revoked (an admin can restore it)"
    revision = SpendGrant.objects.create(
        envelope=envelope,
        pool=envelope.pool,
        owner_id=envelope.owner_id,
        grantee_id=envelope.owner_id,
        valid_from=now,
        source=CONFIG_SOURCE,
        **terms,
    )
    SpendGrant.objects.filter(
        envelope_id=envelope.pk, revoked_at__isnull=True, superseded_at__isnull=True
    ).exclude(pk=revision.pk).update(superseded_at=now)
    revision.refresh_from_db()  # values as stored (time zone handling included)
    return revision, ""


def _overlapping(accounts) -> list:
    """Config envelopes (any state) that funded any of ``accounts``."""
    from django_ergo.conversation.models import SpendEnvelope

    wanted = set(accounts)
    return [
        e
        for e in SpendEnvelope.objects.filter(source=CONFIG_SOURCE).order_by(
            "created_at", "pk"
        )
        if wanted & set(e.accounts or ())
    ]


def _claimed_keys(providers: Providers, owners: dict) -> set[str]:
    """The config envelope keys the current pools stand for."""
    return {
        config_envelope_key(p.name, owners.get(p.owner))
        for p in providers.pools.pools
        if p.owner and owners.get(p.owner) is not None
    }


def config_link_problem(pool: Pool, providers: Providers, owners: dict) -> str:
    """Why the owner's config authority on ``pool`` waits for an admin's
    link (its metered accounts already have spend authority elsewhere), or
    "" (it has an envelope, may be funded, or continues one). Runs the ORM."""
    from django_ergo.conversation.models import SpendEnvelope

    owner_id = owners.get(pool.owner) if pool.owner else None
    if _config_terms(pool, providers, owner_id) is None:
        return ""
    key = config_envelope_key(pool.name, owner_id)
    envelope = SpendEnvelope.objects.filter(key=key).first()
    if envelope is not None:
        return _growth_problem(envelope, providers, pool)
    _, problem = _continuable(pool, providers, owners)
    return problem


def _continuable(pool: Pool, providers: Providers, owners: dict):
    """(the envelope a new config pool continues, "") or (None, why it must
    be linked) or (None, "") when no envelope ever funded its accounts."""
    owner_id = owners.get(pool.owner) if pool.owner else None
    overlap = _overlapping(pool_accounts(providers, pool))
    if not overlap:
        return None, ""
    claimed = _claimed_keys(providers, owners) - {
        config_envelope_key(pool.name, owner_id)
    }
    free = [e for e in overlap if e.key not in claimed]
    if len(overlap) == 1 and free and free[0].owner_id == owner_id:
        return free[0], ""
    names = ", ".join(sorted({repr(e.pool) for e in overlap}))
    return None, (
        f"pool {pool.name!r} spends on accounts that already have spend "
        f"authority (pool {names}); an admin must link it to that authority or "
        "fund it separately before it can spend"
    )


def _carry_pool_switch(old: str, new: str) -> None:
    """A pool switched off at runtime stays off under its new name (its
    envelope continues under that name). Runs the ORM."""
    from django_ergo.conversation.models import ModelPoolState

    if ModelPoolState.objects.filter(name=old, enabled=False).exists():
        state, _ = ModelPoolState.objects.get_or_create(
            name=new, defaults={"version": 0}
        )
        if state.enabled is not False:
            state.enabled = False
            state.version += 1
            state.save()


def _config_envelope(pool: Pool, providers: Providers, owners: dict, now):
    """The owner's config envelope for ``pool``: its own, a continued one
    (renamed or re-added pool), a new one for accounts never funded, or
    None when it waits for an admin's link. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import SpendEnvelope
    from django_ergo.conversation.spend_guard import envelope_lock_key
    from django_ergo.conversation.spend_guard import lock

    owner_id = owners.get(pool.owner) if pool.owner else None
    key = config_envelope_key(pool.name, owner_id)
    found = SpendEnvelope.objects.filter(key=key).first()
    if found is not None:
        return found
    with transaction.atomic():
        lock(CONFIG_ENVELOPES_LOCK)
        found = SpendEnvelope.objects.filter(key=key).first()
        if found is not None:
            return found
        continued, problem = _continuable(pool, providers, owners)
        if problem:
            return None
        if continued is None and not pool_accounts(providers, pool):
            return None  # nothing to identify the money by: fund nothing
        if continued is not None:
            # Under its own lock too: a send on it never sees half a rename.
            lock(envelope_lock_key(continued.pk))
            continued = SpendEnvelope.objects.get(pk=continued.pk)
            logger.info(
                "Pool %r continues the spend envelope of pool %r",
                pool.name,
                continued.pool,
            )
            continued.key = key
            continued.link_note = (
                f"continued from pool {continued.pool!r} by config sync"
            )[:200]
            _carry_pool_switch(continued.pool, pool.name)
            continued.pool = pool.name
            continued.save(update_fields=["key", "pool", "link_note"])
            return continued
        return SpendEnvelope.objects.create(
            key=key,
            pool=pool.name,
            source=CONFIG_SOURCE,
            owner_id=owner_id,
            grantee_id=owner_id,
            accounts=pool_accounts(providers, pool),
        )


def sync_authority(providers: Providers, owners: dict | None = None) -> None:
    """Make stored spend authority match current providers.yaml (re-read
    from its file when ``providers`` is an older load): revoke revisions
    whose pool is gone, has another owner, or (config) no longer sets a
    budget; add the owner's config revision when it changed, continuing a
    renamed pool's envelope. Each envelope is settled in its own transaction
    under its own lock (one at a time). Cheap when nothing changed. Writes
    nothing when providers.yaml can't be read (sends then fail closed).
    Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import SpendEnvelope
    from django_ergo.conversation.models import SpendGrant
    from django_ergo.conversation.spend_guard import envelope_lock_key
    from django_ergo.conversation.spend_guard import lock

    try:
        fresh = fresh_providers(providers)
    except ConfigUnavailable:
        logger.warning("Not syncing spend authority: providers.yaml doesn't load")
        return
    if fresh is not providers:
        providers, owners = fresh, None
    if providers.pools.legacy:
        return
    owners = owner_ids(providers) if owners is None else owners
    live = list(
        SpendGrant.objects.filter(revoked_at__isnull=True).select_related("envelope")
    )
    now = timezone.now()
    unsettled = set()
    for pool in providers.pools.pools:
        owner_id = owners.get(pool.owner) if pool.owner else None
        terms = _config_terms(pool, providers, owner_id)
        if terms is None:
            continue
        key = config_envelope_key(pool.name, owner_id)
        if any(
            g.envelope.key == key
            and g.superseded_at is None
            and g.config_key == terms["config_key"]
            for g in live
        ):
            continue
        envelope = _config_envelope(pool, providers, owners, now)
        if envelope is not None:
            unsettled.add(envelope.pk)
    unsettled |= {
        g.envelope_id
        for g in live
        if g.envelope.source == CONFIG_SOURCE
        and _envelope_problem(g.envelope, providers, owners)
    }
    for envelope_id in sorted(unsettled, key=str):
        with transaction.atomic():
            lock(envelope_lock_key(envelope_id))
            # Re-read under the lock: a continuation may have re-keyed it.
            envelope = SpendEnvelope.objects.get(pk=envelope_id)
            _settle(envelope, providers, owners, now)


def link_candidates(providers: Providers, pool: str) -> dict:
    """For an admin deciding :func:`link_config_envelope`: why the pool
    waits ("" when it doesn't) and the config envelopes overlapping its
    metered accounts (id, pool, owner, accounts, whether a current pool
    uses it, whether it may be continued, its spend). Runs the ORM."""
    from django_ergo.conversation.spend_guard import family_spent_and_reserved

    providers = fresh_providers(providers)
    config = providers.pools.get(pool)
    if config is None or config.legacy:
        msg = f"No configured pool named {pool!r}"
        raise ValueError(msg)
    owners = owner_ids(providers)
    owner_id = owners.get(config.owner) if config.owner else None
    claimed = _claimed_keys(providers, owners) - {config_envelope_key(pool, owner_id)}
    rows = []
    for envelope in _overlapping(pool_accounts(providers, config)):
        spent, held = family_spent_and_reserved(envelope.pk)
        used = envelope.key in claimed
        rows.append(
            {
                "id": str(envelope.pk),
                "pool": envelope.pool,
                "owner_id": envelope.owner_id,
                "accounts": list(envelope.accounts or ()),
                "in_use": used,
                "continuable": not used and envelope.owner_id == owner_id,
                "spent_usd": str(spent),
                "reserved_usd": str(held),
            }
        )
    return {
        "pool": pool,
        "accounts": pool_accounts(providers, config),
        "problem": config_link_problem(config, providers, owners),
        "envelopes": rows,
    }


def link_config_envelope(  # noqa: PLR0912, PLR0913, PLR0915, C901
    providers: Providers,
    pool: str,
    *,
    by_id,
    admin: bool,
    envelope_id=None,
    fund: bool = False,
    restore: bool = False,
    note: str = "",
):
    """An admin's explicit decision for a config pool whose metered accounts
    already have spend authority (:func:`config_link_problem`): continue
    ``envelope_id`` (its cumulative ledger and any revocation carry over), or
    ``fund`` a new envelope (new money, so only by an admin). On a pool that
    has its envelope, ``fund`` approves the accounts it now spends on, and
    ``restore`` lifts a person's revocation of it (separate decisions; its
    ledger carries on either way). Returns the envelope. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import SpendEnvelope
    from django_ergo.conversation.spend_guard import envelope_lock_key
    from django_ergo.conversation.spend_guard import lock

    if not admin or not by_id:
        msg = "Only an admin can link a pool's spend authority"
        raise PermissionError(msg)
    if envelope_id and (fund or restore):
        msg = "Continue an envelope, or fund/restore one, not both"
        raise ValueError(msg)
    providers = fresh_providers(providers)
    config = providers.pools.get(pool)
    if config is None or config.legacy:
        msg = f"No configured pool named {pool!r}"
        raise ValueError(msg)
    owners = owner_ids(providers)
    owner_id = owners.get(config.owner) if config.owner else None
    if _config_terms(config, providers, owner_id) is None:
        msg = f"Pool {pool!r} sets no owner budget and expiry in providers.yaml"
        raise ValueError(msg)
    key = config_envelope_key(pool, owner_id)
    now = timezone.now()
    with transaction.atomic():
        lock(CONFIG_ENVELOPES_LOCK)
        existing = SpendEnvelope.objects.filter(key=key).first()
        if existing is not None and not (fund or restore):
            msg = f"Pool {pool!r} already has its spend authority"
            raise ValueError(msg)
        if existing is None and restore:
            msg = f"Pool {pool!r} has no spend authority to restore"
            raise ValueError(msg)
        if existing is None and not (fund or envelope_id):
            msg = "Give the envelope to continue, or fund=true"
            raise ValueError(msg)
        if existing is not None:
            lock(envelope_lock_key(existing.pk))
            fields = ["linked_by", "linked_at", "link_note"]
            if fund:  # approve the accounts it spends on now
                existing.accounts = sorted(
                    set(existing.accounts or ()) | set(pool_accounts(providers, config))
                )
                fields.append("accounts")
            if restore:  # lift a person's revocation
                existing.restored_at = now
                fields.append("restored_at")
            existing.linked_by_id = by_id
            existing.linked_at = now
            existing.link_note = (
                note or ("restored" if restore else "approved") + " by an admin"
            )[:200]
            existing.save(update_fields=fields)
            envelope = existing
        elif fund:
            envelope = SpendEnvelope.objects.create(
                key=key,
                pool=pool,
                source=CONFIG_SOURCE,
                owner_id=owner_id,
                grantee_id=owner_id,
                accounts=pool_accounts(providers, config),
                linked_by_id=by_id,
                linked_at=now,
                link_note=(note or "funded separately by an admin")[:200],
            )
        else:
            if SpendEnvelope.objects.filter(pk=envelope_id).exists():
                lock(envelope_lock_key(envelope_id))
            envelope = (
                SpendEnvelope.objects.select_for_update()
                .filter(pk=envelope_id, source=CONFIG_SOURCE)
                .first()
            )
            if envelope is None:
                msg = "No such config envelope"
                raise ValueError(msg)
            if envelope.owner_id != owner_id:
                msg = "That envelope belongs to another owner"
                raise ValueError(msg)
            if envelope.key in _claimed_keys(providers, owners):
                msg = f"Pool {envelope.pool!r} still uses that envelope"
                raise ValueError(msg)
            envelope.key = key
            envelope.link_note = (
                note or f"continued from pool {envelope.pool!r} by an admin"
            )[:200]
            _carry_pool_switch(envelope.pool, pool)
            envelope.pool = pool
            # The admin's decision covers the accounts the pool spends on.
            envelope.accounts = sorted(
                set(envelope.accounts or ()) | set(pool_accounts(providers, config))
            )
            envelope.linked_by_id = by_id
            envelope.linked_at = now
            envelope.save(
                update_fields=[
                    "key",
                    "pool",
                    "accounts",
                    "link_note",
                    "linked_by",
                    "linked_at",
                ]
            )
    sync_authority(providers, owners)
    return envelope


def live_providers(source) -> Providers:
    """The providers.yaml in effect now for ``source``: a ``Providers`` as
    given, or a bot's registry as most recently reloaded in this process."""
    if hasattr(source, "pools") and hasattr(source, "providers"):
        return source
    registry = getattr(source, "registry", None)
    latest = getattr(registry, "latest", None)
    if callable(latest):
        registry = latest()
    return registry.providers if registry is not None else source.providers


class SendAuthority:
    """``spend_guard.CurrentAuthority`` for a guard or launch built from a
    bot (or a ``Providers``): at each send, under the envelope's lock, it
    re-reads providers.yaml as it is now (the file itself, when this
    process's copy is older; refusing when it doesn't load), settles the
    envelope (revoking it if the config no longer backs it) and returns the
    current revision's terms, narrowed to the routes and output ceiling the
    pool has now. A grant issued by the pool's owner is also narrowed to
    the owner's current authority, whose budget caps the family's spend."""

    def __init__(self, source):
        self.source = source

    def refresh(self) -> None:
        """Let this process's reloading registry pick up changed bot files
        (outside the lock: a reload reads files)."""
        from django_ergo.bots import webhooks

        webhooks.refresh_registry()

    def __call__(self, grant):  # noqa: PLR0911, C901
        from django_ergo.conversation.spend_guard import Terms

        try:
            providers = fresh_providers(live_providers(self.source))
        except ConfigUnavailable as exc:
            return f"{exc}; metered sends are refused until it loads"
        if providers.pools.legacy:
            return "providers.yaml no longer configures pools"
        owners = owner_ids(providers)
        now = timezone.now()
        envelope = grant.envelope
        current, problem = _settle(envelope, providers, owners, now)
        if current is None:
            return problem
        # The envelope's pool now (a renamed pool's envelope follows it).
        name = envelope.pool
        pool = providers.pools.get(name)
        if pool is None:
            return f"pool {name!r} was removed from providers.yaml"
        if not pool_enabled_now(pool):
            return f"pool {name!r} is switched off"
        terms = Terms.of(current)
        if envelope.source == CONFIG_SOURCE:
            root = envelope
        else:
            root = envelope.parent
            if (
                root is None
                or root.source != CONFIG_SOURCE
                or root.owner_id != envelope.owner_id
                or root.pool != name
            ):
                return (
                    f"the owner of {name!r} has no spend authority of "
                    "their own there to share"
                )
            owners_current, problem = _settle(root, providers, owners, now)
            if owners_current is None:
                return f"the owner's spend authority on {name!r}: {problem}"
            terms = terms.narrowed(Terms.of(owners_current))
            root_budget = owners_current.budget_usd
        if envelope.source == CONFIG_SOURCE:
            root_budget = current.budget_usd
        off = _switched_off_accounts()
        on = set()
        prices = {}
        for ref in pool_refs(providers, pool, metered=True):
            head, _, model = ref.partition("/")
            provider = providers.providers[head]
            if not provider.enabled or off & set(provider_accounts(provider)):
                continue  # a provider switched off at runtime, renamed or not
            on.add(ref)
            found = provider.models.get(model)
            prices[ref] = found.price if found is not None else None
        return replace(
            terms,
            routes=terms.routes & on,
            max_output_tokens=min(terms.max_output_tokens, pool.max_output_tokens),
            cap_envelope=root.pk,
            cap_usd=root_budget,
            prices=prices,
        )


def pool_enabled_now(pool: Pool) -> bool:
    """The pool's switch as selection sees it: the runtime state when set,
    else providers.yaml's ``enabled``. Runs the ORM."""
    from django_ergo.conversation.models import ModelPoolState

    state = ModelPoolState.objects.filter(name=pool.name).first()
    if state is not None and state.enabled is not None:
        return state.enabled
    return pool.enabled


def active_grant(pool: Pool, providers: Providers, user_id, owner_id=None):
    """This user's current revision on ``pool`` from its current owner (even
    if expired or not yet valid: :meth:`PoolView.grant_problem` says why it
    can't be used), or None. With a job policy, only the revision it
    captured (sends intersect it with the current one). Runs the ORM; call
    :func:`sync_authority` first."""
    from django_ergo.conversation.models import SpendGrant

    if not user_id or pool.legacy or owner_id is None:
        return None
    policy = current_policy()
    if policy is not None:
        # Queued work keeps exactly the authority it was queued with.
        captured = policy.get("grant") or ""
        if policy.get("pool") != pool.name or not captured:
            return None
        return SpendGrant.objects.filter(
            pk=captured, grantee_id=user_id, owner_id=owner_id
        ).first()
    return (
        SpendGrant.objects.filter(
            pool=pool.name,
            grantee_id=user_id,
            owner_id=owner_id,
            revoked_at__isnull=True,
            superseded_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )


@dataclass
class PoolView:
    """A pool with its runtime state, and this user's authority, merged in."""

    pool: Pool
    position: int
    enabled: bool
    rollover_out: bool
    allow_auto_entry: bool
    owner_id: int | None = None
    is_owner: bool = False
    grant: object = None  # SpendGrant for this user, or None
    spent_usd: Decimal = Decimal(0)
    reserved_usd: Decimal = Decimal(0)
    version: int = 0
    # Why the owner's authority behind this user's grant (or the owner's
    # own config authority) can't be used, as (code, reason), or None.
    owner_problem: tuple[str, str] | None = None
    # The owner's budget and the owner's plus every grantee's spend under it.
    cap_usd: Decimal | None = None
    family_used_usd: Decimal = Decimal(0)

    @property
    def name(self) -> str:
        return self.pool.name

    @property
    def title(self) -> str:
        return self.pool.title

    @property
    def budget_usd(self) -> Decimal | None:
        return self.grant.budget_usd if self.grant is not None else None

    @property
    def expires_at(self) -> dt.datetime | None:
        return self.grant.expires_at if self.grant is not None else None

    @property
    def per_launch_usd(self) -> Decimal | None:
        return self.grant.per_launch_usd if self.grant is not None else None

    @property
    def max_output_tokens(self) -> int:
        ceiling = self.pool.max_output_tokens
        if self.grant is not None:
            ceiling = min(ceiling, int(self.grant.max_output_tokens))
        return ceiling

    @property
    def subscription_only(self) -> bool:
        return not self.pool.metered

    def grant_problem(self, now=None) -> tuple[str, str] | None:  # noqa: PLR0911
        """Why this user can't spend on the pool's metered routes, or None."""
        if self.pool.legacy or not self.pool.metered:
            return None
        grant = self.grant
        if grant is None and self.owner_problem is not None:
            return self.owner_problem
        if grant is None:
            return (
                BUDGET_REQUIRED,
                f"You have no spend grant for pool {self.title!r}; its owner "
                "can issue one (with a budget and an end date)",
            )
        now = now or timezone.now()
        if grant.revoked_at is not None:
            return BUDGET_REQUIRED, f"Your spend grant for {self.title!r} was revoked"
        if now < grant.valid_from:
            return (
                GRANT_NOT_YET_VALID,
                f"Your spend grant for {self.title!r} starts "
                f"{grant.valid_from:%Y-%m-%d %H:%M} UTC",
            )
        if now >= grant.expires_at:
            return (
                BUDGET_EXPIRED,
                f"Your spend grant for {self.title!r} expired "
                f"{grant.expires_at:%Y-%m-%d %H:%M} UTC",
            )
        if self.spent_usd + self.reserved_usd >= grant.budget_usd:
            return (
                BUDGET_EXHAUSTED,
                f"Your ${grant.budget_usd} grant for {self.title!r} is used "
                f"(${self.spent_usd:.2f} spent, ${self.reserved_usd:.2f} reserved)",
            )
        if self.owner_problem is not None:
            return self.owner_problem
        if self.cap_usd is not None and self.family_used_usd >= self.cap_usd:
            return (
                BUDGET_EXHAUSTED,
                f"The owner's ${self.cap_usd} budget for {self.title!r} is used "
                f"(${self.family_used_usd:.2f} by the owner and their grants)",
            )
        return None

    def route_granted(self, ref: str) -> bool:
        """Whether this user may use ``ref`` in the pool at all: the owner and
        anyone on a deployment pool may use every route; a grantee only the
        routes of their grant."""
        if self.pool.legacy or not self.pool.owner or self.is_owner:
            return True
        return self.grant is not None and ref in (self.grant.routes or [])

    def problem(self, now=None) -> tuple[str, str] | None:
        """Why the pool can't be used at all (not exhaustion), or None. A pool
        of only metered routes is unusable without a valid grant; a mixed
        pool's subscription routes stay usable (``route_problem``)."""
        if not self.enabled:
            return POOL_DISABLED, f"Pool {self.title!r} is switched off"
        if self.pool.legacy or not self.pool.metered:
            return None
        if self.pool.owner and not self.is_owner and self.grant is None:
            return NOT_AUTHORIZED, f"Pool {self.title!r} isn't yours"
        if self._all_metered:
            return self.grant_problem(now)
        return None

    _all_metered: bool = False


@dataclass
class Snapshot:
    providers: Providers
    pools: list[PoolView]
    auto_rollover: bool
    active_pool: str = ""  # manual pick ("" = automatic)
    active_scope: str = ""  # chat, user, job or ""
    disabled: set[str] = field(default_factory=set)
    user_version: int = 0
    chat_version: int = 0
    rolled_from: str = ""
    user_id: object = None
    explicit_order: bool = False

    @property
    def legacy(self) -> bool:
        return self.providers.pools.legacy

    def get(self, name: str) -> PoolView | None:
        return next((p for p in self.pools if p.name == name), None)

    def revision(self, view: PoolView | None = None) -> dict:
        out = {"preference": self.user_version, "chat": self.chat_version}
        if view is not None:
            out.update(
                pool=view.name,
                pool_state=view.version,
                grant=str(view.grant.pk) if view.grant is not None else "",
            )
        if (policy := current_policy()) is not None:
            out["job"] = policy.get("revision") or {}
        return out


def _visible(pool: Pool, user_id, owner_id, grant) -> bool:
    """Deployment pools are everyone's; an owned pool is its owner's and its
    grantees'."""
    if pool.legacy or not pool.owner:
        return True
    if owner_id is not None and user_id == owner_id:
        return True
    return grant is not None


def _views(providers: Providers, user_id=None) -> list[PoolView]:
    from django.db import DatabaseError

    from django_ergo.conversation.models import ModelPoolState
    from django_ergo.conversation.spend_guard import spent_and_reserved

    owners = owner_ids(providers) if not providers.pools.legacy else {}
    sync_authority(providers, owners)
    try:  # after the sync: a renamed pool's switch moves with its envelope
        states = {s.name: s for s in ModelPoolState.objects.all()}
    except DatabaseError:  # pragma: no cover
        states = {}
    views = []
    for index, pool in enumerate(providers.pools.pools):
        owner_id = owners.get(pool.owner) if pool.owner else None
        grant = (
            active_grant(pool, providers, user_id, owner_id)
            if pool.metered and pool.owner
            else None
        )
        if not _visible(pool, user_id, owner_id, grant):
            continue
        state = states.get(pool.name)
        view = PoolView(
            pool=pool,
            position=index,
            enabled=pool.enabled,
            rollover_out=pool.rollover_out,
            allow_auto_entry=pool.allow_auto_entry,
            owner_id=owner_id,
            is_owner=owner_id is not None and owner_id == user_id,
            grant=grant,
        )
        view._all_metered = not pool_refs(providers, pool, metered=False)  # noqa: SLF001
        if state is not None and not pool.legacy:
            view.version = state.version
            for name in ("enabled", "rollover_out", "allow_auto_entry"):
                if getattr(state, name) is not None:
                    setattr(view, name, getattr(state, name))
        if grant is not None:
            view.spent_usd, view.reserved_usd = spent_and_reserved(grant.pk)
            _owner_authority(view, grant)
        elif view.is_owner and pool.metered:
            if problem := config_link_problem(pool, providers, owners):
                view.owner_problem = (BUDGET_REQUIRED, problem)
        views.append(view)
    return views


def _owner_authority(view: PoolView, grant) -> None:
    """Fill ``view`` with the owner's authority behind ``grant``: its
    budget, the family's use of it, or why it can't be used. Runs the ORM."""
    from django_ergo.conversation.spend_guard import family_spent_and_reserved

    envelope = grant.envelope
    if envelope.source == CONFIG_SOURCE:
        root, owners = envelope, grant
    else:
        root = envelope.parent
        owners = _current_revision(root.pk) if root is not None else None
        if owners is None or root.owner_id != envelope.owner_id:
            view.owner_problem = (
                BUDGET_REQUIRED,
                f"The owner of {view.title!r} has no spend authority of their "
                "own there now, so your grant can't be used",
            )
            return
        if timezone.now() >= owners.expires_at:
            view.owner_problem = (
                BUDGET_EXPIRED,
                f"The owner's spend authority on {view.title!r} expired "
                f"{owners.expires_at:%Y-%m-%d %H:%M} UTC",
            )
            return
    spent, held = family_spent_and_reserved(root.pk)
    view.cap_usd = owners.budget_usd
    view.family_used_usd = spent + held


def preference(user_id):
    from django_ergo.conversation.models import ModelPoolPreference

    return ModelPoolPreference.objects.filter(key=preference_key(user_id)).first()


def chat_revision(session) -> int:
    value = (getattr(session, "metadata", None) or {}).get(SESSION_VERSION_KEY, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def snapshot(providers: Providers, user_id=None, session=None) -> Snapshot:
    """Config plus runtime state for one user (and chat). Runs the ORM.

    Only pools this user may see are in it (deployment pools, pools they
    own, pools they hold a grant on). Under a queued job's policy the active
    pool is the one it was queued with."""
    views = _views(providers, user_id)
    pref = None if providers.pools.legacy else preference(user_id)
    if pref is not None and pref.order:
        rank = {name: i for i, name in enumerate(pref.order)}
        views.sort(key=lambda v: (rank.get(v.name, len(rank) + v.position)))
    auto_rollover = providers.pools.auto_rollover
    if pref is not None and pref.auto_rollover is not None:
        auto_rollover = pref.auto_rollover
    active, scope, rolled = "", "", ""
    meta = getattr(session, "metadata", None) or {}
    chat_pool = meta.get(SESSION_POOL_KEY) or ""
    policy = current_policy()
    if not providers.pools.legacy:
        if policy is not None:
            active, scope = str(policy.get("pool") or ""), "job"
            rolled = str(policy.get("rolled_from") or "")
        elif chat_pool:
            active, scope = str(chat_pool), "chat"
            rolled = str(meta.get(SESSION_ROLLED_KEY) or "")
        elif pref is not None and pref.active_pool:
            active, scope = pref.active_pool, "user"
            rolled = pref.rolled_from
    return Snapshot(
        providers=providers,
        pools=views,
        # Queued work never rolls into authority it wasn't queued with.
        auto_rollover=bool(auto_rollover)
        and not providers.pools.legacy
        and policy is None,
        active_pool=active,
        active_scope=scope,
        disabled=disabled_providers(),
        user_version=pref.version if pref is not None else 0,
        chat_version=chat_revision(session),
        rolled_from=rolled,
        user_id=user_id,
        explicit_order=bool(pref and pref.order),
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


def route_problem(  # noqa: C901, PLR0911, PLR0912, PLR0913
    snap: Snapshot,
    privacy: Privacy,
    consumer: str,
    provider_name: str,
    model: str,
    view: PoolView | None = None,
) -> tuple[str, str] | None:
    """Why a route can't serve ``consumer`` now (never pace), or None. With
    ``view`` (the pool it is selected in) the user's authority on it counts
    too: a grantee only uses their grant's routes, a metered route needs a
    valid grant."""
    providers = snap.providers
    ref = f"{provider_name}/{model}"
    provider = providers.providers.get(provider_name)
    if provider is None:
        return NO_ROUTE, f"{provider_name} isn't in providers.yaml"
    if view is not None and not view.route_granted(
        ref if not provider.worker_only else f"{provider_name}/*"
    ):
        return NOT_AUTHORIZED, f"{ref} isn't in your grant for pool {view.title!r}"
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
    if provider.metered and not snap.legacy and view is not None:
        if problem := view.grant_problem():
            return problem
        if ref not in (view.grant.routes or []):
            return NOT_AUTHORIZED, f"{ref} isn't one of your grant's routes"
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
    # For metered routes: the grant (spend authority) and its envelope as
    # selected; the guard re-reads the grant row at every send.
    grant_id: object = None
    budget_usd: Decimal | None = None
    expires_at: dt.datetime | None = None
    max_output_tokens: int = 16_000
    per_launch_usd: Decimal | None = None
    scope: str = ""  # where the active pool came from: chat, user, job or ""
    revision: dict = field(default_factory=dict)

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
            "grant": str(self.grant_id) if self.grant_id else "",
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
        user_id = getattr(session, "user_id", None)
        if self.grant_id is None or not user_id:
            return deny_guard(
                self.provider,
                self.model,
                f"Pool {self.pool!r}: {self.route} has no spend grant for this "
                "user; refusing to send",
            )
        return MeteredGuard(
            pool=self.pool,
            provider=self.provider,
            model=self.model,
            grant_id=self.grant_id,
            current=SendAuthority(bot),
            price=found.price if found else None,
            max_output_tokens=self.max_output_tokens,
            bot=bot.name,
            user_id=user_id,
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
    revision: dict | None = None,
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
            grant=decision.grant_id if decision else None,
            revision=(decision.revision if decision else revision) or {},
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


def _can_spend(snap: Snapshot, view: PoolView) -> bool:
    """Whether automatic entry to ``view`` could spend money: it holds a
    metered route an engine can send and bound (or a metered coding worker).
    A metered route no engine can guard is refused per route (``UNGUARDED``),
    so it doesn't make the pool an entry boundary."""
    if snap.legacy or not view.pool.metered:
        return False
    for selector in view.pool.routes:
        provider = snap.providers.providers.get(selector.partition("/")[0])
        if (
            provider is not None
            and provider.metered
            and (provider.worker_only or spend_enforced(provider))
        ):
            return True
    return False


def _start(snap: Snapshot, privacy: Privacy, consumer: str) -> tuple[int, str]:
    """(index of the active pool, mode). Automatic selection starts at the
    first pool in the user's order; reaching any later pool automatically
    (because earlier ones are off or not allowed) needs that pool's
    ``allow_auto_entry``, so an opt-in boundary stops selection instead of
    being skipped. A first pool that can spend money needs it too, unless
    the user saved their own order (an explicit choice to start there)."""
    if snap.active_scope == "job" and not snap.active_pool:
        msg = "This queued work had no usable pool when it was queued; it can't pick one now"
        raise PoolRefusal(POOL_DISABLED, msg, consumer=consumer)
    if snap.active_pool:
        for index, view in enumerate(snap.pools):
            if view.name == snap.active_pool:
                return index, "manual"
        msg = f"The selected pool {snap.active_pool!r} isn't available to you any more; pick another"
        raise PoolRefusal(
            NOT_AUTHORIZED
            if snap.providers.pools.get(snap.active_pool) is not None
            else POOL_DISABLED,
            msg,
            pool=snap.active_pool,
            next_choices=next_choices(snap, privacy, snap.active_pool),
            consumer=consumer,
        )
    skipped: list[str] = []
    for index, view in enumerate(snap.pools):
        usable = view.enabled and not (
            privacy.restricted and not _pool_permitted(snap, view, privacy)
        )
        if (
            usable
            and not view.allow_auto_entry
            and (skipped or (not snap.explicit_order and _can_spend(snap, view)))
        ):
            msg = (
                f"{view.title} needs to be "
                "picked by hand; not entering it automatically"
            )
            raise PoolRefusal(
                POOL_DISABLED,
                msg,
                pool=view.name,
                next_choices=next_choices(snap, privacy, ""),
                consumer=consumer,
            )
        if usable:
            return index, "auto"
        skipped.append(view.title)
    msg = "No pool is switched on" + (
        f" that privacy policy {privacy.label()} allows" if privacy.restricted else ""
    )
    raise PoolRefusal(
        NO_ROUTE, msg, next_choices=next_choices(snap, privacy, ""), consumer=consumer
    )


def _next_index(snap: Snapshot, index: int, privacy: Privacy) -> int | None:
    """The next pool in order (a switched-off one included: it is a boundary
    that stops a rollover, not one to skip)."""
    for later in range(index + 1, len(snap.pools)):
        view = snap.pools[later]
        if privacy.restricted and not _pool_permitted(snap, view, privacy):
            continue
        return later
    return None


def _walk(snap: Snapshot, privacy: Privacy, consumer: str, attempt):  # noqa: C901
    """Try the active pool, rolling over on exhaustion only when all three
    gates permit. Returns the decision."""
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
        decision.scope = snap.active_scope
        decision.revision = snap.revision(view)
        if decision.metered and not decision.legacy:
            grant = view.grant
            decision.grant_id = grant.pk if grant is not None else None
            decision.budget_usd = view.budget_usd
            decision.expires_at = view.expires_at
            decision.max_output_tokens = view.max_output_tokens
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
            if problem := route_problem(
                snap, privacy, consumer, provider_name, model, view
            ):
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
            if problem := route_problem(
                snap, privacy, consumer, provider_name, model, view
            ):
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
    if record and decision.rollover:
        _persist_rollover(snap, decision, session, user_id)
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


def _persist_rollover(snap: Snapshot, decision: Decision, session, user_id) -> None:
    """Keep work on the pool a rollover reached, in the scope that was in
    effect (this chat's override, else the user's default), with the pool it
    left recorded: it stays there until the person switches back or picks
    "use preferred order". Never an invisible return to an earlier pool."""
    from django.db import transaction

    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ModelPoolPreference

    target, left = decision.pool, decision.rollover[0]
    if snap.active_scope == "job":
        return
    with transaction.atomic():
        if snap.active_scope == "chat" and getattr(session, "pk", None):
            row = ConversationSession.objects.select_for_update().get(pk=session.pk)
            meta = dict(row.metadata or {})
            if chat_revision(row) != snap.chat_version:
                return  # changed since this selection; that choice wins
            meta[SESSION_POOL_KEY] = target
            meta[SESSION_ROLLED_KEY] = left
            meta[SESSION_VERSION_KEY] = snap.chat_version + 1
            row.metadata = meta
            row.save(update_fields=["metadata", "updated_at"])
            session.metadata = {
                **(session.metadata or {}),
                SESSION_POOL_KEY: target,
                SESSION_ROLLED_KEY: left,
                SESSION_VERSION_KEY: snap.chat_version + 1,
            }
            return
        pref, _ = ModelPoolPreference.objects.select_for_update().get_or_create(
            key=preference_key(user_id), defaults={"user_id": user_id, "version": 0}
        )
        if pref.version != snap.user_version:
            return
        pref.active_pool = target
        pref.rolled_from = left
        pref.version += 1
        pref.save()
    decision.scope = decision.scope or "user"


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
                snap, privacy, CODING, provider_name, route_model, view
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
            grant_id=decision.grant_id,
            pool=decision.pool,
            provider=decision.provider,
            model=decision.model,
            amount=decision.per_launch_usd,
            bot=bot.name,
            session_id=getattr(session, "pk", None),
            user_id=user_id,
            current=SendAuthority(bot),
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
    if problem := route_problem(snap, privacy, CODING, provider, model, view):
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
    decision.grant_id = view.grant.pk if view.grant is not None else None
    decision.reservation_id = reserve_launch(
        grant_id=decision.grant_id,
        pool=view.name,
        provider=provider,
        model=model,
        amount=view.per_launch_usd,
        bot=bot.name,
        session_id=getattr(session, "pk", None),
        user_id=getattr(session, "user_id", None),
        current=SendAuthority(bot),
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


def authorize_subscription_call(
    bot: Bot, session, *, provider_type: str, consumer: str
) -> str:
    """For an ancillary call that runs on a subscription CLI outside a chat
    engine (``web_search``'s ``claude_code`` backend): the provider in the
    chat's active pool it may use, or PoolRefusal. "" without pools. Records
    a receipt. Runs the ORM."""
    providers = bot.providers
    if providers.pools.legacy:
        return ""
    privacy = privacy_for(bot, session)
    user_id = getattr(session, "user_id", None)
    if privacy.restricted:
        msg = f"Privacy policy {privacy.label()} doesn't allow {consumer} on a model"
        raise PoolRefusal(PRIVACY_DENIED, msg, consumer=consumer)
    snap = snapshot(providers, user_id, session)
    index, mode = _start(snap, privacy, consumer)
    view = snap.pools[index]
    if problem := view.problem():
        raise PoolRefusal(problem[0], problem[1], pool=view.name, consumer=consumer)
    rejected = []
    for ref in pool_refs(providers, view.pool, metered=False):
        head, _, model = ref.partition("/")
        provider = providers.providers[head]
        if provider.type != provider_type or provider.transport != "cli":
            continue
        if problem := route_problem(snap, privacy, consumer, head, model, view):
            rejected.append({"route": ref, "code": problem[0], "reason": problem[1]})
            continue
        decision = Decision(
            consumer=consumer,
            pool=view.name,
            pool_label=view.title,
            route=ref,
            provider=head,
            model=model,
            billing=provider.billing,
            mode=mode,
            rejected=rejected,
            scope=snap.active_scope,
            revision=snap.revision(view),
            reason=f"{consumer} on the active pool's subscription",
        )
        decision.receipt_id = _record(
            bot=bot,
            session=session,
            user_id=user_id,
            consumer=consumer,
            requested=provider_type,
            decision=decision,
        )
        return head
    msg = (
        f"The active pool {view.title!r} has no usable {provider_type} subscription "
        f"for {consumer}"
    )
    raise PoolRefusal(
        NO_ROUTE, msg, pool=view.name, rejected=rejected, consumer=consumer
    )


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


def _grant_view(grant, spent=None) -> dict | None:
    if grant is None:
        return None
    from django_ergo.conversation.spend_guard import spent_and_reserved

    spent_usd, reserved_usd = (
        spent if spent is not None else spent_and_reserved(grant.pk)
    )
    return {
        "id": str(grant.pk),
        "grantee": grant.grantee_id,
        "routes": list(grant.routes or []),
        "budget_usd": str(grant.budget_usd),
        "per_launch_usd": str(grant.per_launch_usd)
        if grant.per_launch_usd is not None
        else None,
        "max_output_tokens": grant.max_output_tokens,
        "valid_from": grant.valid_from.isoformat(),
        "expires_at": grant.expires_at.isoformat(),
        "source": grant.source,
        "revoked": grant.revoked_at is not None,
        "spent_usd": str(spent_usd),
        "reserved_usd": str(reserved_usd),
    }


def issued_grants(view: PoolView) -> list[dict]:
    """The grants on a pool its owner has issued (owner only). Runs the ORM."""
    from django.db.models import Q

    from django_ergo.conversation.models import SpendGrant

    if not view.is_owner:
        return []
    family = Q(pool=view.name) | Q(
        envelope__parent__pool=view.name,
        envelope__parent__source=CONFIG_SOURCE,
        envelope__parent__owner_id=view.owner_id,
    )
    rows = SpendGrant.objects.filter(
        family, revoked_at__isnull=True, superseded_at__isnull=True
    )
    return [_grant_view(g) for g in rows.order_by("-created_at")[:50]]


def public_view(providers: Providers, privacy: Privacy, snap: Snapshot) -> dict:
    """Pools, routes and tiers with what is *selectable now* (the same full
    eligibility selection uses: pool switch and authority, grant, provider,
    price, adapter, and current capacity) as well as what is configured.
    Spend figures are only this user's own grant (and, for a pool's owner,
    the grants they issued); never another user's totals."""
    rows = []
    usage = routing.current_usage()
    for view in snap.pools:
        problem = view.problem()
        models = []
        selectable_refs = set()
        for selector in view.pool.routes:
            head, _, tail = selector.partition("/")
            provider = providers.providers.get(head)
            if provider is None or provider.worker_only:
                continue
            names = [tail] if tail not in ("", "*") else list(provider.models)
            for name in names:
                ref = f"{head}/{name}"
                issue = problem or route_problem(snap, privacy, CHAT, head, name, view)
                status = issue[0] if issue else "selectable"
                why = issue[1] if issue else ""
                if issue is None and (blocked := routing.pace_block(head, usage, name)):
                    status, why = "no_capacity", blocked
                if status == "selectable":
                    selectable_refs.add(ref)
                model = provider.models.get(name)
                models.append(
                    {
                        "id": ref,
                        "label": (model.label if model else "") or name,
                        "billing": provider.billing,
                        "configured": True,
                        "available": status == "selectable",
                        "status": status,
                        "why": why,
                    }
                )
        tiers = {}
        tier_available = {}
        token = _ROUTING_HOLDER.set(providers.routing)
        try:
            for tier in sorted({*providers.routing.tiers, *view.pool.tiers}):
                refs = tier_candidates(providers, view.pool, tier)
                if refs:
                    tiers[tier] = refs
                    tier_available[tier] = any(r in selectable_refs for r in refs)
        finally:
            _ROUTING_HOLDER.reset(token)
        grant = _grant_view(view.grant, (view.spent_usd, view.reserved_usd))
        rows.append(
            {
                "name": view.name,
                "label": view.title,
                "position": view.position,
                "owner": view.pool.owner,
                "is_owner": view.is_owner,
                "enabled": view.enabled,
                "status": problem[0] if problem else "eligible",
                "why": problem[1] if problem else "",
                "selectable_now": bool(selectable_refs),
                "metered": view.pool.metered,
                "legacy": view.pool.legacy,
                "rollover_out": view.rollover_out,
                "allow_auto_entry": view.allow_auto_entry,
                "grant": grant,
                "grants": [
                    g for g in issued_grants(view) if g["id"] != (grant or {}).get("id")
                ],
                "budget_usd": grant["budget_usd"] if grant else None,
                "spent_usd": grant["spent_usd"] if grant else None,
                "reserved_usd": grant["reserved_usd"] if grant else None,
                "expires_at": grant["expires_at"] if grant else None,
                "per_launch_usd": grant["per_launch_usd"] if grant else None,
                "max_output_tokens": view.max_output_tokens,
                "version": view.version,
                "routes": list(view.pool.routes),
                "models": models,
                "tiers": tiers,
                "tier_available": tier_available,
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
        "rolled_from": snap.rolled_from,
        "auto_rollover": snap.auto_rollover,
        "user_version": snap.user_version,
        "chat_version": snap.chat_version,
        "providers": [
            {
                "name": p.name,
                "type": p.type,
                "transport": p.transport,
                "billing": p.billing,
                "enabled": provider_enabled(p, snap.disabled),
                "configured_enabled": p.enabled,
                "available": p.available,
                "version": toggle_versions.get(p.name, 0),
            }
            for toggle_versions in [_toggle_versions()]
            for p in providers.providers.values()
        ],
        "reset_times": _reset_times(providers),
    }


def _toggle_versions() -> dict[str, int]:
    from django_ergo.conversation.models import ProviderToggle

    return dict(ProviderToggle.objects.values_list("name", "version"))


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


class VersionConflict(ValueError):  # noqa: N818
    pass


def _require_version(version, what: str) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        msg = f"{what}: the version you last saw is required (reload and try again)"
        raise ValueError(msg)
    return version


def selectable_pool(providers: Providers, user_id, pool: str) -> None:
    """ValueError unless ``user_id`` may pick ``pool`` (a deployment pool, one
    they own, or one they hold a grant on). Runs the ORM."""
    if not pool:
        return
    if providers.pools.legacy:
        msg = "Pools aren't configured (providers.yaml has no pools section)"
        raise ValueError(msg)
    if providers.pools.get(pool) is None:
        msg = f"No pool named {pool!r}"
        raise ValueError(msg)
    if not any(v.name == pool for v in _views(providers, user_id)):
        msg = f"Pool {pool!r} isn't yours and you hold no grant on it"
        raise PermissionError(msg)


def set_chat_pool(session, pool: str, providers: Providers, *, version: int) -> None:
    """Pin a chat to ``pool`` ("" = follow the user's default), if the chat
    override is still at ``version``. Runs the ORM."""
    switch_pool(
        session.user_id,
        providers,
        pool,
        scope="chat",
        session=session,
        chat_version=version,
    )


def switch_pool(  # noqa: PLR0913
    user_id,
    providers: Providers,
    pool: str,
    *,
    scope: str,
    session=None,
    version: int | None = None,
    chat_version: int | None = None,
) -> None:
    """Set the active pool for new work in one scope, atomically.

    ``scope="chat"``: this chat's override (``chat_version`` required).
    ``scope="user"``: the user's default (``version`` required) AND, given a
    ``session``, that chat's override cleared (``chat_version`` required),
    in one transaction: both change or neither does. Clearing (``pool=""``)
    returns to the preferred order. VersionConflict on a stale version,
    PermissionError for a pool the user may not use. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ModelPoolPreference

    if scope not in ("chat", "user"):
        msg = "scope must be chat or user"
        raise ValueError(msg)
    selectable_pool(providers, user_id, pool)
    if scope == "chat" or session is not None:
        _require_version(chat_version, "Changing this chat's pool")
        if session is None or getattr(session, "user_id", None) != user_id:
            msg = "That chat isn't yours"
            raise PermissionError(msg)
    if scope == "user":
        _require_version(version, "Changing your pool")
    with transaction.atomic():
        if scope == "user":
            pref, _ = ModelPoolPreference.objects.select_for_update().get_or_create(
                key=preference_key(user_id), defaults={"user_id": user_id, "version": 0}
            )
            if pref.version != version:
                msg = f"Pool settings changed elsewhere (version {pref.version}, not {version}); reload"
                raise VersionConflict(msg)
            pref.active_pool = pool
            pref.rolled_from = ""
            pref.version += 1
            pref.save()
        if session is not None:
            row = ConversationSession.objects.select_for_update().get(pk=session.pk)
            if chat_revision(row) != chat_version:
                msg = f"This chat's pool changed elsewhere (version {chat_revision(row)}); reload"
                raise VersionConflict(msg)
            meta = dict(row.metadata or {})
            if scope == "chat" and pool:
                meta[SESSION_POOL_KEY] = pool
            else:
                meta.pop(SESSION_POOL_KEY, None)
            meta.pop(SESSION_ROLLED_KEY, None)
            meta.pop(SESSION_REFUSAL_KEY, None)
            meta.pop("routed_model", None)  # pick again inside the new pool
            meta[SESSION_VERSION_KEY] = chat_revision(row) + 1
            row.metadata = meta
            row.save(update_fields=["metadata", "updated_at"])
            session.metadata = meta


def update_preference(  # noqa: PLR0913
    user_id,
    providers: Providers,
    *,
    version: int,
    active_pool: str | None = None,
    auto_rollover: bool | None = None,
    order: list[str] | None = None,
    clear_auto_rollover: bool = False,
):
    """Change a user's pool choices atomically; ``version`` (the one last
    read) is required. VersionConflict on a stale version, ValueError for an
    unknown pool, PermissionError for one they may not use. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import ModelPoolPreference

    _require_version(version, "Changing your pool settings")
    names = [p.name for p in providers.pools.pools]
    visible = [v.name for v in _views(providers, user_id)] if order is not None else []
    if active_pool:
        selectable_pool(providers, user_id, active_pool)
    if order is not None and sorted(order) != sorted(visible):
        msg = "order must list every visible pool exactly once"
        raise ValueError(msg)
    if auto_rollover is not None and not isinstance(auto_rollover, bool):
        msg = "auto_rollover must be true or false"
        raise ValueError(msg)
    with transaction.atomic():
        pref, _ = ModelPoolPreference.objects.select_for_update().get_or_create(
            key=preference_key(user_id), defaults={"user_id": user_id, "version": 0}
        )
        if pref.version != version:
            msg = f"Pool settings changed elsewhere (version {pref.version}, not {version}); reload"
            raise VersionConflict(msg)
        if active_pool is not None:
            pref.active_pool = active_pool
            pref.rolled_from = ""
        if auto_rollover is not None:
            pref.auto_rollover = auto_rollover
        if clear_auto_rollover:
            pref.auto_rollover = None
        if order is not None:
            # The visible pools take the new order in the slots they held; a
            # pool hidden from this user stays where it was (and one never in
            # their order isn't added: its name isn't theirs to see).
            kept = [n for n in pref.order if n in names]
            selected = iter(order)
            merged = [next(selected) if n in visible else n for n in kept]
            pref.order = merged + list(selected)
        pref.version += 1
        pref.save()
    return pref


def may_manage(
    providers: Providers, name: str, user_id, *, admin: bool = False
) -> bool:
    """Whether a user may change a pool's gates or issue grants on it: its
    owner, or an admin (a deployment pool: admins only). Runs the ORM."""
    pool = providers.pools.get(name)
    if pool is None or pool.legacy:
        return False
    if admin:
        return True
    if not pool.owner:
        return False
    return owner_ids(providers).get(pool.owner) == user_id and user_id is not None


def update_pool_state(name: str, providers: Providers, *, version: int, **changes):
    """Change one pool's gates (enabled, rollover_out, allow_auto_entry)
    atomically; ``version`` is required. Spend authority is a grant
    (:func:`issue_grant`), not a pool setting. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import ModelPoolState

    _require_version(version, f"Changing pool {name!r}")
    pool = providers.pools.get(name)
    if pool is None or pool.legacy:
        msg = f"No configured pool named {name!r}"
        raise ValueError(msg)
    allowed = {"enabled", "rollover_out", "allow_auto_entry"}
    if unknown := set(changes) - allowed:
        msg = (
            f"Unknown pool settings: {', '.join(sorted(unknown))} (budgets and "
            "expiry are spend grants)"
        )
        raise ValueError(msg)
    for key, value in changes.items():
        if value is not None and not isinstance(value, bool):
            msg = f"{key} must be true, false or null"
            raise ValueError(msg)
    with transaction.atomic():
        state, _ = ModelPoolState.objects.select_for_update().get_or_create(
            name=name, defaults={"version": 0}
        )
        if state.version != version:
            msg = f"Pool {name!r} changed elsewhere (version {state.version}, not {version}); reload"
            raise VersionConflict(msg)
        for key, value in changes.items():
            setattr(state, key, value)
        state.version += 1
        state.save()
    return state


def usd(value, what: str, *, required: bool = True) -> Decimal | None:
    """A finite, non-negative amount of US dollars (strings, ints, floats or
    Decimals), or ValueError."""
    from decimal import InvalidOperation

    if value is None or value == "":
        if required:
            msg = f"{what} is required"
            raise ValueError(msg)
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        msg = f"{what} must be a number of US dollars"
        raise ValueError(msg)  # noqa: TRY004
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        msg = f"{what} must be a number of US dollars"
        raise ValueError(msg) from exc
    if not amount.is_finite() or amount < 0:
        msg = f"{what} must be a finite amount, 0 or more"
        raise ValueError(msg)
    if amount >= Decimal("1e12"):
        msg = f"{what} is too large"
        raise ValueError(msg)
    return amount


def issue_grant(  # noqa: PLR0912, PLR0913, C901, PLR0915
    providers: Providers,
    pool: str,
    *,
    issuer_id,
    grantee_id,
    budget_usd,
    expires_at: dt.datetime,
    routes: list[str] | None = None,
    valid_from: dt.datetime | None = None,
    per_launch_usd=None,
    max_output_tokens: int | None = None,
    admin: bool = False,
):
    """Issue a spend grant on ``pool`` (its owner or an admin): ``grantee``
    may spend up to ``budget_usd`` on ``routes`` (default: all the pool's
    metered routes) between ``valid_from`` (default now) and ``expires_at``.
    It is drawn from the owner's own config authority on the pool (which
    must exist): every send is also held to the owner's current terms, and
    the owner's budget caps the owner's and all their grantees' spend
    together. It replaces (revokes) the grantee's previous grant on the
    pool; the old ledger stays. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import SpendEnvelope
    from django_ergo.conversation.models import SpendGrant

    try:
        providers = fresh_providers(providers)
    except ConfigUnavailable as exc:
        msg = f"Can't issue a grant now: {exc}"
        raise ValueError(msg) from exc
    config = providers.pools.get(pool)
    if config is None or config.legacy:
        msg = f"No configured pool named {pool!r}"
        raise ValueError(msg)
    if not config.owner:
        msg = f"Pool {pool!r} has no owner, so it can't carry spend grants"
        raise ValueError(msg)
    owner_id = owner_ids(providers).get(config.owner)
    if owner_id is None:
        msg = f"Pool {pool!r}'s owner {config.owner!r} isn't a user here"
        raise ValueError(msg)
    if not may_manage(providers, pool, issuer_id, admin=admin):
        msg = f"Only {config.owner} (or an admin) can issue grants on {pool!r}"
        raise PermissionError(msg)
    if not grantee_id:
        msg = "A grant needs a grantee"
        raise ValueError(msg)
    metered = pool_refs(providers, config, metered=True)
    allowed = pool_refs(providers, config)
    routes = list(dict.fromkeys(routes if routes is not None else metered))
    if not routes or not set(routes) <= set(allowed):
        msg = f"routes must be exact provider/model routes of {pool!r}: {', '.join(allowed)}"
        raise ValueError(msg)
    budget = usd(budget_usd, "budget_usd")
    per_launch = usd(per_launch_usd, "per_launch_usd", required=False)
    if per_launch is not None and per_launch > budget:
        msg = "per_launch_usd can't be more than budget_usd"
        raise ValueError(msg)
    ceiling = (
        config.max_output_tokens if max_output_tokens is None else max_output_tokens
    )
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, int)
        or not 0 < ceiling <= config.max_output_tokens
    ):
        msg = f"max_output_tokens must be 1 to {config.max_output_tokens}"
        raise ValueError(msg)
    now = timezone.now()
    start = valid_from or now
    if not isinstance(expires_at, dt.datetime) or not isinstance(start, dt.datetime):
        msg = "valid_from and expires_at must be date-times"
        raise ValueError(msg)  # noqa: TRY004
    try:
        bad = expires_at <= start or expires_at <= now
    except TypeError as exc:  # naive vs aware
        msg = "valid_from and expires_at must be timezone-aware date-times"
        raise ValueError(msg) from exc
    if bad:
        msg = "expires_at must be in the future and after valid_from"
        raise ValueError(msg)
    from django.db.models import Q

    sync_authority(providers)
    parent = SpendEnvelope.objects.filter(
        key=config_envelope_key(pool, owner_id), source=CONFIG_SOURCE
    ).first()
    if parent is None or _current_revision(parent.pk) is None:
        msg = (
            f"{config.owner} has no spend authority of their own on {pool!r} "
            "(budget_usd and expires in providers.yaml) to share"
        )
        raise ValueError(msg)
    with transaction.atomic():
        # Replaces the grantee's earlier issued grant, never the owner's own
        # config authority (every issued grant is drawn from it).
        # Also under the pool's earlier names (the same family): a rename
        # and rename back can't revive a grant this one replaced.
        for old in (
            SpendGrant.objects.select_for_update()
            .filter(Q(pool=pool) | Q(envelope__parent=parent))
            .filter(grantee_id=grantee_id, revoked_at__isnull=True)
            .exclude(source=CONFIG_SOURCE)
        ):
            old.revoked_at = now
            old.revoked_reason = "replaced by a new grant"
            old.save(update_fields=["revoked_at", "revoked_reason"])
        issued = SpendGrant(
            pool=pool,
            owner_id=owner_id,
            grantee_id=grantee_id,
            routes=routes,
            budget_usd=budget,
            per_launch_usd=per_launch,
            max_output_tokens=ceiling,
            valid_from=start,
            expires_at=expires_at,
            source="issued",
            created_by_id=issuer_id,
        )
        issued.envelope = SpendEnvelope.objects.create(
            key=f"issued:{issued.pk}",
            pool=pool,
            source="issued",
            owner_id=owner_id,
            grantee_id=grantee_id,
            parent=parent,
        )
        issued.save(force_insert=True)
        return issued


def revoke_grant(
    providers: Providers, grant_id, *, by_id, admin: bool = False, reason: str = ""
):
    """Revoke a grant (its pool's owner, an admin, or the grantee giving it
    up). Its ledger stays; reservations already held stay held. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import SpendGrant

    with transaction.atomic():
        grant = SpendGrant.objects.select_for_update().filter(pk=grant_id).first()
        if grant is None:
            msg = "No such grant"
            raise ValueError(msg)
        parent = grant.envelope.parent
        if not (
            grant.grantee_id == by_id
            or may_manage(providers, grant.pool, by_id, admin=admin)
            # Its owner's envelope's pool now (after a rename).
            or (
                parent is not None
                and parent.owner_id == by_id
                and may_manage(providers, parent.pool, by_id, admin=admin)
            )
        ):
            msg = "Only the pool's owner, an admin or the grantee can revoke it"
            raise PermissionError(msg)
        if grant.revoked_at is None:
            grant.revoked_at = timezone.now()
            grant.revoked_reason = (reason or "revoked")[:200]
            grant.save(update_fields=["revoked_at", "revoked_reason"])
    return grant


def set_provider_enabled(
    name: str, providers: Providers, enabled: bool, *, version: int
):
    """Switch a provider (account) on or off for everyone; ``version`` (the
    one last read, 0 for a provider never switched) is required. Runs the ORM."""
    from django.db import transaction

    from django_ergo.conversation.models import ProviderToggle

    _require_version(version, f"Switching {name!r}")
    if name not in providers.providers:
        msg = f"No provider named {name!r}"
        raise ValueError(msg)
    if not isinstance(enabled, bool):
        msg = "enabled must be true or false"
        raise ValueError(msg)  # noqa: TRY004
    with transaction.atomic():
        toggle, _ = ProviderToggle.objects.select_for_update().get_or_create(
            name=name, defaults={"version": 0}
        )
        if toggle.version != version:
            msg = f"Provider {name!r} changed elsewhere (version {toggle.version}); reload"
            raise VersionConflict(msg)
        toggle.enabled = enabled
        if not enabled:
            # Remember the account, so a renamed provider stays off for spend.
            toggle.accounts = provider_accounts(providers.providers[name])
        toggle.version += 1
        toggle.save()
    return toggle


# -- queued work ------------------------------------------------------------------


def capture_policy(bot: Bot, session=None, user_id=None) -> dict:
    """The execution policy to store with queued work (a background task or a
    durable worker): the pool new work would use now, the user's grant on it
    and the revision it was read at. Running it later under
    :func:`policy_scope` can only narrow this (a revoked grant or switched
    off pool refuses), never pick up a later, wider choice. {} without pools.
    Runs the ORM."""
    providers = getattr(bot, "providers", None)
    if providers is None or providers.pools.legacy or bot.private_pool:
        return {}
    if (existing := current_policy()) is not None:
        return dict(existing)  # queued from queued work: the same policy
    user_id = user_id if user_id is not None else getattr(session, "user_id", None)
    privacy = privacy_for(bot, session)
    snap = snapshot(providers, user_id, session)
    try:
        index, mode = _start(snap, privacy, BACKGROUND)
    except PoolRefusal:
        return {"pool": "", "grant": "", "user": user_id, "revision": snap.revision()}
    view = snap.pools[index]
    return {
        "pool": view.name,
        "mode": mode,
        "grant": str(view.grant.pk) if view.grant is not None else "",
        "user": user_id,
        "rolled_from": snap.rolled_from,
        "revision": snap.revision(view),
    }


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
