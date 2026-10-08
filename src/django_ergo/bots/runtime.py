"""Bot runtime: a loaded bot definition with its tools, plugins and sessions.

    bot = Bot.load("bots/kitchen")
    main = await bot.main_session(user)
    result = await bot.ask(main, "What's for dinner?")
    result.reply   # ChatReply: a message, or a question with suggestions

Every turn is a chat reply: a structured call against the session (see
``conversation.chat_reply``). The bot uses its tools, then answers with a
``ChatReply``.

Each (bot, user) pair has a main chat, plus one chat per named chat in
bot.yaml (``chats:``). All keep native history with token-based compaction.
Main and named chats can read every session this bot has with the user.
Threads are child sessions using the bot's default compaction policy. A chat's tools come from its skills (see
``bots.skillset``), loaded when needed.

Engines are built per turn from the definition. The API key is read from
the environment variable named in ``engine.api_key_env`` and is never
written to the session.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.utils.module_loading import import_string

from django_ergo.bots import messaging
from django_ergo.bots import page_actions
from django_ergo.bots.definition import MAIN
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.definition import PluginSpec
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.plugins import resolve_plugin_class
from django_ergo.bots.providers import Providers
from django_ergo.bots.routing import ensure_compiled
from django_ergo.bots.routing import is_auto
from django_ergo.bots.routing import pick_model as route_model
from django_ergo.bots.routing import record_switch
from django_ergo.bots.routing import retry_model as routing_retry_model
from django_ergo.bots.routing import tier_of
from django_ergo.bots.skills import Skill
from django_ergo.bots.skills import library_skills
from django_ergo.bots.skills import load_skills
from django_ergo.bots.skillset import STATE_KEY
from django_ergo.bots.skillset import SkillDef
from django_ergo.bots.skillset import SkillSet
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
from django_ergo.conversation.identity import thread_message_identity
from django_ergo.conversation.models import CompactionMode
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.models import normalize_compaction_mode
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.structured import StructuredCallError
from django_ergo.conversation.structured import resume_structured_call
from django_ergo.conversation.structured import run_structured_call

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.tools import PageAction
    from django_ergo.bots.tools import ToolModule
    from django_ergo.conversation.attachments import Attachment
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.runner import PendingApproval
    from django_ergo.conversation.structured import StructuredCallResult
    from django_ergo.conversation.structured import TurnControl
    from django_ergo.conversation.toolkit import Toolkit

logger = logging.getLogger(__name__)

MAIN_ROLE = "main"
CHAT_ROLE = "chat"  # a named chat from bot.yaml
THREAD_ROLE = "thread"
ROOT_ROLE = MAIN_ROLE  # the main chat was called the root
TOP_ROLES = {MAIN_ROLE, CHAT_ROLE, "root"}


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
        self.parent_name = ""  # set by the registry for nested bot folders
        self._engine_factory = engine_factory
        self.tool_modules: list[ToolModule] = [
            load_tool_module(path, definition.name) for path in definition.tool_files
        ]
        # @bot_task functions from the tool files, by name (see bots.background).
        self.tasks = {
            name: fn
            for module in self.tool_modules
            for name, fn in module.tasks.items()
        }
        self.skills: list[Skill] = load_skills(definition.skills_dir)
        named = self._named_skills()
        # Default skills nothing named: left out quietly if they don't fit.
        self.default_skills = self._default_skills() - named
        self.skills += library_skills(
            named | self.default_skills, exclude={s.name for s in self.skills}
        )
        self.tables = []
        if definition.table_files:
            from django_ergo.bots.tables import load_tables

            self.tables = load_tables(
                definition.name, definition.root_dir, definition.table_files
            )
        self.toolkit_factories = [
            import_string(path.replace(":", "."))
            for path in definition.toolkit_factories
        ]
        specs = list(definition.plugins)
        kb_folder = definition.root_dir / "kb" if definition.root_dir else None
        if (
            kb_folder is not None
            and kb_folder.is_dir()
            and not any(spec.name == "ergo_kb" for spec in specs)
        ):
            # A kb/ folder in the bot folder is its knowledge base.
            specs.append(PluginSpec("ergo_kb", {"path": "kb"}))
        specs = self._with_skill_plugins(specs)
        messaging.KNOWN_BOTS[definition.name] = self
        self.plugins: list[BotPlugin] = [
            resolve_plugin_class(spec.name)(self, spec.config) for spec in specs
        ]
        for plugin in self.plugins:
            plugin.on_load()
        # Skill folders that ship tools.py
        self.skill_tool_modules = {
            skill.name: load_tool_module(skill.tools_file, definition.name)
            for skill in self.skills
            if skill.tools_file is not None
        }
        for module in self.skill_tool_modules.values():
            self.tasks.update(module.tasks)
        # @page_action functions pages may call as the viewer, by name (see bots.page_actions).
        self.page_actions: dict[str, PageAction] = {}
        for module in [*self.tool_modules, *self.skill_tool_modules.values()]:
            for action_name, action in module.page_actions.items():
                if self.page_actions.setdefault(action_name, action) is not action:
                    msg = f"{definition.name}: two page actions named {action_name!r}"
                    raise ValueError(msg)
        self.skill_defs: list[SkillDef] = self._skill_defs()

    def _named_skills(self) -> set[str]:
        """Every skill name bot.yaml or the bot's skills mention (for the library)."""
        definition = self.definition
        names = set(definition.skill_includes) | set(definition.thread_skills)
        for chat in definition.chats.values():
            names |= set(chat.skills)
        for required in definition.skill_requires.values():
            names |= set(required)
        for skill in self.skills:
            names |= set(skill.requires)
        return names

    def _default_skills(self) -> set[str]:
        """``DJANGO_ERGO["DEFAULT_SKILLS"]`` for this bot, minus what bot.yaml excludes."""
        from django_ergo.settings import api_settings

        definition = self.definition
        if definition.root_dir is None or not definition.use_default_skills:
            return set()
        return {str(n) for n in api_settings.DEFAULT_SKILLS or []} - set(
            definition.skill_excludes
        )

    def _with_skill_plugins(self, specs: list[PluginSpec]) -> list[PluginSpec]:
        """Add the plugins skills ask for; refuse settings that contradict bot.yaml."""
        specs = [PluginSpec(spec.name, dict(spec.config)) for spec in specs]
        for skill in list(self.skills):
            wanted_specs = []
            clash = ""
            for name, wanted in skill.plugins.items():
                cls = resolve_plugin_class(name)
                spec = next(
                    (s for s in specs if resolve_plugin_class(s.name) is cls), None
                )
                for key, value in wanted.items():
                    if spec is not None and key in spec.config:
                        if spec.config[key] != value:
                            clash = (
                                f"Skill {skill.name} needs {name} with {key}: {value}, "
                                f"but bot.yaml sets {key}: {spec.config[key]}"
                            )
                wanted_specs.append((name, spec, wanted))
            if clash:
                if skill.name in self.default_skills:
                    logger.warning(
                        "%s: leaving out default skill (%s)", self.name, clash
                    )
                    self.skills.remove(skill)
                    continue
                raise ValueError(clash)
            for name, spec, wanted in wanted_specs:
                if spec is None:
                    specs.append(PluginSpec(name, dict(wanted)))
                else:
                    spec.config.update(wanted)
        return specs

    def _skill_defs(self) -> list[SkillDef]:
        """Everything this bot can load, as skills (see bots.skillset)."""
        from django_ergo.bots.orchestrator import ORCHESTRATION_INSTRUCTIONS
        from django_ergo.bots.orchestrator import orchestrator_toolkit
        from django_ergo.bots.orchestrator import upward_toolkit
        from django_ergo.bots.overview import OverviewSource

        requires = self.definition.skill_requires
        defs = [
            SkillDef(
                "history",
                "Read and search this conversation and your other chats with this person",
                toolkits=lambda ctx: [
                    MessageHistoryToolkit(
                        [SessionSource(ctx.session)],
                        source_loader=lambda: self.history_sources(ctx.session),
                    )
                ],
                always=True,
                source="built-in",
            )
        ]
        from django_ergo.bots.workers import worker_toolkit

        defs.append(
            SkillDef(
                "workers",
                "Long-running background work this chat started: list, start and cancel it",
                toolkits=lambda ctx: [worker_toolkit(self, ctx)],
                always=True,
                source="built-in",
            )
        )
        from django_ergo.bots.agents import AGENTS_INSTRUCTIONS
        from django_ergo.bots.agents import agent_toolkit
        from django_ergo.bots.agents import managers

        if managers(self):
            defs.append(
                SkillDef(
                    "agents",
                    "Start coding agents (Codex, Claude Code, omp) on tasks, answer "
                    "their questions and stop them",
                    instructions=AGENTS_INSTRUCTIONS,
                    toolkits=lambda ctx: [agent_toolkit(self, ctx)],
                    requires=requires.get("agents", []),
                    source="built-in",
                )
            )
        if self.definition.root_dir is not None:
            from django_ergo.bots.introspection import introspection_toolkit

            defs.append(
                SkillDef(
                    "introspection",
                    "Read your own config, instructions, tool code and files (read-only), "
                    "and Ergo's source",
                    toolkits=lambda ctx: [introspection_toolkit(self, ctx)],
                    source="built-in",
                )
            )
        if self.definition.orchestration:
            defs.append(
                SkillDef(
                    "orchestration",
                    "Delegate to your threads and to other bots, and check on them",
                    instructions=ORCHESTRATION_INSTRUCTIONS,
                    toolkits=lambda ctx: [orchestrator_toolkit(ctx)],
                    context=lambda ctx, message: [OverviewSource(ctx)],
                    source="built-in",
                )
            )
        else:
            # Upward messages are always allowed (bots.orchestrator).
            defs.append(
                SkillDef(
                    "upward",
                    "Message your main chat, or your parent bot's main chat",
                    toolkits=lambda ctx: [upward_toolkit(ctx)],
                    always=True,
                    source="built-in",
                )
            )
        if self.tables:
            from django_ergo.bots.tables import describe
            from django_ergo.bots.tables import table_tools

            tools = table_tools(self.tables)
            defs.append(
                SkillDef(
                    "tables",
                    "Look up and change rows in this bot's tables: "
                    + ", ".join(t.__name__ for t in self.tables),
                    instructions="Tables and their fields:\n"
                    + "\n".join(describe(t) for t in self.tables),
                    toolkits=lambda ctx, tools=tools: [FunctionToolkit(tools, ctx)],
                    requires=requires.get("tables", []),
                    source="tables",
                )
            )
        for module in self.tool_modules:
            name = module.path.stem
            doc = (inspect.getdoc(module.module) if module.module else "") or ""
            defs.append(
                SkillDef(
                    name,
                    doc.splitlines()[0] if doc else f"Tools from {module.path.name}",
                    toolkits=lambda ctx, module=module: self._module_toolkits(
                        module, ctx
                    ),
                    requires=requires.get(name, []),
                    source=f"tools/{module.path.name}",
                )
            )
        for path, factory in zip(
            self.definition.toolkit_factories, self.toolkit_factories, strict=True
        ):
            name = path.replace(":", ".").rsplit(".", 1)[-1]
            defs.append(
                SkillDef(
                    name,
                    (inspect.getdoc(factory) or "").split("\n")[0]
                    or f"Tools from {path}",
                    toolkits=lambda ctx, factory=factory: _as_list(factory(ctx)),
                    requires=requires.get(name, []),
                    source=path,
                )
            )
        for plugin in self.plugins:
            if type(plugin).toolkits is BotPlugin.toolkits:
                continue  # a plugin without tools (e.g. telegram) isn't a skill
            defs.append(
                SkillDef(
                    plugin.skill_name,
                    plugin.description or f"The {plugin.name} plugin",
                    instructions=plugin.skill_instructions,
                    toolkits=lambda ctx, plugin=plugin: plugin.toolkits(ctx) or [],
                    context=lambda ctx, message, plugin=plugin: (
                        plugin.context_sources(ctx, message) or []
                    ),
                    hint=plugin.skill_hint,
                    requires=[
                        *plugin.skill_requires,
                        *requires.get(plugin.skill_name, []),
                    ],
                    source=f"plugin {plugin.name}",
                )
            )
        for skill in self.skills:
            module = self.skill_tool_modules.get(skill.name)
            defs.append(
                SkillDef(
                    skill.name,
                    skill.description,
                    instructions=skill.body,
                    toolkits=(
                        lambda ctx, module=module: self._module_toolkits(module, ctx)
                    )
                    if module
                    else None,
                    requires=[*skill.requires, *requires.get(skill.name, [])],
                    always=skill.always_load,
                    source=("ergo:skill_library/" if skill.library else "skills/")
                    + (
                        skill.path.parent.name
                        if skill.path.name == "SKILL.md"
                        else skill.path.name
                    ),
                )
            )
        return defs

    def code(self, relative: str):
        """A Python file inside the bot folder, imported once (for schedule ``run`` steps)."""
        from django_ergo.bots.definition import _inside

        root = self.definition.root_dir
        if root is None:
            msg = "This bot wasn't loaded from a folder"
            raise ValueError(msg)
        path = _inside(root, relative)
        if path.suffix != ".py" or not path.is_file():
            msg = f"No Python file {relative} in the bot folder"
            raise ValueError(msg)
        for module in self.tool_modules:
            if module.path == path:
                return module.module
        if path in self.definition.table_files:
            from django_ergo.bots.tables import MODULES

            return MODULES[path]
        cache = self.__dict__.setdefault("_code", {})
        if path not in cache:
            cache[path] = load_tool_module(path, self.name).module
        return cache[path]

    def table(self, name: str):
        """One of this bot's tables (a Django model) by class name, any case."""
        for model in self.tables:
            if model.__name__.lower() == str(name).lower():
                return model
        known = ", ".join(t.__name__ for t in self.tables) or "none"
        msg = f"{self.name} has no table {name!r} (tables: {known})"
        raise LookupError(msg)

    @staticmethod
    def _module_toolkits(module: ToolModule, ctx: ToolContext) -> list[Toolkit]:
        toolkits: list[Toolkit] = (
            [FunctionToolkit(module.tools, ctx)] if module.tools else []
        )
        if module.toolkit_factory is not None:
            toolkits.extend(_as_list(module.toolkit_factory(ctx)))
        return toolkits

    @classmethod
    def load(cls, path: str | Path, **kwargs) -> Bot:
        return cls(BotDefinition.load(path), **kwargs)

    def plugin(self, name: str) -> BotPlugin | None:
        for plugin in self.plugins:
            if plugin.name == name:
                return plugin
        return None

    # -- engines -----------------------------------------------------------

    @property
    def providers(self) -> Providers:
        registry = getattr(self, "registry", None)
        return registry.providers if registry is not None else Providers()

    def model_ref(self) -> str:
        """The ``provider/model`` this bot uses by default, when providers.yaml names it."""
        model = str(self.definition.engine_config.get("model") or "")
        if "/" in model and self.providers.knows(model):
            return model
        if not self.definition.engine_type and self.providers.default:
            return self.providers.default
        return ""

    def session_model(self, session: ConversationSession | None) -> str:
        """The model picked for this chat, if it's still enabled."""
        if session is None:
            return ""
        return self.session_model_ref(session.model)

    def pick_model(self, session: ConversationSession, model: str) -> None:
        """Set the model a chat's next turns use ("" = the bot's default).

        Messages are stored the same way for every engine, so a model on
        another engine just takes the next turn. Runs the ORM.
        """
        session.model = self.session_model_ref(model)
        spec = self.engine_spec(session)
        fields = ["model", "updated_at"]
        if (spec.engine_type, spec.transport_type) != (
            session.engine_type,
            session.transport_type,
        ):
            session.engine_type = spec.engine_type
            session.transport_type = spec.transport_type
            session.session_id = ""  # an engine-native session doesn't carry over
            fields += ["engine_type", "transport_type", "session_id"]
        session.save(update_fields=fields)

    def session_model_ref(self, picked) -> str:
        picked = str(picked or "")
        return picked if picked and self.providers.knows(picked) else ""

    def route(self, session: ConversationSession) -> str:
        """For a chat on ``auto/<tier>``, pick this turn's model from what's
        left on each subscription and remember it (``bots.routing``). Returns
        the model, or "" for a chat with a fixed model. Runs the ORM."""
        ref = self.session_model(session) or self.model_ref()
        if not is_auto(ref):
            return ""
        ensure_compiled(
            self.providers,
            lambda: self.make_engine(model=self.resolve_ref("auto/small", None)),
        )
        before = (session.metadata or {}).get("routed_model", "")
        picked = route_model(self.providers, tier_of(ref), before)
        self._set_routed(session, tier_of(ref), picked)
        return picked

    def _set_routed(
        self, session: ConversationSession, tier: str, picked: str, reason: str = ""
    ) -> None:
        meta = session.metadata or {}
        before = meta.get("routed_model", "")
        if picked == before:
            return
        if before:
            record_switch(self.providers, session, tier, before, picked, reason)
        session.metadata = {**meta, "routed_model": picked}
        spec = self.engine_spec(session)
        session.engine_type = spec.engine_type
        session.transport_type = spec.transport_type
        session.save(
            update_fields=["metadata", "engine_type", "transport_type", "updated_at"]
        )

    def retry_model(self, session: ConversationSession, error: str) -> str:
        """For an ``auto/<tier>`` chat whose turn failed with ``error``: the
        model to offer a retry on when its provider refused for a limit, or
        "". Never applied on its own (see :meth:`reroute`). Runs the ORM."""
        ref = self.session_model(session) or self.model_ref()
        if not is_auto(ref):
            return ""
        failed = self.resolve_ref(ref, session)
        return routing_retry_model(self.providers, tier_of(ref), failed, error)

    def reroute(self, session: ConversationSession, model: str, reason: str) -> None:
        """Move an ``auto/<tier>`` chat to ``model``, one of its tier's
        candidates, for its next turns (it stays while that model qualifies).
        Runs the ORM."""
        ref = self.session_model(session) or self.model_ref()
        if not is_auto(ref) or model not in self.providers.routing.tiers.get(
            tier_of(ref), []
        ):
            msg = f"{model!r} isn't a model in this chat's tier"
            raise ValueError(msg)
        self._set_routed(session, tier_of(ref), model, reason)

    def resolve_ref(self, ref: str, session: ConversationSession | None) -> str:
        """``auto/<tier>`` as a concrete model: the one routed for this chat,
        else the tier's first available one. No ORM."""
        if not is_auto(ref):
            return ref
        tier = self.providers.routing.tiers.get(tier_of(ref), [])
        routed = ((session.metadata or {}) if session else {}).get("routed_model")
        if routed in tier:
            return routed
        for candidate in tier:
            found = self.providers.find(candidate)
            if found and found[0].available:
                return candidate
        return tier[0] if tier else ""

    def engine_spec(
        self, session: ConversationSession | None = None, model: str = ""
    ) -> EngineSpec:
        """The engine for a chat: the model picked for it (or ``model``), else the bot's."""
        default = get_default_engine_spec()
        ref = model or self.session_model(session) or self.model_ref()
        ref = self.resolve_ref(ref, session)
        transport = default.transport_type
        if ref:
            engine_type, config, key_env = self.providers.engine(ref)
            transport = self.providers.find(ref)[0].transport
            config["provider"] = ref.partition("/")[
                0
            ]  # usage windows are kept by provider
            extra = {
                k: v for k, v in self.definition.engine_config.items() if k != "model"
            }
            if self.definition.engine_type in ("", engine_type):
                config = {**config, **extra}  # e.g. the bot's max_tokens
        else:
            engine_type = self.definition.engine_type or default.engine_type
            # The settings default config only applies to the default engine type.
            config = {} if self.definition.engine_type else dict(default.config)
            config.update(self.definition.engine_config)
            key_env = self.definition.api_key_env
            transport = self.definition.engine_transport or transport
        if key_env:
            key = os.environ.get(key_env)
            if not key:
                msg = f"Bot {self.name!r} needs the {key_env} environment variable"
                raise RuntimeError(msg)
            config["api_key"] = key
        return EngineSpec(
            engine_type=engine_type,
            transport_type=transport,
            config=config,
        )

    async def thread_metadata(
        self, text: str, *, user=None, session: ConversationSession | None = None
    ) -> dict:
        """A title for a new thread from its first message (structured call
        ``new_thread_metadata``). Returns {} when the model gives none."""
        from pydantic import BaseModel
        from pydantic import Field

        from django_ergo.conversation.structured import StructuredCallSpec

        class ThreadMetadata(BaseModel):
            title: str = Field(
                description="A short title for the thread: 2 to 6 words, no quotes, no final period"
            )

        spec = StructuredCallSpec(
            kind="new_thread_metadata",
            system_prompt=(
                "Someone is starting a new conversation thread with an assistant. Read their "
                "first message and give the thread a short, specific title, like an email "
                "subject: 2 to 6 words, in the message's language, no quotes."
            ),
            response_model=ThreadMetadata,
            max_turns=2,
        )
        result = await run_structured_call(
            spec,
            text[:4000],
            user=user,
            engine=self.make_engine(),
            metadata={"bot": self.name, "session": str(session.id) if session else ""},
        )
        title = (
            (result.parsed.title if result.parsed else "")
            .strip()
            .strip('"')
            .rstrip(".")
        )
        return {"title": title[:80]} if title else {}

    def make_engine(
        self, session: ConversationSession | None = None, model: str = ""
    ) -> Engine:
        if self._engine_factory is not None:
            engine = self._engine_factory()
        else:
            engine = build_engine(self.engine_spec(session, model))
        if self.definition.tool_results_in_context is not None:
            engine.tool_results_in_context = self.definition.tool_results_in_context
        config = self.engine_spec(session, model).config
        name = str(config.get("model") or getattr(engine, "model", ""))
        engine.context_window = int(
            config.get("context_window")
            or (1_000_000 if name.endswith("[1m]") else 200_000)
        )
        if self.definition.tool_results_tokens is not None:
            engine.tool_results_tokens = self.definition.tool_results_tokens
        return engine

    # -- sessions ----------------------------------------------------------

    def sessions(self, user=None):
        """This bot's sessions, optionally for one user."""
        qs = ConversationSession.objects.filter(bot_name=self.name)
        if user is not None:
            qs = qs.filter(user=user)
        return qs

    async def main_session(self, user) -> ConversationSession:
        """Get or create the user's main chat with this bot."""
        return await self.chat_session(user, MAIN)

    root_session = main_session  # the main chat used to be called the root

    async def chat_session(self, user, name: str = MAIN) -> ConversationSession:
        """Get or create the user's ``main`` chat, or a named chat from bot.yaml."""
        if name in ("root", MAIN):
            name, role = MAIN, MAIN_ROLE
            qs = self.sessions(user).filter(
                parent__isnull=True, metadata__bot_role__in=["root", MAIN_ROLE]
            )
        else:
            if name not in self.definition.chats:
                msg = f"{self.name} has no chat named {name!r}"
                raise ValueError(msg)
            role = CHAT_ROLE
            qs = self.sessions(user).filter(
                metadata__bot_role=CHAT_ROLE, metadata__chat=name
            )
        existing = await qs.exclude(status="completed").order_by("created_at").afirst()
        if existing is not None:
            return existing
        chat = self.definition.chat(name)
        metadata = (
            {}
            if role == MAIN_ROLE
            else {
                "chat": name,
                "title": chat.description or name.replace("-", " ").title(),
            }
        )
        return await self._create(
            user=user,
            parent=None,
            role=role,
            compaction_mode=CompactionMode.CONTEXT_SIZE,
            compaction_config={},
            metadata=metadata,
            system_prompt=self.instructions_for(name),
        )

    def instructions_for(self, chat: str | None) -> str:
        """The bot's instructions, plus a chat's own when it has any."""
        extra = self.definition.chat(chat).instructions if chat else ""
        return "\n\n".join(
            part for part in (self.definition.instructions, extra) if part
        )

    @staticmethod
    def chat_name(session: ConversationSession) -> str | None:
        """``main``, a named chat's name, or None for a thread."""
        meta = session.metadata or {}
        role = meta.get("bot_role")
        if role in ("root", MAIN_ROLE):
            return MAIN
        if role == CHAT_ROLE:
            return meta.get("chat")
        return None

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
        mode = normalize_compaction_mode(
            compaction_mode or self.definition.default_compaction_mode
        )
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
        metadata = dict(metadata or {})
        model = self.session_model_ref(metadata.pop("model", ""))
        engine = self.make_engine(model=model)
        session = await ConversationSession.objects.acreate(
            user=user,
            parent=parent,
            bot_name=self.name,
            model=model,
            engine_type=getattr(engine, "engine_type", None)
            or self.engine_spec(model=model).engine_type,
            transport_type=getattr(engine, "transport_type", "api"),
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
        """A top-level chat: main or a named chat (not a thread)."""
        return (session.metadata or {}).get("bot_role") in TOP_ROLES

    @staticmethod
    def is_main(session: ConversationSession) -> bool:
        return (session.metadata or {}).get("bot_role") in ("root", MAIN_ROLE)

    @staticmethod
    def is_window(session: ConversationSession) -> bool:
        """A window chat: only the current turn goes to the model natively."""
        return (session.compaction_config or {}).get("native_history") == "turn"

    is_stream = is_window  # deprecated alias

    def history_sources(self, session: ConversationSession) -> list[SessionSource]:
        """Sessions the history tools can read: all of this user's sessions
        with the bot, from any chat or thread."""
        return [
            SessionSource(s)
            for s in self.sessions()
            .filter(user_id=session.user_id)
            .order_by("created_at")
        ]

    def skillset(self, session: ConversationSession) -> SkillSet:
        """The chat's skills for this turn (see bots.skillset)."""
        ctx = self.tool_context(session)
        chat = self.chat_name(session)
        always = set(
            self.definition.chat(chat).skills if chat else self.definition.thread_skills
        )
        turn = (
            session.structured_calls.filter(kind="chat_reply").count()
            if session.pk is not None
            else 0
        )

        def save(state: dict) -> None:
            fresh = (
                ConversationSession.objects.filter(pk=session.pk)
                .values_list("metadata", flat=True)
                .first()
            )
            metadata = {**(fresh or {}), STATE_KEY: state}
            ConversationSession.objects.filter(pk=session.pk).update(metadata=metadata)
            session.metadata = metadata

        return SkillSet(
            ctx,
            self.skill_defs,
            always=always,
            unload_after_turns=self.definition.unload_after_turns,
            turn=turn,
            save=save if session.pk is not None else None,
            state=(session.metadata or {}).get(STATE_KEY),
        )

    def _function_context_sources(
        self, ctx: ToolContext, session: ConversationSession, message: str
    ) -> list[TextContextSource]:
        """@bot_context functions of the tool files, and the page actions people ran since the last reply."""
        sources = [
            TextContextSource(
                item.title,
                lambda item=item: item.render(ctx, message),
                weight=item.weight,
            )
            for module in self.tool_modules
            for item in module.contexts
        ]
        if text := page_actions.context_text(session):
            sources.append(TextContextSource(page_actions.CONTEXT_TITLE, text))
        return sources

    def toolkits(self, session: ConversationSession) -> list[Toolkit]:
        return [self.skillset(session)]

    def context_builder(
        self,
        session: ConversationSession,
        message: str,
        skillset: SkillSet | None = None,
        *,
        incoming: bool = True,
    ) -> ContextBuilder | None:
        """The turn's context. ``incoming`` is False when resuming a stored turn."""
        ctx = self.tool_context(session)
        skills = skillset or self.skillset(session)
        builder = ContextBuilder(budget_tokens=self.definition.budget_tokens)
        from django_ergo.bots.orchestrator import chat_identity

        builder.add(TextContextSource("This chat", chat_identity(session), weight=3))
        builder.add(TextContextSource("Skills", skills.context_summary, weight=1))
        empty = False
        if self.definition.current_time:
            builder.add(
                TextContextSource(
                    "Current time",
                    lambda: ctx.now().strftime("%A, %B %d, %Y at %H:%M %Z"),
                    weight=0.2,
                )
            )
            empty = False
        for source in self._function_context_sources(ctx, session, message):
            builder.add(source)
            empty = False
        if self.is_window(session):
            builder.add(
                MessageContextSource(
                    SessionSource(session),
                    weight=3,
                    title="Recent messages in this conversation",
                    min_messages=self.definition.recent,
                    max_messages=self.definition.recent,
                    max_granularity=self.definition.granularity,
                    # What the engine sends natively isn't repeated here.
                    skip_native_turn=True,
                    incoming=incoming,
                )
            )
            empty = False
        for plugin in self.plugins:
            for source in plugin.always_context_sources(ctx, message) or []:
                builder.add(source)
                empty = False
            if type(plugin).toolkits is BotPlugin.toolkits:
                # Not a skill (e.g. telegram): its context is always on.
                for source in plugin.context_sources(ctx, message) or []:
                    builder.add(source)
                    empty = False
        for source in skills.context_sources(message):
            builder.add(source)
            empty = False
        return None if empty else builder

    async def _prepare(
        self, session: ConversationSession, message: str, *, incoming: bool = True
    ):
        def build():
            # Touch the user so sync tool code can use ctx.user.
            _ = session.user
            self._refresh_instructions(session)
            skillset = self.skillset(session)
            return [skillset], self.context_builder(
                session, message, skillset, incoming=incoming
            )

        return await sync_to_async(build, thread_sensitive=True)()

    def _refresh_instructions(self, session: ConversationSession) -> None:
        """Keep a chat's instructions current with agents.md and bot.yaml."""
        meta = session.metadata or {}
        if meta.get("custom_instructions"):
            return
        wanted = self.instructions_for(self.chat_name(session))
        if session.pk is not None and session.system_prompt != wanted:
            session.system_prompt = wanted
            ConversationSession.objects.filter(pk=session.pk).update(
                system_prompt=wanted
            )

    # -- turns -------------------------------------------------------------

    def reply_spec(
        self, toolkits: list[Toolkit], session: ConversationSession | None = None
    ):
        spec = chat_reply_spec(toolkits, max_turns=self.definition.max_turns)
        # Toolkits pre-seed what every session should start knowing; window
        # sessions need user-defined seeds again on each turn.
        spec.pre_seed_each_turn = session is not None and self.is_window(session)
        return spec

    async def ask(  # noqa: PLR0913
        self,
        session: ConversationSession,
        message: str,
        *,
        attachments: list[Attachment] | None = None,
        thread_message: ThreadMessage | None = None,
        author: dict | None = None,
        control: TurnControl | None = None,
    ) -> TurnResult:
        """Answer one message with a ChatReply. Plugins see before/after hooks.

        With ``thread_message`` (another session's message, see
        ``django_ergo.bots.messaging``) the reply is routed back to its sender.
        ``control`` steers or stops the turn between steps (see
        ``conversation.structured``).
        ``author`` identifies the actual external sender; otherwise ordinary
        human text belongs to the session's Django user. Delegated messages
        carry their original author and origin separately from their raw body.
        """
        metadata = {}
        if thread_message is not None:
            metadata["thread_message"] = str(thread_message.id)
            author, provenance = await sync_to_async(
                thread_message_identity, thread_sensitive=True
            )(thread_message)
            if provenance:
                message = thread_message.text
                metadata["message_provenance"] = provenance
            elif (thread_message.metadata or {}).get("worker") or (
                thread_message.metadata or {}
            ).get("schedule"):
                meta = thread_message.metadata or {}
                author = {
                    "kind": "system",
                    "ref": str(meta.get("worker") or meta.get("schedule") or ""),
                    "display_name": "Worker"
                    if meta.get("worker")
                    else "Scheduled message",
                }
            else:
                message = thread_message.text
                author = author or None
        if author is not None:
            metadata["message_author"] = author
        for plugin in self.plugins:
            await _maybe_await(plugin.before_turn(session, message))
        toolkits, builder = await self._prepare(session, message)
        await sync_to_async(self.route, thread_sensitive=True)(session)
        outcome = await run_structured_call(
            self.reply_spec(toolkits, session),
            message,
            session=session,
            engine=self.make_engine(session),
            attachments=attachments,
            context_builder=builder,
            allow_approvals=True,
            control=control,
            metadata=metadata,
        )
        result = TurnResult.from_call(session, outcome)
        for plugin in self.plugins:
            await _maybe_await(plugin.after_turn(session, message, result))
        await sync_to_async(messaging.finish_turn, thread_sensitive=True)(
            self, session, result
        )
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
        self,
        session: ConversationSession,
        decisions: dict[str, bool] | bool,
        *,
        control: TurnControl | None = None,
    ) -> TurnResult:
        """Continue a turn that stopped for approval.

        ``decisions`` maps tool_use_id to approve/deny, or is one bool for
        every pending tool call. ``control`` works as in ``ask``.
        """
        call = await self.pending_call(session)
        if call is None:
            return TurnResult(session=session)
        if isinstance(decisions, bool):
            pending = (call.metadata or {}).get("pending_approvals", [])
            decisions = {item["id"]: decisions for item in pending}
        toolkits, builder = await self._prepare(session, "", incoming=False)
        await sync_to_async(self.route, thread_sensitive=True)(session)
        try:
            outcome = await resume_structured_call(
                self.reply_spec(toolkits),
                call,
                decisions,
                engine=self.make_engine(session),
                context_builder=builder,
                control=control,
            )
        except StructuredCallError:
            return TurnResult(session=session)  # someone else answered it first
        result = TurnResult.from_call(session, outcome)
        for plugin in self.plugins:
            await _maybe_await(plugin.after_turn(session, call.request, result))
        await sync_to_async(messaging.finish_turn, thread_sensitive=True)(
            self, session, result
        )
        return result

    async def serve(self) -> None:
        """Run every plugin's long-running ``serve`` (e.g. chat channels)."""
        await asyncio.gather(*(plugin.serve() for plugin in self.plugins))


def _as_list(made) -> list:
    if made is None:
        return []
    return list(made) if isinstance(made, list | tuple) else [made]
