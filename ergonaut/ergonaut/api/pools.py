"""Model pools: the Routing page's Pools section and the chat pool picker.

Everything here reads and writes through ``django_ergo.bots.pools``, the same
module every model consumer selects with, so what the UI lists is what the
runtime allows. Every write needs the version it was made from (409 on a
stale one). Pools are user-owned: a user sees deployment pools, pools they
own and pools they hold a spend grant on, and only their own spend. A pool's
owner (or an admin) changes its gates and issues or revokes grants; admins
switch providers; a user's own choices (order, active pool, auto-rollover)
and a chat's pool are theirs.
"""

from __future__ import annotations

from datetime import datetime

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django_ergo.bots import pools
from django_ergo.conversation.inference_policy import PolicyError
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import ConfigDict

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import get_bot, get_session, registry, session_out

router = Router(tags=["pools"], auth=user_auth)


class PreferenceIn(Schema):
    version: int  # the preference version last read (0 before any)
    active_pool: str | None = None  # "" = automatic (preferred order)
    auto_rollover: bool | None = None
    clear_auto_rollover: bool = False  # back to the providers.yaml default
    order: list[str] | None = None


class PoolStateIn(Schema):
    # Budgets, expiry and per-launch caps are spend grants, not pool settings:
    # an old client sending them gets a 422, not a silent no-op.
    model_config = ConfigDict(extra="forbid")

    version: int
    enabled: bool | None = None
    rollover_out: bool | None = None
    allow_auto_entry: bool | None = None


class GrantIn(Schema):
    grantee: str  # username
    # Strings (or numbers) parsed and checked by the library: finite, >= 0.
    budget_usd: str | int | float
    expires_at: datetime  # with a timezone (UTC "Z" or an offset)
    valid_from: datetime | None = None
    per_launch_usd: str | int | float | None = None
    max_output_tokens: int | None = None
    routes: list[str] | None = None  # default: the pool's metered routes


class EnvelopeLinkIn(Schema):
    # A pool without an envelope: the config envelope to continue, or
    # fund=true (new money). A pool with one: fund=true approves the accounts
    # it spends on now; restore=true lifts a person's revocation of it.
    envelope: str | None = None
    fund: bool = False
    restore: bool = False
    note: str = ""


class ProviderIn(Schema):
    enabled: bool
    version: int  # 0 for a provider never switched


class ChatPoolIn(Schema):
    pool: str  # "" = follow the user's default / preferred order
    scope: str = "chat"  # chat, or user (the user-wide default)
    chat_version: int  # this chat's pool override version (pool_version)
    version: int | None = None  # the user preference version, for scope=user


def _admin(request) -> None:
    if not request.auth.is_superuser:
        raise HttpError(403, "Only admins can switch providers")


def _manager(request, name: str) -> None:
    if not pools.may_manage(registry().providers, name, request.auth.pk, admin=request.auth.is_superuser):
        raise HttpError(403, f"Only pool {name!r}'s owner (or an admin) can change it")


def _apply(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except pools.VersionConflict as exc:
        raise HttpError(409, str(exc)) from exc
    except PermissionError as exc:
        raise HttpError(403, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc


def _settings(request) -> dict:
    providers = registry().providers
    view = pools.settings_view(providers, request.auth.pk)
    admin = request.auth.is_superuser
    for row in view["pools"]:
        row["manageable"] = pools.may_manage(providers, row["name"], request.auth.pk, admin=admin)
    return {**view, "editable": admin, "admin": admin}


@router.get("/pools")
def pools_settings(request):
    """The pools this user may see, with status, what is selectable now,
    their own grant (owners: the grants they issued), provider switches and
    the user's own choices (order, active pool, auto-rollover)."""
    return _settings(request)


@router.patch("/pools/preferences")
def update_preferences(request, data: PreferenceIn):
    _apply(
        pools.update_preference,
        request.auth.pk,
        registry().providers,
        version=data.version,
        active_pool=data.active_pool,
        auto_rollover=data.auto_rollover,
        clear_auto_rollover=data.clear_auto_rollover,
        order=data.order,
    )
    return _settings(request)


@router.patch("/pools/{name}")
def update_pool(request, name: str, data: PoolStateIn):
    _manager(request, name)
    changes = {
        key: getattr(data, key)
        for key in ("enabled", "rollover_out", "allow_auto_entry")
        if getattr(data, key) is not None
    }
    _apply(pools.update_pool_state, name, registry().providers, version=data.version, **changes)
    return _settings(request)


@router.post("/pools/{name}/grants")
def issue_grant(request, name: str, data: GrantIn):
    """Issue a spend grant on a pool you own (admins: any). It replaces the
    grantee's previous grant on the pool; its ledger is new, and it is drawn
    from (and capped by) the owner's own budget on the pool."""
    _manager(request, name)
    grantee = get_user_model().objects.filter(username=data.grantee).first()
    if grantee is None:
        raise HttpError(400, f"No user named {data.grantee!r}")
    _apply(
        pools.issue_grant,
        registry().providers,
        name,
        issuer_id=request.auth.pk,
        grantee_id=grantee.pk,
        budget_usd=data.budget_usd,
        expires_at=data.expires_at,
        valid_from=data.valid_from,
        per_launch_usd=data.per_launch_usd,
        max_output_tokens=data.max_output_tokens,
        routes=data.routes,
        admin=request.auth.is_superuser,
    )
    return _settings(request)


@router.get("/pools/{name}/envelope")
def envelope_candidates(request, name: str):
    """Admins: why a pool's owner budget waits for a link, and the spend
    envelopes overlapping its metered accounts."""
    if not request.auth.is_superuser:
        raise HttpError(403, "Only admins can link a pool's spend authority")
    return _apply(pools.link_candidates, registry().providers, name)


@router.post("/pools/{name}/envelope")
def link_envelope(request, name: str, data: EnvelopeLinkIn):
    """Admins: give a pool whose metered accounts already have spend
    authority its envelope, by continuing one (its spend carries over) or
    funding a new one. Config sync never does either on its own."""
    if not request.auth.is_superuser:
        raise HttpError(403, "Only admins can link a pool's spend authority")
    _apply(
        pools.link_config_envelope,
        registry().providers,
        name,
        by_id=request.auth.pk,
        admin=True,
        envelope_id=data.envelope,
        fund=data.fund,
        restore=data.restore,
        note=data.note,
    )
    return _settings(request)


@router.delete("/pools/grants/{grant_id}")
def revoke_grant(request, grant_id: str):
    """Revoke a grant (the pool's owner, an admin, or its grantee)."""
    _apply(
        pools.revoke_grant,
        registry().providers,
        grant_id,
        by_id=request.auth.pk,
        admin=request.auth.is_superuser,
    )
    return _settings(request)


@router.patch("/providers/{name}")
def update_provider(request, name: str, data: ProviderIn):
    _admin(request)
    _apply(
        pools.set_provider_enabled,
        name,
        registry().providers,
        data.enabled,
        version=data.version,
    )
    return _settings(request)


@router.get("/sessions/{session_id}/pool")
async def chat_pool(request, session_id: str):
    """This chat's pools as its bot sees them (privacy applied), the active
    one, and the last refusal if the chat is stopped on one."""
    session = await get_session(request, session_id)
    bot = get_bot(session.bot_name, request.auth)
    view = await sync_to_async(pools.listing)(bot, session, request.auth.pk)
    meta = session.metadata or {}
    return {
        **view,
        "chat_pool": meta.get(pools.SESSION_POOL_KEY) or "",
        "chat_version": pools.chat_revision(session),
        "route": meta.get(pools.SESSION_ROUTE_KEY) or {},
        "refusal": meta.get(pools.SESSION_REFUSAL_KEY),
    }


@router.post("/sessions/{session_id}/pool")
async def set_chat_pool(request, session_id: str, data: ChatPoolIn):
    """Switch this chat's pool, or set the user-wide default from it (which
    clears this chat's override in the same transaction). Applies to the next
    turn and new descendants, never to a running one."""
    session = await get_session(request, session_id)
    bot = get_bot(session.bot_name, request.auth)
    privacy = pools.privacy_for(bot, session)
    if privacy.restricted and data.pool:
        raise HttpError(403, f"This chat is locked by privacy policy {privacy.label()}")
    if data.scope not in ("chat", "user"):
        raise HttpError(400, "scope is chat or user")
    await sync_to_async(_apply)(
        pools.switch_pool,
        request.auth.pk,
        bot.providers,
        data.pool,
        scope=data.scope,
        session=session,
        version=data.version,
        chat_version=data.chat_version,
    )
    if data.pool:
        # Fail now, not at the next turn, when the pool can't serve this chat.
        try:
            await sync_to_async(pools.select_route)(
                bot,
                session,
                ref=bot.session_model(session) or bot.model_ref() or "auto/medium",
                record=False,
            )
        except PolicyError as exc:
            return {"session": await sync_to_async(session_out)(session), "warning": str(exc)}
    return {"session": await sync_to_async(session_out)(session), "warning": ""}
