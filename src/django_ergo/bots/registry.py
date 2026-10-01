"""Load several bots and look them up by name (for bots that call bots)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.definition import CONFIG_FILE
from django_ergo.bots.runtime import Bot

if TYPE_CHECKING:
    from collections.abc import Iterable


class BotRegistry:
    def __init__(self):
        self.bots: dict[str, Bot] = {}

    def add(self, bot: Bot) -> Bot:
        if bot.name in self.bots:
            msg = f"Duplicate bot name {bot.name!r}"
            raise ValueError(msg)
        bot.registry = self
        self.bots[bot.name] = bot
        return bot

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
        """Load every subfolder of ``directory`` that has a bot.yaml."""
        folders = sorted(
            p for p in Path(directory).iterdir() if (p / CONFIG_FILE).is_file()
        )
        return cls.from_paths(folders, **kwargs)

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
