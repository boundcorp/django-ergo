"""Bot runtime: a loaded bot definition with its tools, plugins and sessions.

    bot = Bot.load("bots/kitchen")
    root = await bot.root_session(user)
    result = await bot.ask(root, "What's for dinner?")
    result.reply   # ChatReply: a message, or a question with suggestions

Every turn is a chat reply: a structured call against the session (see
``conversation.chat_reply``). The bot uses its tools, then answers with a
``ChatReply``.

Each (bot, user) pair has one root session: a stream chat (see
``conversation.stream``) that only sends the current turn natively and gets
its latest messages through a context block. The root can read the history
of every session this bot has with the user. Other sessions (threads) use
the bot's default compaction mode and keep full native history.

Engines are built per turn from the definition. The API key is read from
the environment variable named in ``engine.api_key_env`` and is never
written to the session.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.utils.module_loading import import_string

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.orchestrator import orchestrator_toolkit
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.plugins import resolve_plugin_class
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import ToolContext
from django_ergo.bots.tools import load_tool_module
from django_ergo.conversation.chat_reply import ChatReply
from django_ergo.conversation.chat_reply import chat_reply_spec
from django_ergo.conversation.context import ContextBuilder
from django_ergo.conversation.context import MessageContextSource
from django_ergo.conversation.context import TextContextSource
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.history_search_toolkit import MessageHistoryToolkit
from django_ergo.conversation.models import CompactionMode
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.stream import STREAM_CONFIG
from django_ergo.conversation.structured import resume_structured_call
from django_ergo.conversation.structured import run_structured_call

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.tools import ToolModule
    from django_ergo.conversation.attachments import Attachment
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.runner import PendingApproval
    from django_ergo.conversation.structured import StructuredCallResult
    from django_ergo.conversation.toolkit import Toolkit

ROOT_ROLE = "root"
THREAD_ROLE = "thread"


@dataclass
class TurnResult:
    session: ConversationSession
    call: StructuredCall | None = None
    reply: ChatReply | None = None
    approvals: list[PendingApproval] = field(default_factory=list)

    @classmethod
    def from_call(cls, session, result: StructuredCallResult) -> TurnResult:
        return cls(
            session=session,
            call=result.call,
            reply=result.parsed,
            approvals=list(result.approvals),
        )

    @property
    def text(self) -> str:
        return self.reply.text if self.reply else ""

    @property
    def suggestions(self) -> list[str]:
        return list(self.reply.suggestions) if self.reply else []

    @property
    def needs_approval(self) -> bool:
        return bool(self.approvals)

    @property
    def error(self) -> str:
        return self.call.error if self.call else ""


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class Bot:
    def __init__(
        self,
        definition: BotDefinition,
        *,
        engine_factory: Callable[[], Engine] | None = None,
        registry: BotRegistry | None = None,
    ):
        self.definition = definition
        self.name = definition.name
        self.registry = registry
        self._engine_factory = engine_factory
        self.tool_modules: list[ToolModule] = [
            load_tool_module(path, definition.name) for path in definition.tool_files
        ]
        self.toolkit_factories = [
            import_string(path.replace(":", "."))
            for path in definition.toolkit_factories
        ]
        self.plugins: list[BotPlugin] = [
            resolve_plugin_class(spec.name)(self, spec.config)
            for spec in definition.plugins
        ]
        for plugin in self.plugins:
            plugin.on_load()

    @classmethod
    def load(cls, path: str | Path, **kwargs) -> Bot:
        return cls(BotDefinition.load(path), **kwargs)

    def plugin(self, name: str) -> BotPlugin | None:
        for plugin in self.plugins:
            if plugin.name == name:
                return plugin
        return None

    # -- engines -----------------------------------------------------------

    def engine_spec(self) -> EngineSpec:
        default = get_default_engine_spec()
        engine_type = self.definition.engine_type or default.engine_type
        # The settings default config only applies to the default engine type.
        config = {} if self.definition.engine_type else dict(default.config)
        config.update(self.definition.engine_config)
        if self.definition.api_key_env:
            key = os.environ.get(self.definition.api_key_env)
            if not key:
                msg = (
                    f"Bot {self.name!r} needs the {self.definition.api_key_env} "
                    "environment variable"
                )
                raise RuntimeError(msg)
            config["api_key"] = key
        return EngineSpec(
            engine_type=engine_type,
            transport_type=default.transport_type,
            config=config,
        )

    def make_engine(self) -> Engine:
        if self._engine_factory is not None:
            return self._engine_factory()
        return build_engine(self.engine_spec())

    # -- sessions ----------------------------------------------------------

    def sessions(self, user=None):
        """This bot's sessions, optionally for one user."""
        qs = ConversationSession.objects.filter(bot_name=self.name)
        if user is not None:
            qs = qs.filter(user=user)
        return qs

    async def root_session(self, user) -> ConversationSession:
        """Get or create the user's root session with this bot."""
        existing = await (
            self.sessions(user)
            .filter(parent__isnull=True, metadata__bot_role=ROOT_ROLE)
            .exclude(status="completed")
            .order_by("created_at")
            .afirst()
        )
        if existing is not None:
            return existing
        return await self._create(
            user=user,
            parent=None,
            role=ROOT_ROLE,
            compaction_mode=CompactionMode.NONE,
            compaction_config=dict(STREAM_CONFIG),
        )

    async def create_session(  # noqa: PLR0913
        self,
        user,
        *,
        parent: ConversationSession | None = None,
        title: str = "",
        compaction_mode: str | None = None,
        compaction_config: dict | None = None,
        system_prompt: str | None = None,
        metadata: dict | None = None,
    ) -> ConversationSession:
        """Start a thread session for this bot."""
        mode = compaction_mode or self.definition.default_compaction_mode
        if mode not in CompactionMode.values:
            msg = f"Unknown compaction mode {mode!r}"
            raise ValueError(msg)
        if compaction_config is None:
            compaction_config = (
                dict(self.definition.default_compaction_config)
                if mode == self.definition.default_compaction_mode
                else {}
            )
        return await self._create(
            user=user,
            parent=parent,
            role=THREAD_ROLE,
            compaction_mode=mode,
            compaction_config=compaction_config,
            system_prompt=system_prompt,
            metadata={**(metadata or {}), **({"title": title} if title else {})},
        )

    async def _create(  # noqa: PLR0913
        self,
        *,
        user,
        parent,
        role,
        compaction_mode,
        compaction_config,
        system_prompt=None,
        metadata=None,
    ) -> ConversationSession:
        engine = self.make_engine()
        session = await ConversationSession.objects.acreate(
            user=user,
            parent=parent,
            bot_name=self.name,
            engine_type=getattr(engine, "engine_type", None)
            or self.engine_spec().engine_type,
            transport_type="api",
            status="active",
            metadata={**(metadata or {}), "bot_role": role},
            compaction_mode=compaction_mode,
            compaction_config=compaction_config or {},
            system_prompt=(
                self.definition.instructions if system_prompt is None else system_prompt
            ),
        )
        session.session_id = await engine.start_session(session) or ""
        await session.asave(update_fields=["session_id"])
        for plugin in self.plugins:
            await _maybe_await(plugin.on_session_created(session))
        return session

    async def close_session(self, session: ConversationSession) -> None:
        session.status = "completed"
        await session.asave(update_fields=["status", "updated_at"])
        for plugin in self.plugins:
            await _maybe_await(plugin.on_session_closed(session))

    # -- per-turn wiring ---------------------------------------------------

    def tool_context(self, session: ConversationSession) -> ToolContext:
        return ToolContext(bot=self, session=session, user=session.user)

    @staticmethod
    def is_root(session: ConversationSession) -> bool:
        return (session.metadata or {}).get("bot_role") == ROOT_ROLE

    @staticmethod
    def is_stream(session: ConversationSession) -> bool:
        return (session.compaction_config or {}).get("native_history") == "turn"

    def history_sources(self, session: ConversationSession) -> list[SessionSource]:
        """Sessions the history tools can read: all of this user's sessions
        with the bot for the root, otherwise just the session itself."""
        if not self.is_root(session):
            return [SessionSource(session)]
        return [
            SessionSource(s)
            for s in self.sessions()
            .filter(user_id=session.user_id)
            .order_by("created_at")
        ]

    def toolkits(self, session: ConversationSession) -> list[Toolkit]:
        ctx = self.tool_context(session)
        toolkits: list[Toolkit] = [
            MessageHistoryToolkit(
                [SessionSource(session)],
                source_loader=lambda: self.history_sources(session),
            )
        ]
        if self.is_root(session) and self.definition.orchestration:
            toolkits.append(orchestrator_toolkit(ctx))
        tools = [tool for module in self.tool_modules for tool in module.tools]
        if tools:
            toolkits.append(FunctionToolkit(tools, ctx))
        for module in self.tool_modules:
            if module.toolkit_factory is not None:
                toolkits.extend(module.toolkit_factory(ctx) or [])
        for factory in self.toolkit_factories:
            made = factory(ctx)
            toolkits.extend(made if isinstance(made, list | tuple) else [made])
        for plugin in self.plugins:
            toolkits.extend(plugin.toolkits(ctx) or [])
        return toolkits

    def context_builder(
        self, session: ConversationSession, message: str
    ) -> ContextBuilder | None:
        ctx = self.tool_context(session)
        builder = ContextBuilder(budget_tokens=self.definition.budget_tokens)
        empty = True
        if self.definition.current_time:
            builder.add(
                TextContextSource(
                    "Current time",
                    lambda: ctx.now().strftime("%A, %B %d, %Y at %H:%M %Z"),
                    weight=0.2,
                )
            )
            empty = False
        for module in self.tool_modules:
            for item in module.contexts:
                builder.add(
                    TextContextSource(
                        item.title,
                        lambda item=item: item.render(ctx, message),
                        weight=item.weight,
                    )
                )
                empty = False
        if self.is_stream(session):
            builder.add(
                MessageContextSource(
                    SessionSource(session),
                    weight=3,
                    title="Recent messages in this conversation",
                    min_messages=self.definition.recent,
                    max_messages=self.definition.recent,
                    max_granularity=self.definition.granularity,
                )
            )
            empty = False
        for plugin in self.plugins:
            for source in plugin.context_sources(ctx, message) or []:
                builder.add(source)
                empty = False
        return None if empty else builder

    async def _prepare(self, session: ConversationSession, message: str):
        def build():
            # Touch the user so sync tool code can use ctx.user.
            _ = session.user
            return self.toolkits(session), self.context_builder(session, message)

        return await sync_to_async(build, thread_sensitive=True)()

    # -- turns -------------------------------------------------------------

    def reply_spec(self, toolkits: list[Toolkit]):
        return chat_reply_spec(toolkits)

    async def ask(
        self,
        session: ConversationSession,
        message: str,
        *,
        attachments: list[Attachment] | None = None,
    ) -> TurnResult:
        """Answer one message with a ChatReply. Plugins see before/after hooks."""
        for plugin in self.plugins:
            await _maybe_await(plugin.before_turn(session, message))
        toolkits, builder = await self._prepare(session, message)
        outcome = await run_structured_call(
            self.reply_spec(toolkits),
            message,
            session=session,
            engine=self.make_engine(),
            attachments=attachments,
            context_builder=builder,
            allow_approvals=True,
        )
        result = TurnResult.from_call(session, outcome)
        for plugin in self.plugins:
            await _maybe_await(plugin.after_turn(session, message, result))
        return result

    async def pending_call(self, session: ConversationSession) -> StructuredCall | None:
        """The session's latest turn if it is waiting for approval."""
        return (
            await session.structured_calls.filter(
                status=StructuredCallStatus.AWAITING_APPROVAL
            )
            .order_by("-created_at")
            .afirst()
        )

    async def resume(
        self, session: ConversationSession, decisions: dict[str, bool] | bool
    ) -> TurnResult:
        """Continue a turn that stopped for approval.

        ``decisions`` maps tool_use_id to approve/deny, or is one bool for
        every pending tool call.
        """
        call = await self.pending_call(session)
        if call is None:
            return TurnResult(session=session)
        if isinstance(decisions, bool):
            pending = (call.metadata or {}).get("pending_approvals", [])
            decisions = {item["id"]: decisions for item in pending}
        toolkits, builder = await self._prepare(session, "")
        outcome = await resume_structured_call(
            self.reply_spec(toolkits),
            call,
            decisions,
            engine=self.make_engine(),
            context_builder=builder,
        )
        result = TurnResult.from_call(session, outcome)
        for plugin in self.plugins:
            await _maybe_await(plugin.after_turn(session, call.request, result))
        return result

    async def serve(self) -> None:
        """Run every plugin's long-running ``serve`` (e.g. chat channels)."""
        await asyncio.gather(*(plugin.serve() for plugin in self.plugins))
