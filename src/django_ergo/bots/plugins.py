"""Bot plugins: add toolkits and context, and hook into the bot lifecycle.

A plugin is a class with any of these hooks::

    class MyPlugin(BotPlugin):
        name = "my_plugin"

        def on_load(self): ...                         # bot constructed
        def toolkits(self, ctx): return [...]          # tools for a session
        def context_sources(self, ctx, message): ...   # context for a turn
        async def on_session_created(self, session): ...
        async def before_turn(self, session, message): ...
        async def after_turn(self, session, message, result): ...
        async def on_session_closed(self, session): ...
        async def serve(self): ...                     # long-running, e.g. a channel
        def webhooks(self): return {"update": handler} # see django_ergo.bots.webhooks
        def agent_managers(self): return {"name": m}   # see django_ergo.bots.agents

``bot.yaml`` names plugins by short name (official plugins in
``django_ergo.plugins``, listed below, or
``DJANGO_ERGO["BOT_PLUGINS"]``) or by dotted path ``module:Class``. Every
other key in the plugin entry is passed as ``config``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.agents import AgentManager
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.runtime import TurnResult
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit

OFFICIAL_PLUGINS = {
    "ergo_kb": "django_ergo.plugins.kb.ErgoKBPlugin",
    "bot_management": "django_ergo.plugins.bot_management.BotManagementPlugin",
    "telegram": "django_ergo.plugins.telegram.TelegramPlugin",
    "orca": "django_ergo.plugins.orca.OrcaPlugin",
    "bash": "django_ergo.plugins.bash.BashPlugin",
    "kubectl": "django_ergo.plugins.kubectl.KubectlPlugin",
    "attachments": "django_ergo.plugins.attachments.AttachmentsPlugin",
    "pages": "django_ergo.plugins.pages.PagesPlugin",
}


class BotPlugin:
    name: str = ""
    # A plugin that adds tools is a skill (see bots.skillset): loaded on
    # request, with this description in the listing.
    description: str = ""

    def __init__(self, bot: Bot, config: dict[str, Any] | None = None):
        self.bot = bot
        self.config = dict(config or {})

    def on_load(self) -> None:
        """Called once, after the bot and all its plugins are constructed."""

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return []

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        """Context for a turn, while the plugin's skill is loaded."""
        return []

    def always_context_sources(
        self, ctx: ToolContext, message: str
    ) -> list[ContextSource]:
        """Context for every turn, loaded or not (e.g. a KB's root article)."""
        return []

    def skill_hint(self, ctx: ToolContext) -> str:
        """A short note for the skill listing while it's not loaded."""
        return ""

    @property
    def skill_name(self) -> str:
        return self.name

    def worker_functions(self) -> dict[str, Callable]:
        """Functions this plugin's tools start as workers, by name ("<plugin>:<name>")."""
        return {}

    def agent_managers(self) -> dict[str, AgentManager]:
        """Agent managers this plugin adds (where coding agents run), by name.
        Return the same instances on every call."""
        return {}

    def worker_log(self, worker) -> dict | None:
        """The recent output of one of this plugin's workers, read now, as
        ``{"source", "entries": [{"kind", "text", "at"}]}``; None if it has none."""
        return None

    @property
    def skill_instructions(self) -> str:
        """What the model reads when it loads this plugin's skill."""
        return ""

    @property
    def skill_requires(self) -> list[str]:
        """Skills loaded along with this one."""
        return []

    async def on_session_created(self, session: ConversationSession) -> None:
        pass

    async def before_turn(self, session: ConversationSession, message: str) -> None:
        pass

    async def after_turn(
        self, session: ConversationSession, message: str, result: TurnResult
    ) -> None:
        pass

    async def on_session_closed(self, session: ConversationSession) -> None:
        pass

    async def serve(self) -> None:
        """Long-running work, such as polling a chat channel. Optional."""

    def webhooks(self) -> dict[str, Callable]:
        """Webhook handlers by name: ``async handler(request) -> response``.

        Served at ``<BOT_WEBHOOK_BASE_URL>/<bot>/<plugin>/<name>/`` when the
        project includes ``django_ergo.bots.urls``. A handler returns an
        ``HttpResponse``, a JSON-able value, or None for an empty 200.
        """
        return {}

    def webhook_url(self, name: str) -> str | None:
        """This plugin's public URL for one of its webhooks, if one is set."""
        from django_ergo.settings import api_settings

        base = api_settings.BOT_WEBHOOK_BASE_URL
        if not base:
            return None
        return f"{base.rstrip('/')}/{self.bot.name}/{self.name}/{name}/"


def resolve_plugin_class(name: str) -> type[BotPlugin]:
    from django_ergo.settings import api_settings

    custom = getattr(api_settings, "BOT_PLUGINS", None) or {}
    path = custom.get(name) or OFFICIAL_PLUGINS.get(name) or name
    path = path.replace(":", ".")
    try:
        cls = import_string(path)
    except ImportError as e:
        msg = f"Unknown bot plugin {name!r}"
        raise ValueError(msg) from e
    if not (isinstance(cls, type) and issubclass(cls, BotPlugin)):
        msg = f"{path} is not a BotPlugin"
        raise ValueError(msg)  # noqa: TRY004
    return cls
