"""Ergo KB plugin: knowledge-base tools plus RAG prefetch.

    plugins:
      - name: ergo_kb
        knowledgebases: [Kitchen]        # Knowledgebase names (legacy KB app)
        # or: toolkit: "myapp.kb:make_toolkit"   # factory(ctx) -> Toolkit
        prefetch: new_session            # new_session | every_turn | off
        search_tool: kb_search           # tool called for prefetch
        top_k: 5
        weight: 1                        # share of the context budget

Every session of the bot gets the KB tools. Prefetch runs the search tool
with the user's message and puts the results in the turn's context block,
on a session's first turn (``new_session``) or on every turn. Results are
never stored in the conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.module_loading import import_string

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
        if self.prefetch not in PREFETCH_MODES:
            msg = f"ergo_kb prefetch must be one of {sorted(PREFETCH_MODES)}"
            raise ValueError(msg)
        factory = self.config.get("toolkit")
        self._factory = import_string(factory.replace(":", ".")) if factory else None
        if self._factory is None and not self.config.get("knowledgebases"):
            msg = "ergo_kb needs knowledgebases or toolkit"
            raise ValueError(msg)
        self.search_tool = self.config.get("search_tool", "kb_search")
        self.top_k = int(self.config.get("top_k", 5))

    def make_toolkit(self, ctx: ToolContext) -> Toolkit:
        if self._factory is not None:
            return self._factory(ctx)
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
        return not (session.claude_messages.exists() or session.openai_messages.exists())

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if not message.strip() or not self.should_prefetch(ctx):
            return []

        found: list[str] = []

        def search() -> str:
            # The builder may render a source twice; search only once.
            if not found:
                found.append(self._search(ctx, message))
            return found[0]

        return [
            TextContextSource(
                "Knowledge base results for this message",
                search,
                weight=float(self.config.get("weight", 1)),
            )
        ]

    def _search(self, ctx: ToolContext, message: str) -> str:
        try:
            toolkit = self.make_toolkit(ctx)
            return toolkit.execute_tool(
                self.search_tool, {"query": message, "top_k": self.top_k}
            )
        except Exception as e:  # noqa: BLE001 — prefetch must never block a turn
            return f"(Knowledge base prefetch failed: {e})"
