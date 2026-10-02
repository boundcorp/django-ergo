"""Skill folders: instructions (and optionally tools) a bot loads when it needs them.

A bot folder may have a ``skills/`` folder (``skills: {folder: ...}`` in
bot.yaml names another one). Each skill is a Markdown file, either
``skills/<name>.md`` or ``skills/<name>/SKILL.md``, with optional front matter::

    ---
    name: weekly-plan
    description: How to plan a week of dinners around the pantry
    requires: [tandoor]        # load these skills along with this one
    always_load: false         # load it in every chat from the start
    ---
    1. Check the pantry with view_pantry...

A folder skill can ship tools too: ``skills/<name>/tools.py`` (with
``@bot_tool`` functions) is imported when the bot loads, and its tools are
offered once the skill is loaded. See ``django_ergo.bots.skillset`` for how
skills, tool files and plugins all load the same way.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    requires: list[str] = field(default_factory=list)
    always_load: bool = False
    tools_file: Path | None = None


def _parse(path: Path, default_name: str) -> Skill:
    text = path.read_text()
    meta: dict = {}
    match = _FRONT_MATTER.match(text)
    if match:
        import yaml

        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            logger.warning("Bad front matter in %s", path)
        text = text[match.end() :]
    body = text.strip()
    description = str(meta.get("description") or "")
    if not description:
        first = next((line for line in body.splitlines() if line.strip()), "")
        description = first.lstrip("# ").strip()
    tools_file = path.parent / "tools.py" if path.name == SKILL_FILE else None
    return Skill(
        name=str(meta.get("name") or default_name),
        description=description,
        path=path,
        body=body,
        requires=[str(r) for r in meta.get("requires") or []],
        always_load=bool(meta.get("always_load", False)),
        tools_file=tools_file if tools_file and tools_file.is_file() else None,
    )


def load_skills(folder: Path | None) -> list[Skill]:
    """Every skill in ``folder``, sorted by name. A missing folder has none."""
    if folder is None or not folder.is_dir():
        return []
    skills = []
    for entry in sorted(folder.iterdir()):
        if entry.is_dir() and (entry / SKILL_FILE).is_file():
            skills.append(_parse(entry / SKILL_FILE, entry.name))
        elif entry.is_file() and entry.suffix == ".md":
            skills.append(_parse(entry, entry.stem))
    return sorted(skills, key=lambda skill: skill.name)
