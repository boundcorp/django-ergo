"""Conversation turn runner — tool execution loop above the engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit


@dataclass
class PendingApproval:
    """Yielded when a tool requires user approval before execution."""

    tool_use_id: str
    tool_name: str
    arguments: dict


def _tool_requires_approval(tool_name: str, workflow) -> bool:
    tool_config = tool_registry.get_tool(tool_name)
    if not tool_config or not tool_config.requires_approval:
        return False
    if workflow:
        tools_config = workflow.get_tools_config()
        approved = tools_config.get("approved_tools", [])
        if tool_name in approved:
            return False
    return True


def _collect_toolkit_schemas(
    toolkits: list[Toolkit],
    adapter,
) -> list[dict]:
    """Collect tool schemas from all toolkits for engine injection."""
    schemas = []
    for toolkit in toolkits:
        schemas.extend(toolkit.get_tools_schema(adapter))
    return schemas


def _find_toolkit_for_tool(
    toolkits: list[Toolkit],
    tool_name: str,
) -> Toolkit | None:
    """Find the first toolkit that handles the given tool name."""
    for toolkit in toolkits:
        if toolkit.has_tool(tool_name):
            return toolkit
    return None


async def _record_kb_usage(
    session: ConversationSession,
    toolkits: list[Toolkit],
) -> None:
    """Record KB usage for all toolkits bound to knowledgebases."""
    from django_ergo.conversation.models import ConversationKBUsage

    for toolkit in toolkits:
        for kb, mode in toolkit.get_bound_knowledgebases():
            await ConversationKBUsage.objects.aget_or_create(
                session=session,
                knowledgebase=kb,
                mode=mode,
            )


async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: list[Toolkit] | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    adapter = engine.get_tool_adapter()
    toolkits = extra_tools or []
    additional_tool_schemas = (
        _collect_toolkit_schemas(toolkits, adapter) if toolkits else None
    )
    if toolkits:
        await _record_kb_usage(session, toolkits)

    async for response in engine.send(
        session, message, additional_tools=additional_tool_schemas
    ):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)

            # Check toolkits first (e.g., KB toolkit, history toolkit)
            toolkit = _find_toolkit_for_tool(toolkits, name)
            if toolkit is not None:
                result = toolkit.execute_tool(name, args)
                async for continuation in engine.submit_tool_result(
                    session,
                    response.tool_use["id"],
                    result,
                    additional_tools=additional_tool_schemas,
                ):
                    yield continuation
                continue

            if _tool_requires_approval(name, session.workflow):
                yield PendingApproval(
                    tool_use_id=response.tool_use["id"], tool_name=name, arguments=args
                )
                return
            result = tool_registry.execute_tool(
                name=name, user=session.user, arguments=args, approved=True
            )
            async for continuation in engine.submit_tool_result(
                session,
                response.tool_use["id"],
                result,
                additional_tools=additional_tool_schemas,
            ):
                yield continuation
        else:
            yield response
