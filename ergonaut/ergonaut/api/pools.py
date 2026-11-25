"""Model pools: the Routing page's Pools section and the chat pool picker.

Everything here reads and writes through ``django_ergo.bots.pools``, the same
module every model consumer selects with, so what the UI lists is what the
runtime allows. Writes are version-checked (409 on a stale version).
Deployment-wide settings (pool toggles, budgets, provider switches) are for
superusers; a user's own pool choices (order, active pool, auto-rollover) and
a chat's pool are theirs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from asgiref.sync import sync_to_async
from django_ergo.bots import pools
from django_ergo.conversation.inference_policy import PolicyError
from ninja import Router, Schema
from ninja.errors import HttpError

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import get_bot, get_session, registry, session_out

router = Router(tags=["pools"], auth=user_auth)


class PreferenceIn(Schema):
    version: int | None = None
    active_pool: str | None = None  # "" = automatic (preferred order)
    auto_rollover: bool | None = None
    clear_auto_rollover: bool = False  # back to the providers.yaml default
    order: list[str] | None = None


class PoolStateIn(Schema):
    version: int | None = None
    enabled: bool | None = None
    rollover_out: bool | None = None
    allow_auto_entry: bool | None = None
    budget_usd: Decimal | None = None
    clear_budget: bool = False  # an explicit "no budget" (metered routes refuse)
    expires_at: datetime | None = None
    clear_expiry: bool = False
    per_launch_usd: Decimal | None = None


class ProviderIn(Schema):
    enabled: bool
    version: int | None = None


class ChatPoolIn(Schema):
    pool: str  # "" = follow the user's default / preferred order
    scope: str = "chat"  # chat, or user (the user-wide default)
    version: int | None = None  # the user preference version, for scope=user


def _admin(request) -> None:
    if not request.auth.is_superuser:
        raise HttpError(403, "Only admins can change deployment-wide pool settings")


def _apply(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except pools.VersionConflict as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(400, str(exc)) from exc


def _settings(request) -> dict:
    providers = registry().providers
    return {
        **pools.settings_view(providers, request.auth.pk),
        "editable": request.auth.is_superuser,
    }


@router.get("/pools")
def pools_settings(request):
    """Every pool with status, budget and spend, provider switches and the
    user's own choices (order, active pool, auto-rollover)."""
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
    _admin(request)
    changes = {}
    for key in ("enabled", "rollover_out", "allow_auto_entry"):
        value = getattr(data, key)
        if value is not None:
            changes[key] = value
    if data.clear_budget:
        changes["budget_usd"] = None
    elif data.budget_usd is not None:
        changes["budget_usd"] = data.budget_usd
    if data.clear_expiry:
        changes["expires_at"] = None
    elif data.expires_at is not None:
        changes["expires_at"] = data.expires_at
    if data.per_launch_usd is not None:
        changes["per_launch_usd"] = data.per_launch_usd
    _apply(pools.update_pool_state, name, registry().providers, version=data.version, **changes)
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
        "route": meta.get(pools.SESSION_ROUTE_KEY) or {},
        "refusal": meta.get(pools.SESSION_REFUSAL_KEY),
    }


@router.post("/sessions/{session_id}/pool")
async def set_chat_pool(request, session_id: str, data: ChatPoolIn):
    """Switch this chat's pool, or set the user-wide default from it. Applies
    to the next turn and new descendants, never to a running one."""
    session = await get_session(request, session_id)
    bot = get_bot(session.bot_name, request.auth)
    privacy = pools.privacy_for(bot, session)
    if privacy.restricted and data.pool:
        raise HttpError(403, f"This chat is locked by privacy policy {privacy.label()}")
    if data.scope == "user":
        await sync_to_async(_apply)(
            pools.update_preference,
            request.auth.pk,
            bot.providers,
            version=data.version,
            active_pool=data.pool,
        )
        if (session.metadata or {}).get(pools.SESSION_POOL_KEY):
            await sync_to_async(_apply)(pools.set_chat_pool, session, "", bot.providers)
        else:
            await sync_to_async(_clear_refusal)(session)
    elif data.scope == "chat":
        await sync_to_async(_apply)(pools.set_chat_pool, session, data.pool, bot.providers)
    else:
        raise HttpError(400, "scope is chat or user")
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


def _clear_refusal(session) -> None:
    meta = dict(session.metadata or {})
    if meta.pop(pools.SESSION_REFUSAL_KEY, None) is not None:
        meta.pop("routed_model", None)
        session.metadata = meta
        session.save(update_fields=["metadata", "updated_at"])
