"""The ``introspection`` skill: a bot reads its own config, files and code.

Every bot loaded from a folder has it, whether or not it can change its
repository (that's the ``bot_management`` plugin). Nothing here writes.

- ``ergo_self_overview``: what the bot is made of: folder, model, skills and
  where each comes from, plugins, tables, schedules, chats, sub-bots.
- ``ergo_self_files``: files in the bot folder, or in Ergo itself (``ergo:``).
- ``ergo_self_read``: one of those files, by line range.

Paths are relative to the bot folder. ``ergo:<path>`` reads Ergo's own source
(the installed ``django_ergo`` package), e.g. ``ergo:plugins/attachments.py``,
so a bot can see how a built-in or plugin tool works. Hidden files and
folders (``.env``, ``.git``) are never listed or read.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot

ERGO_PREFIX = "ergo:"
SKIP_DIRS = {"__pycache__", "node_modules"}
MAX_LINES = 400
MAX_FILES = 500
SECRET_WORDS = ("key", "token", "secret", "password")


def ergo_root() -> Path:
    import django_ergo

    return Path(django_ergo.__file__).resolve().parent


def resolve(bot: Bot, path: str) -> tuple[Path, Path]:
    """(root, file) for a path in the bot folder or, with ``ergo:``, in Ergo."""
    path = str(path or ".").strip()
    in_ergo = path.startswith(ERGO_PREFIX)
    if in_ergo:
        root = ergo_root()
        path = path[len(ERGO_PREFIX) :] or "."
    else:
        if bot.definition.root_dir is None:
            msg = "This bot wasn't loaded from a folder"
            raise ValueError(msg)
        root = Path(bot.definition.root_dir).resolve()
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root):
        msg = f"{path} is outside the bot folder"
        raise ValueError(msg)
    if any(part.startswith(".") for part in target.relative_to(root).parts):
        msg = f"{path} is hidden"
        raise ValueError(msg)
    if not target.exists():
        raise ValueError(missing(root, path, in_ergo=in_ergo))
    return root, target


def missing(root: Path, path: str, *, in_ergo: bool) -> str:
    """Say where paths start, and suggest the path without the folder's own
    name (a bot often writes "design/tools/x.py" for its "tools/x.py")."""
    if in_ergo:
        return f"ergo:{path} doesn't exist; list Ergo's source with ergo_self_files('ergo:')"
    msg = (
        f"{path} doesn't exist. Paths are relative to your bot folder "
        f"({root.name}/), e.g. 'tools/x.py'"
    )
    first, _, rest = path.strip("/").partition("/")
    if first == root.name and rest and (root / rest).exists():
        msg += f"; did you mean '{rest}'?"
    return msg


def list_files(bot: Bot, path: str = ".") -> str:
    root, base = resolve(bot, path)
    if base.is_file():
        return str(base.relative_to(root))
    lines = []
    for file in sorted(base.rglob("*")):
        rel = file.relative_to(root)
        if not file.is_file() or any(
            p.startswith(".") or p in SKIP_DIRS for p in rel.parts
        ):
            continue
        if file.suffix == ".pyc":
            continue
        lines.append(f"{rel} ({file.stat().st_size} bytes)")
        if len(lines) >= MAX_FILES:
            lines.append(f"[stopped at {MAX_FILES} files; list a subfolder]")
            break
    return "\n".join(lines) or "(no files)"


def read_file(
    bot: Bot, path: str, start_line: int = 1, max_lines: int = MAX_LINES
) -> str:
    _root, target = resolve(bot, path)
    if target.is_dir():
        msg = f"{path} is a folder; list it with ergo_self_files"
        raise ValueError(msg)
    try:
        text = target.read_text()
    except UnicodeDecodeError:
        return f"{path} is a binary file ({target.stat().st_size} bytes)"
    lines = text.splitlines()
    start = max(1, int(start_line))
    count = max(1, min(int(max_lines), MAX_LINES))
    chunk = lines[start - 1 : start - 1 + count]
    end = start + len(chunk) - 1
    header = f"{path} lines {start}-{end} of {len(lines)}"
    if end < len(lines):
        header += f" (read on with start_line={end + 1})"
    return header + "\n" + "\n".join(chunk)


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[hidden]"
            if any(w in str(k).lower() for w in SECRET_WORDS)
            and not str(k).lower().endswith("_env")
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def overview(bot: Bot) -> dict:
    from django_ergo.bots import messaging

    definition = bot.definition
    root = definition.root_dir
    sub_bots = sorted(
        name
        for name, other in messaging.KNOWN_BOTS.items()
        if getattr(other, "parent_name", "") == bot.name
    )
    return {
        "name": bot.name,
        "description": definition.description,
        "folder": str(root) if root else None,
        "parent_bot": getattr(bot, "parent_name", "") or None,
        "sub_bots": sub_bots,
        "model": bot.model_ref() or definition.engine_type or "default",
        "orchestration": definition.orchestration,
        "skills": [
            {"name": s.name, "description": s.description, "source": s.source}
            for s in bot.skill_defs
        ],
        "plugins": [
            {"name": p.name, "config": redact(p.config)} for p in definition.plugins
        ],
        "tables": [t.__name__ for t in bot.tables],
        "schedules": [s.name for s in definition.schedules],
        "chats": sorted(definition.chats),
        "read_more": "ergo_self_read('bot.yaml'), ergo_self_read('agents.md'), "
        "ergo_self_files() for the folder, ergo_self_files('ergo:') for Ergo's code",
    }


def introspection_toolkit(bot: Bot, ctx):
    """The ``introspection`` skill's tools."""
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool

    @bot_tool(name="ergo_self_overview")
    def self_overview() -> dict:
        """What you are made of: your folder, model, skills (and the file or plugin each comes from), plugins, tables, schedules, chats and sub-bots."""
        return overview(bot)

    @bot_tool(name="ergo_self_files")
    def self_files(path: str = ".") -> str:
        """List files in your bot folder (bot.yaml, agents.md, tools/, skills/, kb/, pages/...), or under a subfolder. Start the path with ergo: to list Ergo's own source instead, e.g. "ergo:plugins"."""
        return list_files(bot, path)

    @bot_tool(name="ergo_self_read")
    def self_read(path: str, start_line: int = 1, max_lines: int = MAX_LINES) -> str:
        """Read a file from your bot folder (e.g. "bot.yaml", "tools/x.py"), or Ergo's source with an ergo: path (e.g. "ergo:plugins/attachments.py"). Long files come in pieces of up to 400 lines; pass start_line to read on."""
        return read_file(bot, path, start_line, max_lines)

    return FunctionToolkit(
        [fn.__bot_tool__ for fn in (self_overview, self_files, self_read)], ctx
    )
