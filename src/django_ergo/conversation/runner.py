"""Conversation turn runner — tool execution loop above the engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit

MAX_TOOL_ROUNDS = 30


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


def _execute_tool(
    name: str,
    args: dict,
    toolkits: list[Toolkit],
    session: ConversationSession,
) -> tuple[Any, bool]:
    """Execute a tool via toolkit or global registry. Returns (result, is_error)."""
    toolkit = _find_toolkit_for_tool(toolkits, name)
    if toolkit is not None:
        try:
            return toolkit.execute_tool(name, args), False
        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            return str(e), True

    return (
        tool_registry.execute_tool(
            name=name, user=session.user, arguments=args, approved=True
        ),
        False,
    )


async def _split_events(
    event_iter,
) -> tuple[list[EngineResponse], list[EngineResponse]]:
    """Consume an event iterator, separating tool_use events from others."""
    other_events = []
    tool_events = []
    async for response in event_iter:
        if response.event_type == "tool_use":
            tool_events.append(response)
        else:
            other_events.append(response)
    return other_events, tool_events


async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: list[Toolkit] | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    """Send a message and handle multi-round tool calls until the LLM is done.

    Collects all tool_use events per round, executes them, and submits results
    as a batch before the next API call. This handles engines like OpenAI that
    require all tool results before continuing.
    """
    adapter = engine.get_tool_adapter()
    toolkits = extra_tools or []
    additional_tool_schemas = (
        _collect_toolkit_schemas(toolkits, adapter) if toolkits else None
    )
    if toolkits:
        await _record_kb_usage(session, toolkits)

    other_events, tool_events = await _split_events(
        engine.send(session, message, additional_tools=additional_tool_schemas)
    )
    for event in other_events:
        yield event

    for _round in range(max_rounds):
        if not tool_events:
            break

        has_approval = False
        results: list[tuple[str, Any, bool]] = []
        for response in tool_events:
            name, args = adapter.parse_tool_call(response.tool_use)
            tool_id = response.tool_use["id"]

            if _tool_requires_approval(name, session.workflow):
                yield PendingApproval(
                    tool_use_id=tool_id, tool_name=name, arguments=args
                )
                has_approval = True
                break

            yield response

            result, is_error = _execute_tool(name, args, toolkits, session)
            results.append((tool_id, result, is_error))

        if has_approval or not results:
            return

        other_events, tool_events = await _split_events(
            engine.submit_tool_results_batch(
                session, results, additional_tools=additional_tool_schemas
            )
        )
        for event in other_events:
            yield event
