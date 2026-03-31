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
    from django_ergo.conversation.toolkits import ChatWithHistoryToolkit


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


async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: ChatWithHistoryToolkit | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    adapter = engine.get_tool_adapter()
    async for response in engine.send(session, message):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)

            # Check extra_tools first (e.g., history toolkit)
            if extra_tools and extra_tools.has_tool(name):
                result = extra_tools.execute_tool(name, args)
                async for continuation in engine.submit_tool_result(
                    session, response.tool_use["id"], result
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
                session, response.tool_use["id"], result
            ):
                yield continuation
        else:
            yield response
