"""Load several bots and look them up by name (for bots that call bots).

Bot folders can nest: a bot folder inside another bot's folder is that
bot's sub-bot. ``discover`` finds every ``bot.yaml`` under a folder, and a
bot may call its sub-bots (as well as those in ``permissions.call_bots``)::

    boundcorp/
      bot.yaml          # the parent, with orchestration on
      kitchen/
        bot.yaml        # a sub-bot of boundcorp
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.definition import CONFIG_FILE
from django_ergo.bots.runtime import Bot

if TYPE_CHECKING:
    from collections.abc import Iterable

# Folders never searched for bots.
SKIP_DIRS = {"node_modules", "__pycache__", "skills", "tools", "kb"}


def find_bot_folders(directory: str | Path) -> list[Path]:
    """Every folder at or under ``directory`` with a bot.yaml, parents first."""
    root = Path(directory).resolve()
    found = []
    if (root / CONFIG_FILE).is_file():
        found.append(root)
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.is_dir() and not child.name.startswith(".") and child.name not in SKIP_DIRS:
            found.extend(find_bot_folders(child))
    return found


class BotRegistry:
    def __init__(self):
        self.bots: dict[str, Bot] = {}

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
            parent = next(
                (folders[up] for up in folder.parents if up in folders), None
            )
            bot.parent_name = parent.name if parent else ""

    def children(self, bot: Bot) -> list[Bot]:
        """The bots nested directly in ``bot``'s folder."""
        return [b for b in self.bots.values() if b.parent_name == bot.name]

    def may_call(self, caller: Bot, name: str) -> bool:
        if name in caller.definition.call_bots:
            return True
        return name in self.bots and self.bots[name].parent_name == caller.name

    def callable_bots(self, caller: Bot) -> list[Bot]:
        """Bots ``caller`` may message: its sub-bots and ``call_bots``."""
        return [
            b for b in self.bots.values() if b is not caller and self.may_call(caller, b.name)
        ]

    def load(self, path: str | Path, **kwargs) -> Bot:
        return self.add(Bot.load(path, **kwargs))

    @classmethod
    def from_paths(cls, paths: Iterable[str | Path], **kwargs) -> BotRegistry:
        registry = cls()
        for path in paths:
            registry.load(path, **kwargs)
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
