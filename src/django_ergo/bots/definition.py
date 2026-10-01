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
    root:                              # the bot's root (stream) session
      recent: 15
      budget_tokens: 8000
      granularity: conversation
    orchestration: true                # thread tools on the root; false = none
    sessions:
      allow_create: true               # may the root start threads?
      default_compaction: {mode: stream, config: {keep_recent: 15}}
    tools: [tools/tandoor.py]
    toolkits: ["myapp.toolkits:make_toolkit"]   # factory(ctx) -> Toolkit
    plugins:
      - name: ergo_kb
        knowledgebase: Kitchen
      - name: telegram
        token_env: KITCHEN_TELEGRAM_TOKEN
    permissions:
      call_bots: [sysadmin]            # other bots this bot may message

Only files listed under ``tools`` are imported, and only from inside the bot
folder, so reading a bot definition never runs code it didn't name.
"""

from __future__ import annotations

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
    allow_create_sessions: bool = False
    default_compaction_mode: str = CompactionMode.STREAM
    default_compaction_config: dict = field(default_factory=dict)
    tool_files: list[Path] = field(default_factory=list)
    toolkit_factories: list[str] = field(default_factory=list)
    plugins: list[PluginSpec] = field(default_factory=list)
    call_bots: list[str] = field(default_factory=list)
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
        sessions = _mapping(data, "sessions")
        compaction = sessions.get("default_compaction") or {}
        mode = compaction.get("mode", CompactionMode.STREAM)
        if mode not in CompactionMode.values:
            msg = f"Unknown compaction mode {mode!r}"
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
            orchestration=bool(data.get("orchestration", True)),
            allow_create_sessions=bool(sessions.get("allow_create", False)),
            default_compaction_mode=mode,
            default_compaction_config=dict(compaction.get("config") or {}),
            tool_files=[_tool_path(p, root_dir) for p in data.get("tools") or []],
            toolkit_factories=list(data.get("toolkits") or []),
            plugins=[_plugin(spec) for spec in data.get("plugins") or []],
            call_bots=list(_mapping(data, "permissions").get("call_bots") or []),
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
