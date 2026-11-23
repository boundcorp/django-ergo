"""Which bots may connect Google accounts here, and how their tokens are stored.

A bot declares it in one of its tool modules::

    GOOGLE_OAUTH = {
        "label": "Personal Google",
        "client_id_env": "PERSONAL_GOOGLE_OAUTH_CLIENT_ID",          # environment secret names
        "client_secret_env": "PERSONAL_GOOGLE_OAUTH_CLIENT_SECRET",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly", ...],   # all required
    }

    def google_store_account(ctx, account: dict) -> None: ...     # encrypts and saves
    def google_list_accounts(ctx) -> list[dict]: ...              # status only, never tokens
    def google_revoke_token(ctx, email) -> str | None: ...        # the plain token, to revoke it
    def google_delete_account(ctx, email) -> None: ...

``calendar.events`` can create and change events and email invitations to attendees; the
page says so before sign-in. Only a bot with ``private: true`` is offered, and only to a user its ``permissions.users`` names
(or a superuser). Scopes outside ``ALLOWED_SCOPES`` are refused."""

from __future__ import annotations

import re
from dataclasses import dataclass

SCOPE_PREFIX = "https://www.googleapis.com/auth/"
ALLOWED_SCOPES = frozenset(
    SCOPE_PREFIX + name
    for name in (
        "gmail.readonly",
        "gmail.compose",
        "gmail.modify",
        "calendar.readonly",
        "calendar.events.readonly",
        "calendar.events",
        "calendar.calendarlist.readonly",
    )
)
BASE_SCOPES = ("openid", "email")
HOOKS = ("google_store_account", "google_list_accounts", "google_revoke_token", "google_delete_account")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")


class ConnectorError(ValueError):
    """This bot can't connect Google here; the message is safe to show."""


@dataclass(frozen=True)
class Connector:
    bot: object
    bot_name: str
    label: str
    client_id_env: str
    client_secret_env: str
    scopes: tuple[str, ...]
    module: object

    def hook(self, name: str):
        return getattr(self.module, name)


def get_bot(bot_name: str):
    from django_ergo.bots import webhooks

    registry = webhooks.get_registry()
    if registry is None:
        raise ConnectorError("No bots are loaded.")
    try:
        return registry.get(bot_name)
    except Exception as exc:  # noqa: BLE001 - an unknown bot is just "not found"
        raise ConnectorError("No such bot.") from exc


def _module(bot):
    for module in getattr(bot, "tool_modules", []):
        if isinstance(getattr(module.module, "GOOGLE_OAUTH", None), dict):
            return module.module
    return None


def user_may_connect(bot, user) -> bool:
    """The bot's own allowed users, or a superuser. A bot that lists nobody is superuser-only."""
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return user.get_username() in list(bot.definition.allowed_users)


def load(bot_name: str, user) -> Connector:
    """The connector for a bot, or ConnectorError. Fails closed on every doubt, and answers an
    unauthorised user exactly as it answers an unknown bot."""
    not_found = ConnectorError("No Google connection is set up for this bot.")
    try:
        bot = get_bot(bot_name)
    except ConnectorError:
        raise not_found from None
    if not user_may_connect(bot, user):
        raise not_found
    if not getattr(bot.definition, "private", False):
        raise ConnectorError("Google accounts can only be connected to a private bot.")
    module = _module(bot)
    if module is None:
        raise not_found
    spec = module.GOOGLE_OAUTH
    scopes = spec.get("scopes")
    if not isinstance(scopes, list | tuple) or not scopes or not all(isinstance(s, str) for s in scopes):
        raise ConnectorError("This bot's Google scopes aren't valid.")
    if not set(scopes) <= ALLOWED_SCOPES:
        raise ConnectorError("This bot asks for a Google scope that isn't allowed here.")
    id_env, secret_env = spec.get("client_id_env"), spec.get("client_secret_env")
    if not all(isinstance(n, str) and ENV_NAME.match(n) for n in (id_env, secret_env)):
        raise ConnectorError("This bot's client secret names aren't valid.")
    if not all(callable(getattr(module, name, None)) for name in HOOKS):
        raise ConnectorError("This bot doesn't define the Google store functions.")
    return Connector(
        bot=bot,
        bot_name=bot.name,
        label=str(spec.get("label") or bot.name)[:80],
        client_id_env=id_env,
        client_secret_env=secret_env,
        scopes=tuple(dict.fromkeys(scopes)),
        module=module,
    )
