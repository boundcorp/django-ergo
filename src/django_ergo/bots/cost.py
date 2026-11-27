"""Per-turn context cost report: where a turn's context goes, before any change.

Read only: no model call and no provider token count. ``turn_cost(bot, session)``
returns rows for what a chat turn carries:

- ``system``: the stored instructions, plus the fixed chat-reply instructions.
- ``tools``: tool schemas, split into the skill-loader tools and each loaded
  skill (JSON of the Claude-shaped schemas; other engines render differently).
- ``context``: the per-turn context sections (skills listing, bot context
  functions, loaded skills' context), after the builder's token budget.
- ``history``: the session's stored messages by block kind, and the largest tool
  results by tool. Stored messages, before compaction or windowing, so this can
  overstate what a given engine actually sends.
- ``skills_on_load``: what each *unloaded* skill would add when loaded.

Every number is one of three kinds, never mixed:

- ``chars``: an exact character count of the text measured. A proxy, not tokens.
- ``est_tokens``: ``chars / 4`` rounded up (``context.estimate_tokens``), an
  estimate with no tokenizer behind it.
- ``provider_reported``: input-token counts a provider returned on a stored
  assistant message, measured by that provider for that earlier request. It is
  the only token figure here that is not an estimate, and it is not
  decomposed by part.

Rendering context functions runs bot code: use a throwaway session (the
``ergo_bot_cost`` command does, inside a rolled-back transaction).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django_ergo.conversation.context import estimate_tokens

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.conversation.models import ConversationSession

LEGEND = {
    "chars": "exact character count of the measured text; a proxy, not tokens",
    "est_tokens": "chars/4 rounded up; an estimate, not a tokenizer count",
    "provider_reported": "input tokens a provider returned for an earlier request; "
    "measured by the provider, not split by part",
    "tools_shape": "Claude tool-schema JSON; other engines serialize differently",
    "history_scope": "stored messages before compaction or windowing; "
    "may overstate what an engine sends",
}
LARGEST_RESULTS = 5


def _row(part: str, name: str, text: str, **extra) -> dict:
    return {
        "part": part,
        "name": name,
        "chars": len(text),
        "est_tokens": estimate_tokens(text),
        **extra,
    }


def _schema_text(schemas: list[dict]) -> str:
    return json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))


def _system_rows(bot: Bot, session: ConversationSession) -> list[dict]:
    from django_ergo.conversation.chat_reply import CHAT_REPLY_INSTRUCTIONS

    stored = session.system_prompt or bot.instructions_for(bot.chat_name(session)) or ""
    return [
        _row("system", "instructions", stored),
        _row("system", "chat_reply_instructions", CHAT_REPLY_INSTRUCTIONS),
    ]


def _skill_rows(skills) -> tuple[list[dict], list[dict]]:
    """Tool-schema rows for what is loaded now, and the cost of loading the rest."""
    from django_ergo.conversation.adapters import ClaudeToolAdapter

    adapter = ClaudeToolAdapter()
    tools = [
        _row(
            "tools",
            "skill loader tools",
            _schema_text(skills._loader_schemas(adapter)),  # noqa: SLF001
            tool_count=3,
            loaded=True,
        )
    ]
    on_load = []
    for name, skill in skills._listable_skills():  # noqa: SLF001
        schemas = [
            s for kit in skills.toolkits_of(name) for s in kit.get_tools_schema(adapter)
        ]
        loaded = skills.is_loaded(name)
        sections = [
            section
            for source in skills._context_of(name, "")  # noqa: SLF001
            if (section := source.render(4000)) is not None
        ]
        schema_text = _schema_text(schemas) if schemas else ""
        row = {
            "name": name,
            "loaded": loaded,
            "always_loaded": name in skills.always,
            "tool_count": len(schemas),
            "instructions": _row("skill", name, skill.instructions or ""),
            "tool_schemas": _row("skill", name, schema_text),
            "context": _row("skill", name, "\n".join(s.body for s in sections)),
        }
        on_load.append(row)
        if loaded and schemas:
            tools.append(
                _row("tools", name, schema_text, tool_count=len(schemas), loaded=True)
            )
    return tools, on_load


def _history(session: ConversationSession) -> dict:
    from django_ergo.conversation.models import SessionMessage

    kinds = {
        k: {"chars": 0, "count": 0}
        for k in ("user_text", "assistant_text", "thinking", "tool_use", "tool_result")
    }
    names: dict[str, str] = {}
    by_tool: dict[str, dict] = {}
    messages = 0
    last_reported = None
    qs = SessionMessage.objects.filter(session=session).prefetch_related(
        "content_blocks"
    )
    for message in qs:
        messages += 1
        if message.role == "assistant" and message.input_tokens is not None:
            last_reported = message
        for block in message.content_blocks.all():
            if block.block_type == "text":
                kind, size = f"{message.role}_text", len(block.text or "")
            elif block.block_type == "thinking":
                kind, size = "thinking", len(block.thinking or "")
            elif block.block_type == "tool_use":
                names[block.tool_use_id or ""] = block.tool_name or "?"
                kind = "tool_use"
                size = len(json.dumps(block.tool_input, ensure_ascii=False))
            else:
                kind = "tool_result"
                size = len(json.dumps(block.tool_result_content, ensure_ascii=False))
                tool = names.get(block.tool_result_for or "", "unknown")
                entry = by_tool.setdefault(tool, {"chars": 0, "count": 0, "max": 0})
                entry["chars"] += size
                entry["count"] += 1
                entry["max"] = max(entry["max"], size)
            kinds[kind]["chars"] += size
            kinds[kind]["count"] += 1
    for entry in kinds.values():
        entry["est_tokens"] = (entry["chars"] + 3) // 4
    results = sorted(by_tool.items(), key=lambda item: -item[1]["chars"])
    reported = None
    if last_reported is not None:
        reported = {
            "sequence": last_reported.sequence,
            "model": last_reported.model_name,
            "input_tokens": last_reported.input_tokens,
            "cache_read_input_tokens": last_reported.cache_read_input_tokens,
            "cache_creation_input_tokens": last_reported.cache_creation_input_tokens,
            "basis": "provider_reported",
        }
    return {
        "messages": messages,
        "by_block": kinds,
        "tool_results_by_tool": [
            {"tool": tool, **stats} for tool, stats in results[:LARGEST_RESULTS]
        ],
        "provider_reported_last_request": reported,
    }


def turn_cost(bot: Bot, session: ConversationSession, message: str = "") -> dict:
    """Context cost of one turn in ``session``. See the module docstring for what
    each number means; nothing here calls a model."""
    skills = bot.skillset(session)
    system = _system_rows(bot, session)
    tools, on_load = _skill_rows(skills)
    built = bot.context_builder(session, message, skills, incoming=False).build()
    context = [
        _row("context", section.title, section.body, complete=section.complete)
        for section in built.sections
    ]
    history = _history(session)
    fixed = [*system, *tools, *context]
    history_chars = sum(v["chars"] for v in history["by_block"].values())
    total_chars = sum(r["chars"] for r in fixed) + history_chars
    return {
        "bot": bot.name,
        "session": str(session.pk),
        "legend": LEGEND,
        "system": system,
        "tools": tools,
        "context": context,
        "context_budget_tokens": built.budget_tokens,
        "history": history,
        "skills_on_load": on_load,
        "totals": {
            "chars": total_chars,
            "est_tokens": (total_chars + 3) // 4,
            "basis": "chars exact for the measured text; est_tokens = chars/4 estimate",
            "excludes": [
                "provider framing and tokenizer overhead",
                "images and attachment blocks",
                "engine-side compaction or windowing",
            ],
        },
    }


def render_text(report: dict) -> str:
    """The report as plain text, each number tagged with what it is."""
    lines = [
        f"Context cost: {report['bot']} (session {report['session']})",
        "chars = exact characters (proxy); ~tok = chars/4 estimate; "
        "reported = provider-measured",
        "",
    ]
    for title, rows in (
        ("System", report["system"]),
        ("Tool schemas (Claude shape)", report["tools"]),
        ("Context sections", report["context"]),
    ):
        lines.append(title)
        lines += [
            f"  {r['name']}: {r['chars']} chars, ~{r['est_tokens']} tok" for r in rows
        ]
    lines.append("History (stored, before compaction)")
    history = report["history"]
    lines += [
        f"  {kind}: {v['chars']} chars in {v['count']} blocks, ~{v['est_tokens']} tok"
        for kind, v in history["by_block"].items()
    ]
    lines += [
        f"  largest tool results: {r['tool']} {r['chars']} chars in {r['count']}"
        for r in history["tool_results_by_tool"]
    ]
    if reported := history["provider_reported_last_request"]:
        lines.append(
            f"  reported: last request had {reported['input_tokens']} input tokens "
            f"(message {reported['sequence']}, {reported['model']})"
        )
    lines.append("Skills when loaded (instructions / tool schemas / context)")
    lines += [
        f"  {s['name']}{' [loaded]' if s['loaded'] else ''}: "
        f"{s['instructions']['chars']} / {s['tool_schemas']['chars']} / "
        f"{s['context']['chars']} chars, {s['tool_count']} tools"
        for s in report["skills_on_load"]
    ]
    totals = report["totals"]
    lines.append(
        f"Total measured: {totals['chars']} chars, ~{totals['est_tokens']} tok (estimate)"
    )
    return "\n".join(lines)
