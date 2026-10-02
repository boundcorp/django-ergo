"""WindowChat: a long-running chat whose context is rebuilt every turn.

Instead of replaying its whole history, a window chat sends the model:

- the system prompt,
- a context block from a ``ContextBuilder``: the latest ``recent`` messages
  of this chat (conversation granularity by default) plus any extra sources,
- the current turn only, natively (the new message and its tool calls),

and gives it ``MessageHistoryToolkit`` over its own history (plus any other
sources), so it can page further back or ask for more detail when the
recent window isn't enough. Context size stays flat however long the chat
runs.

(Formerly ``StreamChat`` in ``conversation.stream``; that import still works.)

    chat = await WindowChat.create(user=user, recent=15)
    async for event in chat.send("What did we decide about the fridge?"):
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.context import ContextBuilder
from django_ergo.conversation.context import MessageContextSource
from django_ergo.conversation.history import Granularity
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.history_search_toolkit import MessageHistoryToolkit
from django_ergo.conversation.manager import SessionManager
from django_ergo.conversation.runner import run_conversation_turn
from django_ergo.conversation.runtime import get_default_engine_spec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.attachments import Attachment
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.history import MessageSource
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.runner import PendingApproval
    from django_ergo.conversation.runtime import EngineSpec
    from django_ergo.conversation.toolkit import Toolkit

DEFAULT_RECENT = 15
DEFAULT_BUDGET_TOKENS = 8000
WINDOW_CONFIG = {"native_history": "turn"}


class WindowChat:
    def __init__(  # noqa: PLR0913
        self,
        session: ConversationSession,
        engine: Engine,
        *,
        recent: int = DEFAULT_RECENT,
        granularity: Granularity | str = Granularity.CONVERSATION,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        context_sources: list[ContextSource] | None = None,
        history_sources: list[MessageSource] | None = None,
        toolkits: list[Toolkit] | None = None,
    ):
        self.session = session
        self.engine = engine
        self.recent = recent
        self.granularity = Granularity.parse(granularity)
        self.budget_tokens = budget_tokens
        self.context_sources = list(context_sources or [])
        self.history_sources = list(history_sources or [])
        self.toolkits = list(toolkits or [])
        self.own_source = SessionSource(session)

    @classmethod
    async def create(  # noqa: PLR0913
        cls,
        *,
        user,
        workflow=None,
        engine: Engine | None = None,
        engine_spec: EngineSpec | None = None,
        system_prompt: str = "",
        metadata: dict | None = None,
        **options,
    ) -> WindowChat:
        spec = engine_spec or get_default_engine_spec()
        manager = SessionManager()
        session = await manager.create_session(
            user=user,
            workflow=workflow,
            engine_type=getattr(engine, "engine_type", None) or spec.engine_type,
            transport_type=spec.transport_type,
            metadata=metadata,
            compaction_config=dict(WINDOW_CONFIG),
            system_prompt=system_prompt,
        )
        active_engine = engine or await manager.get_engine(session)
        return cls(session, active_engine, **options)

    @classmethod
    async def resume(
        cls, session: ConversationSession, engine: Engine | None = None, **options
    ) -> WindowChat:
        """Wrap an existing session, switching it to per-turn native history."""
        config = dict(session.compaction_config or {})
        if config.get("native_history") != "turn":
            config.update(WINDOW_CONFIG)
            session.compaction_config = config
            await session.asave(update_fields=["compaction_config", "updated_at"])
        active_engine = engine or await SessionManager().get_engine(session)
        return cls(session, active_engine, **options)

    def context_builder(self) -> ContextBuilder:
        builder = ContextBuilder(budget_tokens=self.budget_tokens)
        builder.add(
            MessageContextSource(
                self.own_source,
                weight=3,
                title="Recent messages in this conversation",
                min_messages=self.recent,
                max_messages=self.recent,
                max_granularity=self.granularity,
                # The new message and its turn go natively, not here too.
                skip_native_turn=True,
            )
        )
        for source in self.context_sources:
            builder.add(source)
        return builder

    def history_toolkit(self) -> MessageHistoryToolkit:
        return MessageHistoryToolkit([self.own_source, *self.history_sources])

    async def send(
        self,
        message: str,
        *,
        attachments: list[Attachment] | None = None,
    ) -> AsyncIterator[EngineResponse | PendingApproval]:
        self.own_source.refresh()
        async for event in run_conversation_turn(
            self.engine,
            self.session,
            message,
            extra_tools=[self.history_toolkit(), *self.toolkits],
            attachments=attachments,
            context_builder=self.context_builder(),
        ):
            yield event
