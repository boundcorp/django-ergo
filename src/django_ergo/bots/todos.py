"""Todo lists: every chat and thread can keep a checklist of its own work.

The built-in ``todos`` skill is loaded in every chat and thread (bot.yaml
``todos: false`` leaves it out). It gives the model two tools, much like
Claude Code's TodoWrite:

- ``ergo_todo_write`` replaces the whole list (an empty list clears it),
- ``ergo_todo_update`` changes one item by its number (check it off, start it,
  reword it).

Each item is ``{"content": ..., "status": "pending" | "in_progress" |
"completed"}``. The list is kept in the session's metadata (``todos``), goes
into the model's context on every turn while it has items, and apps show it
with the chat (Ergonaut's thread view does).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.skillset import SkillDef
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ConversationSession

logger = logging.getLogger(__name__)

STATE_KEY = "todos"
STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 50
MAX_CONTENT_CHARS = 500
MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}

WRITE_DESCRIPTION = """\
Replace this chat's todo list with the given items; an empty list clears it.
Use it for work with several steps (three or more), or when the user gives
you a list of things to do: write the steps first, then keep the list current
as you go. Keep one item in_progress at a time, mark each one completed as
soon as it is done (not in a batch at the end), and add items you discover
along the way. Skip it for a single quick task or a plain question. The user
sees the list next to the chat, and you see it in your context every turn."""

UPDATE_DESCRIPTION = """\
Change one item of this chat's todo list by its number (from 1): set its
status (pending, in_progress, completed) to start or check it off, and/or
reword it. Use ergo_todo_write to add, remove or reorder items."""


def get_todos(session: ConversationSession | None) -> list[dict]:
    """The session's todo list (items with ``content`` and ``status``)."""
    if session is None:
        return []
    return list((session.metadata or {}).get(STATE_KEY) or [])


def normalize(items) -> list[dict]:
    """Checked items, or ValueError saying what's wrong."""
    if not isinstance(items, list):
        msg = "todos must be a list of {content, status} items"
        raise ValueError(msg)  # noqa: TRY004 — the model reads ValueErrors
    if len(items) > MAX_ITEMS:
        msg = f"A todo list holds at most {MAX_ITEMS} items"
        raise ValueError(msg)
    out = []
    for number, item in enumerate(items, start=1):
        if isinstance(item, str):
            item = {"content": item}  # noqa: PLW2901
        if not isinstance(item, dict):
            msg = f"Item {number} must be an object with content and status"
            raise ValueError(msg)  # noqa: TRY004
        out.append(_item(item, number))
    return out


def _item(item: dict, number: int) -> dict:
    content = " ".join(str(item.get("content") or "").split())
    if not content:
        msg = f"Item {number} needs content"
        raise ValueError(msg)
    status = str(item.get("status") or "pending").strip().lower().replace("-", "_")
    if status not in STATUSES:
        msg = f"Item {number}: status must be one of {', '.join(STATUSES)}"
        raise ValueError(msg)
    return {"content": content[:MAX_CONTENT_CHARS], "status": status}


def save_todos(session: ConversationSession, items: list[dict]) -> list[dict]:
    """Store the list on the session, keeping the rest of its metadata as it is now."""
    from django_ergo.conversation.models import ConversationSession

    items = normalize(items)
    fresh = (
        ConversationSession.objects.filter(pk=session.pk)
        .values_list("metadata", flat=True)
        .first()
    )
    metadata = {**(fresh or {}), STATE_KEY: items}
    if not items:
        metadata.pop(STATE_KEY)
    ConversationSession.objects.filter(pk=session.pk).update(metadata=metadata)
    session.metadata = metadata
    _notify(session)
    return items


def _notify(session: ConversationSession) -> None:
    """Tell live views the list changed (Ergonaut wires SESSION_NOTIFIER)."""
    from django_ergo.settings import api_settings

    notifier = api_settings.SESSION_NOTIFIER
    if notifier is None:
        return
    try:
        notifier(str(session.pk))
    except Exception:  # noqa: BLE001 — live updates are best-effort
        logger.debug("Could not notify about todos in %s", session.pk, exc_info=True)


def render(items: list[dict]) -> str:
    """The list as numbered checklist lines, with a count of what's done."""
    if not items:
        return "The todo list is empty."
    done = sum(1 for item in items if item["status"] == "completed")
    lines = [
        f"{number}. {MARKS.get(item['status'], '[ ]')} {item['content']}"
        for number, item in enumerate(items, start=1)
    ]
    return "\n".join([f"{done} of {len(items)} done:", *lines])


def todo_toolkit(ctx: ToolContext) -> FunctionToolkit:
    """The ``todos`` skill's tools for one chat."""
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import bot_tool

    @bot_tool(
        name="ergo_todo_write",
        description=WRITE_DESCRIPTION,
        parameters={
            "todos": {
                "type": "array",
                "description": "The whole list, in order",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The step, in a few words",
                        },
                        "status": {"type": "string", "enum": list(STATUSES)},
                    },
                    "required": ["content", "status"],
                },
            }
        },
    )
    def write(todos: list) -> str:
        return render(save_todos(ctx.session, todos))

    @bot_tool(
        name="ergo_todo_update",
        description=UPDATE_DESCRIPTION,
        parameters={
            "item": {"type": "integer", "description": "The item's number, from 1"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "content": {"type": "string", "description": "New wording"},
        },
        required=["item"],
    )
    def update(item: int, status: str = "", content: str = "") -> str:
        items = get_todos(ctx.session)
        if not items:
            msg = "The todo list is empty; write one with ergo_todo_write"
            raise ValueError(msg)
        try:
            index = int(item) - 1
        except (TypeError, ValueError):
            index = -1
        if not 0 <= index < len(items):
            msg = f"No item {item}; the list has {len(items)} items"
            raise ValueError(msg)
        changed = dict(items[index])
        if status:
            changed["status"] = status
        if content:
            changed["content"] = content
        items[index] = changed
        return render(save_todos(ctx.session, items))

    return FunctionToolkit([write.__bot_tool__, update.__bot_tool__], ctx)


def todo_context(ctx: ToolContext, message: str) -> list:
    """The list in the turn's context, while it has items."""
    from django_ergo.conversation.context import TextContextSource

    return [
        TextContextSource(
            "Todo list (update it with ergo_todo_update / ergo_todo_write)",
            lambda: render(items) if (items := get_todos(ctx.session)) else "",
            weight=2,
        )
    ]


def todo_skills(bot: Bot) -> list[SkillDef]:
    """The built-in ``todos`` skill, always loaded (none with bot.yaml ``todos: false``)."""
    from django_ergo.bots.skillset import SkillDef

    if not bot.definition.todos:
        return []
    return [
        SkillDef(
            "todos",
            "This chat's todo list: write the steps of multi-step work and "
            "check them off as you go",
            toolkits=lambda ctx: [todo_toolkit(ctx)],
            context=todo_context,
            always=True,
            source="built-in",
        )
    ]
