"""Which logins can be redone, and with what command.

A bot declares its credential stores in one of its tool modules::

    RELOGIN_STORES = {
        "omp/anthropic": {
            "label": "Anthropic login in omp",
            "command": "omp login anthropic",
            "shape": "paste_redirect",      # device_code, paste_code or paste_redirect
            "url_hosts": ["claude.ai"],
            "max_seconds": 300,             # the provider's own limit, if under 10 minutes
            "port": 54545,                  # a fixed local callback port, if any
        },
    }
    RELOGIN_SSH = {"host": ..., "port": 22, "user": ..., "identity": ..., "host_key": ...}

    def relogin_identity(ctx, key) -> str | None: ...        # an opaque hash of the account
    def relogin_post_check(ctx, key, before) -> dict: ...    # {"ok", "state", "detail", "account"}

Whatever the bot says, only a command in ``COMMANDS`` below runs, with exactly that argv:
nothing a request sends ever reaches it. URL hosts are cut down to ``URL_HOSTS``."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The fixed allow-list. A bot picks one of these by its exact text; nothing else runs.
COMMANDS: dict[str, tuple[str, ...]] = {
    "omp login anthropic": ("omp", "login", "anthropic"),
    "omp login openai-codex-device": ("omp", "login", "openai-codex-device"),
    "omp login xai-oauth": ("omp", "login", "xai-oauth"),
    "claude auth login --claudeai": ("claude", "auth", "login", "--claudeai"),
    "codex login --device-auth": ("codex", "login", "--device-auth"),
}
SHAPES = {"device_code", "paste_code", "paste_redirect"}
# Sign-in pages a shown link may point at (the host or a subdomain of it).
URL_HOSTS = frozenset({"claude.ai", "claude.com", "auth.openai.com", "auth.x.ai", "accounts.x.ai"})
# The only local callback a pasted redirect URL may target.
REDIRECT_PORTS = frozenset({54545})
MAX_SECONDS = 600


class StoreError(ValueError):
    """The bot or store can't be re-logged in here; the message is safe to show."""


@dataclass(frozen=True)
class Store:
    bot_name: str
    key: str
    label: str
    command: str
    argv: tuple[str, ...]
    shape: str
    url_hosts: frozenset[str]
    max_seconds: int
    port: int | None

    @property
    def port_group(self) -> str:
        return f"port:{self.port}" if self.port else ""


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    user: str
    identity: Path
    host_key: Path


def _module(bot):
    for module in getattr(bot, "tool_modules", []):
        if isinstance(getattr(module.module, "RELOGIN_STORES", None), dict):
            return module.module
    return None


def bot_stores(bot) -> dict[str, Store]:
    module = _module(bot)
    if module is None:
        return {}
    found = {}
    for key, spec in module.RELOGIN_STORES.items():
        try:
            found[key] = _store(bot.name, key, spec)
        except StoreError:
            continue  # a store the allow-list doesn't cover is simply not offered
    return found


def _store(bot_name: str, key: str, spec) -> Store:
    if not isinstance(spec, dict) or not isinstance(key, str) or len(key) > 80:
        raise StoreError("This login store isn't set up for re-login.")
    command = spec.get("command")
    if command not in COMMANDS:
        raise StoreError("This store's command isn't on the re-login allow-list.")
    shape = spec.get("shape")
    if shape not in SHAPES:
        raise StoreError("This store's input shape isn't known.")
    hosts = frozenset(host for host in spec.get("url_hosts") or [] if isinstance(host, str) and host in URL_HOSTS)
    if not hosts:
        raise StoreError("This store names no allowed sign-in host.")
    port = spec.get("port")
    if shape == "paste_redirect" and port not in REDIRECT_PORTS:
        raise StoreError("This store's callback port isn't allowed.")
    if port is not None and not isinstance(port, int):
        raise StoreError("This store's callback port isn't allowed.")
    try:
        seconds = int(spec.get("max_seconds") or MAX_SECONDS)
    except (TypeError, ValueError):
        seconds = MAX_SECONDS
    return Store(
        bot_name=bot_name,
        key=key,
        label=str(spec.get("label") or key)[:120],
        command=command,
        argv=COMMANDS[command],
        shape=shape,
        url_hosts=hosts,
        max_seconds=max(60, min(MAX_SECONDS, seconds)),
        port=port,
    )


def get_bot(bot_name: str):
    from django_ergo.bots import webhooks

    registry = webhooks.get_registry()
    if registry is None:
        raise StoreError("No bots are loaded.")
    try:
        return registry.get(bot_name)
    except Exception as exc:  # noqa: BLE001 - an unknown bot is just "not found"
        raise StoreError("No such bot.") from exc


def load(bot_name: str, key: str) -> Store:
    bot = get_bot(bot_name)
    module = _module(bot)
    if module is None or key not in module.RELOGIN_STORES:
        raise StoreError("This bot has no such login store.")
    return _store(bot.name, key, module.RELOGIN_STORES[key])


def hook(bot, name: str):
    """The bot's ``relogin_identity`` or ``relogin_post_check``, if it has one."""
    module = _module(bot)
    function = getattr(module, name, None) if module else None
    return function if callable(function) else None


def ssh_target(bot) -> SshTarget:
    """Where the CLI runs: the bot's ``RELOGIN_SSH``; DEVBOX_SSH_HOST overrides the host."""
    module = _module(bot)
    spec = getattr(module, "RELOGIN_SSH", None) if module else None
    if not isinstance(spec, dict):
        raise StoreError("No ssh route to the login host is configured.")
    host = os.environ.get("DEVBOX_SSH_HOST") or spec.get("host")
    user = spec.get("user")
    if not _plain(host) or not _plain(user):
        raise StoreError("The ssh route to the login host is not valid.")
    try:
        port = int(spec.get("port") or 22)
    except (TypeError, ValueError) as exc:
        raise StoreError("The ssh route to the login host is not valid.") from exc
    identity = Path(os.path.expanduser(str(spec.get("identity") or "~/.ssh/id_ed25519")))
    host_key = Path(os.path.expanduser(str(spec.get("host_key") or "")))
    if not spec.get("host_key"):
        raise StoreError("The login host's ssh key isn't pinned.")
    return SshTarget(host=host, port=port, user=user, identity=identity, host_key=host_key)


def _plain(value) -> bool:
    """A host or user name with nothing ssh could read as an option or a second word."""
    return (
        isinstance(value, str)
        and 0 < len(value) < 254
        and not value.startswith("-")
        and all(ch.isalnum() or ch in "._-" for ch in value)
    )
