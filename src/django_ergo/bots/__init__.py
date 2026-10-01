"""Ergo bots: a bot is a folder with agents.md, bot.yaml and tool modules.

Tool modules only need ``bot_tool`` from here, so importing this package
stays light. The runtime lives in ``django_ergo.bots.runtime``.
"""

from django_ergo.bots.tools import ToolContext
from django_ergo.bots.tools import bot_context
from django_ergo.bots.tools import bot_tool

__all__ = ["ToolContext", "bot_context", "bot_tool"]
