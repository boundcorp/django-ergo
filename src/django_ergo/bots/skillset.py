"""Skills: one way to give a bot instructions and tools, loaded when needed.

Everything a bot can do beyond answering is a skill:

- a skill folder (``skills/<name>/SKILL.md``, optionally with ``tools.py``),
- a tool file listed in bot.yaml (``tools/tandoor.py`` is the ``tandoor`` skill),
- a plugin that adds tools (``orca``, ``bash``, ``attachments``, ``ergo_kb``...),
- built-ins: ``history`` (reading past conversations) and ``orchestration``
  (messaging threads and other bots).

A chat starts with a listing of the bot's skills (pre-seeded, so the model
sees it before answering). ``ergo_skill_load`` returns a skill's
instructions and offers its tools from the next model call on, in the same
turn. A skill whose tools go ``unload_after_turns`` turns unused (default 30,
counting every turn in the chat) is dropped again; ``ergo_skill_unload``
drops one sooner. Loaded skills are kept per chat in the session's metadata.

``history`` is always loaded, and so are the skills a chat lists in bot.yaml
(``chats: {main: {skills: [...]}}``, ``threads: {skills: [...]}``) and skill
folders marked ``always_load``. Loading a skill also loads the skills it
``requires`` (front matter, or ``skills: {requires: {...}}`` in bot.yaml).

While unloaded, a plugin skill can still show a one-line hint in the
listing (e.g. "3 files in this chat"), and its instructions and context
arrive with it when it loads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.structured import PreSeedCall

logger = logging.getLogger(__name__)

LIST_TOOL = "ergo_skills_list"
LOAD_TOOL = "ergo_skill_load"
UNLOAD_TOOL = "ergo_skill_unload"
STATE_KEY = "skills"
ALWAYS = "history"


@dataclass
class SkillDef:
    """One skill of a bot. ``toolkits``, ``context`` and ``hint`` take the turn's ToolContext."""

    name: str
    description: str
    instructions: str = ""
    toolkits: Callable[[ToolContext], list[Toolkit]] | None = None
    context: Callable[[ToolContext, str], list[ContextSource]] | None = None
    hint: Callable[[ToolContext], str] | None = None
    requires: list[str] = field(default_factory=list)
    always: bool = False
    source: str = ""


class SkillSet(Toolkit):
    """A chat's skills for one turn: the loader tools, plus the tools of loaded skills."""

    def __init__(  # noqa: PLR0913
        self,
        ctx: ToolContext,
        skills: list[SkillDef],
        *,
        always: set[str] | None = None,
        unload_after_turns: int = 30,
        turn: int = 0,
        save: Callable[[dict], None] | None = None,
        state: dict | None = None,
    ):
        self.ctx = ctx
        self.skills = {s.name: s for s in skills}
        self.turn = turn
        self._save = save
        self._toolkits: dict[str, list[Toolkit]] = {}
        self.always = {n for n in (always or set()) | {ALWAYS} if n in self.skills}
        self.always |= {s.name for s in skills if s.always}
        self.always = self.closure(self.always)
        loaded = {k: int(v) for k, v in (state or {}).items() if k in self.skills}
        if unload_after_turns > 0:
            stale = [
                k for k, last in loaded.items() if turn - last > unload_after_turns
            ]
            for name in stale:
                loaded.pop(name)
            self.loaded = loaded
            if stale:
                self.persist()
        else:
            self.loaded = loaded

    # -- state -------------------------------------------------------------

    def closure(self, names: set[str]) -> set[str]:
        """``names`` plus everything they require, transitively."""
        found, todo = set(), list(names)
        while todo:
            name = todo.pop()
            if name in found or name not in self.skills:
                continue
            found.add(name)
            todo.extend(self.skills[name].requires)
        return found

    def is_loaded(self, name: str) -> bool:
        return name in self.always or name in self.loaded

    @property
    def active(self) -> list[str]:
        return [n for n in self.skills if self.is_loaded(n)]

    def persist(self) -> None:
        if self._save is not None:
            try:
                self._save(dict(self.loaded))
            except Exception:
                logger.exception("Could not save loaded skills")

    def load(self, name: str) -> list[str]:
        """Load a skill and what it requires. Returns the names newly loaded."""
        if name not in self.skills:
            known = ", ".join(sorted(self.skills)) or "none"
            msg = f"No skill named {name!r}. Skills: {known}"
            raise ValueError(msg)
        added = []
        for each in sorted(self.closure({name})):
            if not self.is_loaded(each):
                added.append(each)
            if each not in self.always:
                self.loaded[each] = self.turn
        self.persist()
        return added

    def unload(self, name: str) -> str:
        if name in self.always:
            return f"{name} is always loaded in this chat."
        if self.loaded.pop(name, None) is None:
            return f"{name} wasn't loaded."
        self.persist()
        return f"Unloaded {name}; load it again any time."

    def toolkits_of(self, name: str) -> list[Toolkit]:
        if name not in self._toolkits:
            factory = self.skills[name].toolkits
            try:
                self._toolkits[name] = list(factory(self.ctx) or []) if factory else []
            except Exception:
                logger.exception("Skill %s couldn't build its tools", name)
                self._toolkits[name] = []
        return self._toolkits[name]

    def owner_of(self, tool_name: str) -> tuple[str, Toolkit] | None:
        for name in self.skills:
            for toolkit in self.toolkits_of(name):
                if toolkit.has_tool(tool_name):
                    return name, toolkit
        return None

    def tool_names(self, name: str) -> list[str]:
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        return [
            schema["name"]
            for kit in self.toolkits_of(name)
            for schema in kit.get_tools_schema(adapter)
        ]

    # -- the loader tools ------------------------------------------------------

    def listing(self) -> str:
        lines = [
            "Skills (ergo_skill_load gives you a skill's instructions and tools; "
            "load one before doing that kind of task):"
        ]
        for name, skill in self.skills.items():
            if name == ALWAYS:
                continue
            tools = self.tool_names(name)
            if not tools and not skill.instructions:
                continue
            state = "loaded" if self.is_loaded(name) else "not loaded"
            line = f"- {name} [{state}]: {skill.description or 'no description'}"
            if tools:
                line += f" ({len(tools)} tools)"
            if skill.hint is not None and not self.is_loaded(name):
                try:
                    if hint := skill.hint(self.ctx):
                        line += f" — {hint}"
                except Exception:
                    logger.exception("Skill %s hint failed", name)
            lines.append(line)
        if len(lines) == 1:
            return "This bot has no skills to load."
        return "\n".join(lines)

    def load_result(self, name: str) -> str:
        added = self.load(name)
        parts = []
        for each in [name, *[a for a in added if a != name]]:
            skill = self.skills[each]
            block = [f"# Skill: {each}"]
            if skill.instructions:
                block.append(skill.instructions)
            for source in self._context_of(each, ""):
                section = source.render(4000)
                if section is not None:
                    block.append(f"## {section.title}\n{section.body}")
            tools = self.tool_names(each)
            if tools:
                block.append("Tools now available: " + ", ".join(tools))
            parts.append("\n\n".join(block))
        return "\n\n".join(parts)

    def _context_of(self, name: str, message: str) -> list[ContextSource]:
        context = self.skills[name].context
        if context is None:
            return []
        try:
            return list(context(self.ctx, message) or [])
        except Exception:
            logger.exception("Skill %s context failed", name)
            return []

    def context_sources(self, message: str) -> list[ContextSource]:
        """The context of every loaded skill, for this turn."""
        return [
            source for name in self.active for source in self._context_of(name, message)
        ]

    def _loader_schemas(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.bots.tools import BotTool
        from django_ergo.bots.tools import FunctionToolkit

        name_param = {
            "name": {"type": "string", "description": "A skill name from the listing"}
        }
        tools = [
            BotTool(
                LIST_TOOL,
                "List this bot's skills and which are loaded.",
                lambda: None,
                {},
                [],
            ),
            BotTool(
                LOAD_TOOL,
                "Load a skill: returns its instructions and makes its tools available.",
                lambda name: None,
                name_param,
                ["name"],
            ),
            BotTool(
                UNLOAD_TOOL,
                "Drop a loaded skill you no longer need.",
                lambda name: None,
                name_param,
                ["name"],
            ),
        ]
        return FunctionToolkit(tools).get_tools_schema(adapter)

    # -- Toolkit -------------------------------------------------------------

    def has_tool(self, tool_name: str) -> bool:
        return (
            tool_name in (LIST_TOOL, LOAD_TOOL, UNLOAD_TOOL)
            or self.owner_of(tool_name) is not None
        )

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        schemas = self._loader_schemas(adapter)
        for name in self.active:
            for toolkit in self.toolkits_of(name):
                schemas.extend(toolkit.get_tools_schema(adapter))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> Any:
        if tool_name == LIST_TOOL:
            return self.listing()
        if tool_name == LOAD_TOOL:
            return self.load_result(str(arguments.get("name", "")))
        if tool_name == UNLOAD_TOOL:
            return self.unload(str(arguments.get("name", "")))
        found = self.owner_of(tool_name)
        if found is None:
            msg = f"Unknown tool: {tool_name}"
            raise ValueError(msg)
        name, toolkit = found
        if name not in self.always:
            # Using a tool keeps its skill loaded (and loads it if it wasn't).
            self.loaded[name] = self.turn
            self.persist()
        return toolkit.execute_tool(tool_name, arguments)

    def requires_approval(self, tool_name: str) -> bool:
        found = self.owner_of(tool_name)
        return bool(found and found[1].requires_approval(tool_name))

    def approval_preview(self, tool_name: str, arguments: dict):
        found = self.owner_of(tool_name)
        return found[1].approval_preview(tool_name, arguments) if found else None

    def render_overview(self) -> str:
        return ""

    def pre_seeds(self) -> list[PreSeedCall]:
        from django_ergo.conversation.structured import PreSeedCall

        seeds = [PreSeedCall(LIST_TOOL, {}, lambda _arguments: self.listing())]
        for name in self.active:
            for toolkit in self.toolkits_of(name):
                seeds.extend(toolkit.pre_seeds())
        return seeds

    def get_bound_knowledgebases(self) -> list[tuple]:
        return [
            kb
            for name in self.active
            for kit in self.toolkits_of(name)
            for kb in kit.get_bound_knowledgebases()
        ]
