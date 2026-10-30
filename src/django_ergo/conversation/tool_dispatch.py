"""Tool dispatch shared by both turn loops (``runner`` and ``structured``).

Tools come only from toolkits (``conversation.toolkit.Toolkit``): a bot's
``@bot_tool`` functions, skills, plugins and the KB and history toolkits all
are one. A tool call runs in the first toolkit that has it, and any exception
it raises goes back to the model as an error result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from asgiref.sync import sync_to_async

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit


def collect_toolkit_schemas(toolkits: list[Toolkit], adapter) -> list[dict]:
    """Every toolkit's tool schemas, in the engine's format."""
    schemas = []
    for toolkit in toolkits:
        schemas.extend(toolkit.get_tools_schema(adapter))
    return schemas


def find_toolkit(toolkits: list[Toolkit], tool_name: str) -> Toolkit | None:
    """The first toolkit that has ``tool_name``."""
    for toolkit in toolkits:
        if toolkit.has_tool(tool_name):
            return toolkit
    return None


def requires_approval(tool_name: str, toolkits: list[Toolkit] | None) -> bool:
    """Whether the toolkit that has ``tool_name`` wants the user to approve the call."""
    toolkit = find_toolkit(toolkits or [], tool_name)
    checker = getattr(toolkit, "requires_approval", None)
    return bool(checker(tool_name)) if callable(checker) else False


def dispatch_tool(name: str, args: dict, toolkits: list[Toolkit]) -> tuple[Any, bool]:
    """Run one tool call. Returns (result, is_error); never raises for tool failures."""
    toolkit = find_toolkit(toolkits, name)
    if toolkit is None:
        return f"Unknown tool: {name}", True
    try:
        return toolkit.execute_tool(name, args), False
    except Exception as e:  # noqa: BLE001 — any tool failure goes back to the model
        return str(e), True


async def record_kb_usage(
    session: ConversationSession, toolkits: list[Toolkit]
) -> None:
    """Record KB usage for all toolkits bound to knowledgebases."""
    from django_ergo.conversation.models import ConversationKBUsage

    for toolkit in toolkits:
        recorder = getattr(toolkit, "record_usage", None)
        if callable(recorder):
            await sync_to_async(recorder)(str(session.pk))
        for kb, mode in toolkit.get_bound_knowledgebases():
            await ConversationKBUsage.objects.aget_or_create(
                session=session, knowledgebase=kb, mode=mode
            )
