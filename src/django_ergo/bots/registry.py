"""Load several bots and look them up by name (for bots that call bots).

Bot folders can nest: a bot folder inside another bot's folder is that
bot's sub-bot. ``discover`` finds every ``bot.yaml`` under a folder. A bot
may message chats of its sub-bots (downward, with ``orchestration`` on), the
bots in ``permissions.call_bots`` (sideways), and its parent bot. Without
orchestration, ``ergo_message_up`` permits only its own main chat or its parent
bot's main chat from its own main chat. A thread can also send or forward to its
exact starter chat, including a starter inherited from a same-bot ancestor,
even when that bot is not otherwise callable. Sends to the starter are reports
by default; ``ask=True`` requests a reply. This exception does not grant access
to other chats of the starter's bot::

    boundcorp/
      bot.yaml          # the parent, with orchestration on
      kitchen/
        bot.yaml        # a sub-bot of boundcorp
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.definition import CONFIG_FILE
from django_ergo.bots.providers import Providers
from django_ergo.bots.runtime import Bot

if TYPE_CHECKING:
    from collections.abc import Iterable

# Folders never searched for bots.
SKIP_DIRS = {"node_modules", "__pycache__", "skills", "tools", "kb"}


def may_use(bot: Bot, user) -> bool:
    """Whether a user may access this bot; superusers are always allowed."""
    allowed = bot.definition.allowed_users
    return (
        not allowed
        or user is None
        or user.is_superuser
        or user.get_username() in allowed
    )


def closed_to(bot: Bot, session) -> bool:
    """Whether ``session`` is a chat of a private bot other than ``bot``, so
    ``bot`` may not read it or its files (a file shared with a message is the
    exception, see ``messaging.shared_with``)."""
    registry = bot.registry
    return bool(
        registry is not None
        and session.bot_name != bot.name
        and registry.is_private(session.bot_name)
    )


def find_bot_folders(directory: str | Path) -> list[Path]:
    """Every folder at or under ``directory`` with a bot.yaml, parents first."""
    root = Path(directory).resolve()
    found = []
    if (root / CONFIG_FILE).is_file():
        found.append(root)
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if (
            child.is_dir()
            and not child.name.startswith(".")
            and child.name not in SKIP_DIRS
        ):
            found.extend(find_bot_folders(child))
    return found


logger = logging.getLogger(__name__)


class BotRegistry:
    def __init__(self):
        self.bots: dict[str, Bot] = {}
        # Bot folders that didn't load (with skip_broken): folder -> the error.
        self.failed: dict[str, str] = {}
        # Engines and models chats may use (providers.yaml).
        self.providers = Providers()

    def add(self, bot: Bot) -> Bot:
        if bot.name in self.bots:
            msg = f"Duplicate bot name {bot.name!r}"
            raise ValueError(msg)
        bot.registry = self
        self.bots[bot.name] = bot
        self._link_parents()
        return bot

    def _link_parents(self) -> None:
        """Each bot's parent is the bot whose folder most closely contains it."""
        folders = {
            bot.definition.root_dir.resolve(): bot
            for bot in self.bots.values()
            if bot.definition.root_dir
        }
        for folder, bot in folders.items():
            parent = next((folders[up] for up in folder.parents if up in folders), None)
            bot.parent_name = parent.name if parent else ""

    def children(self, bot: Bot) -> list[Bot]:
        """The bots nested directly in ``bot``'s folder."""
        return [b for b in self.bots.values() if b.parent_name == bot.name]

    def may_call(self, caller: Bot, name: str) -> bool:
        if self.is_private(name) and name != caller.name:
            return False
        if name in caller.definition.call_bots or self.is_parent(caller, name):
            return True
        return name in self.bots and self.bots[name].parent_name == caller.name

    def is_private(self, name: str) -> bool:
        """Whether ``name`` is a bot with ``private: true``: no other bot may
        message it or read its chats. Only a chat it started reaches back, and
        only to that chat (the starter exception in bots.orchestrator)."""
        return name in self.bots and self.bots[name].definition.private

    def is_parent(self, caller: Bot, name: str) -> bool:
        """Whether ``name`` is the caller's parent, reachable upward by default.

        Other permissions and exact starter chats may allow additional targets.
        """
        return bool(name) and name == caller.parent_name and name in self.bots

    def callable_bots(self, caller: Bot) -> list[Bot]:
        """Bots ``caller`` may message: its sub-bots, ``call_bots`` and its parent."""
        return [
            b
            for b in self.bots.values()
            if b is not caller and self.may_call(caller, b.name)
        ]

    def load(self, path: str | Path, **kwargs) -> Bot:
        return self.add(Bot.load(path, **kwargs))

    @classmethod
    def from_paths(
        cls, paths: Iterable[str | Path], *, skip_broken: bool = False, **kwargs
    ) -> BotRegistry:
        """Load each bot folder. With ``skip_broken``, a folder that fails (bad
        bot.yaml, a tool file that won't import, a duplicate name) is logged and
        recorded in ``failed`` while the others load."""
        registry = cls()
        for path in paths:
            if not skip_broken:
                registry.load(path, **kwargs)
                continue
            try:
                registry.load(path, **kwargs)
            except Exception as exc:
                logger.exception("Bot folder %s didn't load", path)
                registry.failed[str(path)] = f"{type(exc).__name__}: {exc}"[:2000]
        return registry

    @classmethod
    def discover(cls, directory: str | Path, **kwargs) -> BotRegistry:
        """Load every bot.yaml at or under ``directory``, nested bots included."""
        return cls.from_paths(find_bot_folders(directory), **kwargs)

    def get(self, name: str) -> Bot:
        try:
            return self.bots[name]
        except KeyError:
            msg = f"No bot named {name!r}"
            raise KeyError(msg) from None

    def __contains__(self, name: str) -> bool:
        return name in self.bots

    def __iter__(self):
        return iter(self.bots.values())
