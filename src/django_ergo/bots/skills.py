"""Skills: instructions a bot loads only when it needs them.

A bot folder may have a ``skills/`` folder (``skills:`` in bot.yaml names
another one). Each skill is a Markdown file, either ``skills/<name>.md`` or
``skills/<name>/SKILL.md``, with optional front matter::

    ---
    name: weekly-plan
    description: How to plan a week of dinners around the pantry
    ---
    1. Check the pantry with view_pantry...

When a bot has skills it gets two tools, ``list_skills`` and ``load_skill``,
and every session starts with a ``list_skills`` result already in its
history, so the model knows the skill names and its tools without asking.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit

if TYPE_CHECKING:
    from pathlib import Path

    from django_ergo.conversation.toolkit import Toolkit

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
LIST_SKILLS = "list_skills"
LOAD_SKILL = "load_skill"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str


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
    return Skill(
        name=str(meta.get("name") or default_name),
        description=description,
        path=path,
        body=body,
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


def tool_overview(toolkits: list[Toolkit]) -> list[dict]:
    """Name and description of every tool in ``toolkits``."""
    from django_ergo.conversation.adapters import ClaudeToolAdapter

    tools = []
    for toolkit in toolkits:
        for schema in toolkit.get_tools_schema(ClaudeToolAdapter()):
            tools.append(  # noqa: PERF401
                {"name": schema["name"], "description": schema.get("description", "")}
            )
    return tools


def render_listing(skills: list[Skill], tools: list[dict]) -> str:
    lines = ["Skills (load one with load_skill before doing that kind of task):"]
    lines += [f"- {s.name}: {s.description}" for s in skills]
    if tools:
        lines += ["", "Tools:"]
        lines += [f"- {t['name']}: {t['description']}" for t in tools]
    return "\n".join(lines)


def skills_toolkit(skills: list[Skill], tools_for_listing) -> Toolkit:
    """``list_skills`` and ``load_skill`` for ``skills``.

    ``tools_for_listing()`` returns the other tools to describe in the
    listing.
    """
    by_name = {skill.name: skill for skill in skills}

    def list_skills() -> str:
        return render_listing(skills, tools_for_listing())

    def load_skill(name: str) -> str:
        skill = by_name.get(name)
        if skill is None:
            known = ", ".join(by_name) or "none"
            return f"No skill named {name!r}. Skills: {known}"
        return f"# Skill: {skill.name}\n\n{skill.body}"

    return FunctionToolkit(
        [
            BotTool(
                name=LIST_SKILLS,
                description="List this bot's skills and tools.",
                function=list_skills,
                parameters={},
                required=[],
            ),
            BotTool(
                name=LOAD_SKILL,
                description="Load a skill's instructions by name.",
                function=load_skill,
                parameters={"name": {"type": "string", "description": "Skill name"}},
                required=["name"],
            ),
        ],
        seed=[LIST_SKILLS],
    )
