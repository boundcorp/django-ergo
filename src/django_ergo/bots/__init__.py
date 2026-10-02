"""Ergo bots: a bot is a folder with agents.md, bot.yaml and tool modules.

Tool modules only need ``bot_tool`` (and ``bot_task``) from here, so importing this package
stays light. The runtime lives in ``django_ergo.bots.runtime``.
"""

from django_ergo.bots.tools import ToolContext
from django_ergo.bots.tools import bot_context
from django_ergo.bots.tools import bot_task
from django_ergo.bots.tools import bot_tool

__all__ = ["BotTable", "ToolContext", "bot_context", "bot_task", "bot_tool"]


def __getattr__(name: str):
    # BotTable is a Django model, so import it only when a bot's tables ask.
    if name == "BotTable":
        from django_ergo.bots.tables import BotTable

        return BotTable
    raise AttributeError(name)
