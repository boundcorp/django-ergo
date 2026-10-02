"""Bot definitions: a folder with agents.md, bot.yaml and Python tool modules.

    kitchen/
      agents.md          # instructions (system prompt)
      bot.yaml           # configuration, see below
      tools/tandoor.py   # tool modules, listed explicitly in bot.yaml

bot.yaml::

    name: kitchen
    description: Household kitchen manager
    instructions: agents.md            # default
    engine:
      type: claude                     # or openai
      config: {model: claude-sonnet-4-5}
      api_key_env: KITCHEN_ANTHROPIC_KEY   # read at runtime, never stored
    root:                              # stream settings for main and named chats
      recent: 15
      budget_tokens: 8000
      granularity: conversation
    orchestration: true                # may the bot delegate at all (default true)
    timezone: America/Los_Angeles      # for the current time in context
    current_time: true                 # put the current date and time in context
    chats:
      main:                            # every user's main chat (always there)
        skills: [orchestration, tandoor]   # loaded from the start (default: orchestration)
        pins: [pages/dashboard.jhtml]      # bot-folder files pinned in this chat (see bots.pages)
      reports:                         # a named chat: one per user, its own purpose
        description: Weekly analytics
        instructions: Keep each report short.   # added to agents.md in this chat
        skills: [analytics]
    threads:                           # child threads of any chat
      skills: []
      allow_create: true               # may chats start threads of this bot?
      archive_after_days: 7            # archive threads idle this long (0 = never)
      default_compaction: {mode: stream, config: {keep_recent: 15}}
    skills:                            # see django_ergo.bots.skillset
      folder: skills                   # default
      unload_after_turns: 30           # drop a lazily loaded skill unused this long
      requires: {meal-planning: [tandoor]}
    tools: [tools/tandoor.py]
    tables: [tables.py]                # BotTable models (see django_ergo.bots.tables)
    toolkits: ["myapp.toolkits:make_toolkit"]   # factory(ctx) -> Toolkit
    plugins:
      - name: ergo_kb
        knowledgebase: Kitchen
      - name: telegram
        token_env: KITCHEN_TELEGRAM_TOKEN
    schedules:                         # see django_ergo.bots.schedules
      - {name: weekly-plan, cron: "0 17 * * sun", message: Plan next week's dinners}
    permissions:
      call_bots: [sysadmin]            # other bots this bot may message
      users: [lee]                     # who may use it in apps like Ergonaut (default: everyone)

Only files listed under ``tools`` are imported, and only from inside the bot
folder, so reading a bot definition never runs code it didn't name.

The older spellings still work: ``sessions:`` for ``threads:``, and
``skills: <folder>`` for ``skills: {folder: ...}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from django_ergo.conversation.history import Granularity
from django_ergo.conversation.models import CompactionMode

CONFIG_FILE = "bot.yaml"
DEFAULT_INSTRUCTIONS = "agents.md"


class BotDefinitionError(ValueError):
    pass


@dataclass
class PluginSpec:
    name: str
    config: dict = field(default_factory=dict)


CHAT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
MAIN = "main"


@dataclass
class ChatDefinition:
    """A chat every user has with the bot: ``main``, or a named one."""

    name: str
    description: str = ""
    instructions: str = ""  # added to the bot's instructions in this chat
    skills: list[str] = field(default_factory=list)  # loaded from the start
    pins: list[str] = field(
        default_factory=list
    )  # bot-folder files shown pinned in the chat


@dataclass
class BotDefinition:
    name: str
    root_dir: Path | None = None
    description: str = ""
    instructions: str = ""
    engine_type: str = ""
    engine_config: dict = field(default_factory=dict)
    api_key_env: str = ""
    recent: int = 15
    budget_tokens: int = 8000
    granularity: Granularity = Granularity.CONVERSATION
    orchestration: bool = True
    timezone: str = ""
    current_time: bool = True
    allow_create_sessions: bool = False
    archive_after_days: int = 7
    default_compaction_mode: str = CompactionMode.STREAM
    default_compaction_config: dict = field(default_factory=dict)
    tool_files: list[Path] = field(default_factory=list)
    table_files: list[Path] = field(default_factory=list)
    skills_dir: Path | None = None
    toolkit_factories: list[str] = field(default_factory=list)
    plugins: list[PluginSpec] = field(default_factory=list)
    call_bots: list[str] = field(default_factory=list)
    allowed_users: list[str] = field(default_factory=list)  # empty: everyone
    schedules: list = field(default_factory=list)  # bots.schedules.Schedule
    chats: dict[str, ChatDefinition] = field(default_factory=dict)  # main + named
    thread_skills: list[str] = field(default_factory=list)
    unload_after_turns: int = 30
    skill_requires: dict[str, list[str]] = field(default_factory=dict)

    def chat(self, name: str) -> ChatDefinition:
        return self.chats.get(name) or ChatDefinition(name=name)

    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict, root_dir: Path | None = None) -> BotDefinition:
        if not isinstance(data, dict):
            msg = f"{CONFIG_FILE} must be a mapping"
            raise BotDefinitionError(msg)
        name = data.get("name") or (root_dir.name if root_dir else "")
        if not name:
            msg = "Bot needs a name"
            raise BotDefinitionError(msg)

        engine = _mapping(data, "engine")
        root = _mapping(data, "root")
        sessions = {**_mapping(data, "sessions"), **_mapping(data, "threads")}
        skills_config = data.get("skills")
        if not isinstance(skills_config, dict):
            skills_config = {"folder": skills_config} if skills_config else {}
        orchestration = bool(data.get("orchestration", True))
        compaction = sessions.get("default_compaction") or {}
        mode = compaction.get("mode", CompactionMode.STREAM)
        if mode not in CompactionMode.values:
            msg = f"Unknown compaction mode {mode!r}"
            raise BotDefinitionError(msg)

        chats = _chats(data.get("chats") or {}, orchestration=orchestration)
        schedules = _schedules(data.get("schedules") or [])
        for schedule in schedules:
            for chat in {c for action in schedule.actions for c in action.chats}:
                if chat not in chats:
                    msg = f"schedules: {schedule.name} targets {chat!r}, which isn't in chats"
                    raise BotDefinitionError(msg)
        return cls(
            name=name,
            root_dir=root_dir,
            description=data.get("description", ""),
            instructions=_instructions(data, root_dir),
            engine_type=engine.get("type", ""),
            engine_config=dict(engine.get("config") or {}),
            api_key_env=engine.get("api_key_env", ""),
            recent=int(root.get("recent", 15)),
            budget_tokens=int(root.get("budget_tokens", 8000)),
            granularity=Granularity.parse(root.get("granularity")),
            orchestration=orchestration,
            timezone=str(data.get("timezone") or ""),
            current_time=bool(data.get("current_time", True)),
            allow_create_sessions=bool(sessions.get("allow_create", False)),
            archive_after_days=int(sessions.get("archive_after_days", 7) or 0),
            default_compaction_mode=mode,
            default_compaction_config=dict(compaction.get("config") or {}),
            tool_files=[_tool_path(p, root_dir) for p in data.get("tools") or []],
            table_files=[_tool_path(p, root_dir) for p in data.get("tables") or []],
            skills_dir=_inside(root_dir, str(skills_config.get("folder") or "skills"))
            if root_dir
            else None,
            toolkit_factories=list(data.get("toolkits") or []),
            plugins=[_plugin(spec) for spec in data.get("plugins") or []],
            call_bots=list(_mapping(data, "permissions").get("call_bots") or []),
            allowed_users=[
                str(u) for u in _mapping(data, "permissions").get("users") or []
            ],
            chats=chats,
            thread_skills=[str(s) for s in sessions.get("skills") or []],
            unload_after_turns=int(skills_config.get("unload_after_turns", 30) or 0),
            skill_requires={
                str(k): [str(v) for v in (vs or [])]
                for k, vs in (skills_config.get("requires") or {}).items()
            },
            schedules=schedules,
            raw=data,
        )

    @classmethod
    def load(cls, path: str | Path) -> BotDefinition:
        """Load a bot folder (or its bot.yaml)."""
        try:
            import yaml
        except ImportError as e:  # pragma: no cover - optional dependency
            msg = "Bot definitions need PyYAML: pip install 'django-ergo[bots]'"
            raise BotDefinitionError(msg) from e

        path = Path(path)
        config_path = path / CONFIG_FILE if path.is_dir() else path
        if not config_path.exists():
            msg = f"No {CONFIG_FILE} at {config_path}"
            raise BotDefinitionError(msg)
        data = yaml.safe_load(config_path.read_text()) or {}
        return cls.from_dict(data, root_dir=config_path.parent.resolve())


def _mapping(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        msg = f"{key} must be a mapping"
        raise BotDefinitionError(msg)
    return value


def _instructions(data: dict, root_dir: Path | None) -> str:
    if "instructions_text" in data:
        return str(data["instructions_text"])
    filename = data.get("instructions", DEFAULT_INSTRUCTIONS)
    if root_dir is None:
        return ""
    path = _inside(root_dir, filename)
    if not path.exists():
        if "instructions" in data:
            msg = f"Instructions file {filename} not found"
            raise BotDefinitionError(msg)
        return ""
    return path.read_text()


def _inside(root_dir: Path, relative: str) -> Path:
    path = (root_dir / relative).resolve()
    if not path.is_relative_to(root_dir.resolve()):
        msg = f"{relative} is outside the bot folder"
        raise BotDefinitionError(msg)
    return path


def _tool_path(value: Any, root_dir: Path | None) -> Path:
    if root_dir is None:
        msg = "Tool files need a bot folder"
        raise BotDefinitionError(msg)
    path = _inside(root_dir, str(value))
    if path.suffix != ".py":
        msg = f"Tool file {value} must be a .py file"
        raise BotDefinitionError(msg)
    return path


def _plugin(spec: Any) -> PluginSpec:
    if isinstance(spec, str):
        return PluginSpec(name=spec)
    if isinstance(spec, dict) and spec.get("name"):
        config = {k: v for k, v in spec.items() if k != "name"}
        return PluginSpec(name=spec["name"], config=config)
    msg = f"Invalid plugin entry: {spec!r}"
    raise BotDefinitionError(msg)


def _schedules(items: list) -> list:
    from django_ergo.bots.schedules import Schedule
    from django_ergo.bots.schedules import ScheduleError

    found, names = [], set()
    for item in items:
        try:
            schedule = Schedule.from_config(item)
        except ScheduleError as e:
            msg = f"schedules: {e}"
            raise BotDefinitionError(msg) from e
        if schedule.name in names:
            msg = f"schedules: two named {schedule.name!r}"
            raise BotDefinitionError(msg)
        names.add(schedule.name)
        found.append(schedule)
    return found


def _pin(chat: str, value: Any) -> str:
    pin = str(value).strip().lstrip("./")
    if not pin or pin.startswith("/") or ".." in pin.split("/"):
        msg = f"chats: {chat} pins {value!r}; pins are paths inside the bot folder"
        raise BotDefinitionError(msg)
    return pin


def _chats(data: dict, *, orchestration: bool) -> dict[str, ChatDefinition]:
    if not isinstance(data, dict):
        msg = "chats must map chat names to their settings"
        raise BotDefinitionError(msg)
    chats = {}
    for key, value in {MAIN: {}, **data}.items():
        name, config = str(key), value or {}
        if not CHAT_NAME.match(name):
            msg = f"chats: {name!r} must be lowercase letters, digits, - or _"
            raise BotDefinitionError(msg)
        default_skills = ["orchestration"] if name == MAIN and orchestration else []
        chats[name] = ChatDefinition(
            name=name,
            description=str(config.get("description") or ""),
            instructions=str(config.get("instructions") or ""),
            skills=[str(s) for s in config.get("skills", default_skills) or []],
            pins=[_pin(name, p) for p in config.get("pins") or []],
        )
    return chats
