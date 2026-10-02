"""Ergo KB plugin: knowledge-base tools plus RAG prefetch.

    plugins:
      - name: ergo_kb
        path: kb                         # a folder of Markdown files in the bot repo
        # or: knowledgebases: [Kitchen]  # Knowledgebase names (legacy KB app)
        # or: toolkit: "myapp.kb:make_toolkit"   # factory(ctx) -> Toolkit
        prefetch: new_session            # new_session | every_turn | off
        search_tool: ergo_kb_search      # tool called for prefetch (kb_search for knowledgebases)
        top_k: 5
        weight: 1                        # share of the context budget

Every session of the bot gets the KB tools. Prefetch runs the search tool
with the user's message and puts the results in the turn's context block,
on a session's first turn (``new_session``) or on every turn. Results are
never stored in the conversation.

A bot folder with a ``kb/`` folder gets this plugin with ``path: kb``
automatically. A folder KB's root article (``kb/index.md``) is in context on
every turn, with the title "Knowledge base".

A ``path`` knowledge base is plain Markdown (see ``bots.folder_kb``). Pair it
with the bot_management plugin and the bot can edit its own articles and
propose them as pull requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.module_loading import import_string

from django_ergo.bots.folder_kb import FolderKB
from django_ergo.bots.plugins import BotPlugin
from django_ergo.conversation.context import TextContextSource

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

PREFETCH_MODES = {"new_session", "every_turn", "off"}


class ErgoKBPlugin(BotPlugin):
    name = "ergo_kb"

    def on_load(self) -> None:
        self.prefetch = self.config.get("prefetch", "new_session")
        if self.prefetch is False:  # YAML reads a bare `off` as false
            self.prefetch = "off"
        if self.prefetch not in PREFETCH_MODES:
            msg = f"ergo_kb prefetch must be one of {sorted(PREFETCH_MODES)}"
            raise ValueError(msg)
        factory = self.config.get("toolkit")
        self._factory = import_string(factory.replace(":", ".")) if factory else None
        self.folder = self._folder()
        if (
            self._factory is None
            and self.folder is None
            and not self.config.get("knowledgebases")
        ):
            msg = "ergo_kb needs path, knowledgebases or toolkit"
            raise ValueError(msg)
        default_search = "ergo_kb_search" if self.folder is not None else "kb_search"
        self.search_tool = self.config.get("search_tool", default_search)
        self.top_k = int(self.config.get("top_k", 5))

    def _folder(self) -> FolderKB | None:
        relative = self.config.get("path")
        if not relative:
            return None
        root_dir = self.bot.definition.root_dir
        if root_dir is None:
            msg = "ergo_kb path needs a bot loaded from a folder"
            raise ValueError(msg)
        root = (root_dir / str(relative)).resolve()
        repo_top = root_dir.resolve()
        # The KB may sit next to the bot folder in the same repo (../kb).
        if not (root.is_relative_to(repo_top) or root.is_relative_to(repo_top.parent)):
            msg = f"ergo_kb path {relative} is outside the bot repository"
            raise ValueError(msg)
        return FolderKB(root)

    def make_toolkit(self, ctx: ToolContext) -> Toolkit:
        if self._factory is not None:
            return self._factory(ctx)
        if self.folder is not None:
            return self.folder.toolkit(ctx)
        from django_ergo.kb_toolkit import KBToolkit
        from django_ergo.models import Knowledgebase

        names = list(self.config["knowledgebases"])
        kbs = list(Knowledgebase.objects.filter(name__in=names))
        missing = set(names) - {kb.name for kb in kbs}
        if missing:
            msg = f"Unknown knowledge bases: {', '.join(sorted(missing))}"
            raise ValueError(msg)
        return KBToolkit(kbs)

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return [self.make_toolkit(ctx)]

    def should_prefetch(self, ctx: ToolContext) -> bool:
        if self.prefetch == "off":
            return False
        if self.prefetch == "every_turn":
            return True
        session = ctx.session
        return not (
            session.claude_messages.exists() or session.openai_messages.exists()
        )

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        sources = self._root_sources()
        if not message.strip() or not self.should_prefetch(ctx):
            return sources

        found: list[str] = []

        def search() -> str:
            # The builder may render a source twice; search only once.
            if not found:
                found.append(self._search(ctx, message))
            return found[0]

        return [
            *sources,
            TextContextSource(
                "Knowledge base results for this message",
                search,
                weight=float(self.config.get("weight", 1)),
            ),
        ]

    def _root_sources(self) -> list[ContextSource]:
        if self._factory is not None or self.folder is None:
            return []
        root = self.folder.root_article()
        if root is None:
            return []
        return [
            TextContextSource(
                f"Knowledge base: {root.title}",
                lambda: root.body,
                weight=float(self.config.get("weight", 1)),
            )
        ]

    def _search(self, ctx: ToolContext, message: str) -> str:
        try:
            if self._factory is None and self.folder is not None:
                if not self.folder.search(message, self.top_k):
                    return ""  # nothing relevant: add no section
                return self.folder.render_results(message, self.top_k)
            toolkit = self.make_toolkit(ctx)
            return toolkit.execute_tool(
                self.search_tool, {"query": message, "top_k": self.top_k}
            )
        except Exception as e:  # noqa: BLE001 — prefetch must never block a turn
            return f"(Knowledge base prefetch failed: {e})"
