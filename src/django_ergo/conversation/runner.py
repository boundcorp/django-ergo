"""Conversation turn runner — tool execution loop above the engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from django_ergo.conversation.compaction import maybe_compact
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.context import ContextBuilder
    from django_ergo.conversation.toolkit import Toolkit

MAX_TOOL_ROUNDS = 30


@dataclass
class PendingApproval:
    """Yielded when a tool requires user approval before execution."""

    tool_use_id: str
    tool_name: str
    arguments: dict


def _tool_requires_approval(
    tool_name: str, workflow, toolkits: list[Toolkit] | None = None
) -> bool:
    toolkit = _find_toolkit_for_tool(toolkits or [], tool_name)
    if toolkit is not None:
        checker = getattr(toolkit, "requires_approval", None)
        return bool(checker(tool_name)) if callable(checker) else False
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
        recorder = getattr(toolkit, "record_usage", None)
        if callable(recorder):
            await sync_to_async(recorder)(str(session.pk))
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
    *,
    approved: bool = True,
) -> tuple[Any, bool]:
    """Execute a tool via toolkit or global registry. Returns (result, is_error).

    Callers check approval first; ``approved`` is passed to the registry.
    """
    toolkit = _find_toolkit_for_tool(toolkits, name)
    if toolkit is not None:
        try:
            return toolkit.execute_tool(name, args), False
        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            return str(e), True

    return (
        tool_registry.execute_tool(
            name=name, user=session.user, arguments=args, approved=approved
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
    attachments: list | None = None,
    context_builder: ContextBuilder | None = None,
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
    await maybe_compact(session, engine)

    if context_builder is not None:
        built = await sync_to_async(context_builder.build, thread_sensitive=True)()
        engine.ephemeral_context = built.text
    try:
        async for event in _run_rounds(
            engine,
            session,
            message,
            toolkits,
            adapter,
            additional_tool_schemas,
            max_rounds,
            attachments,
        ):
            yield event
    finally:
        if context_builder is not None:
            engine.ephemeral_context = ""


async def _run_rounds(  # noqa: PLR0913
    engine: Engine,
    session: ConversationSession,
    message: str,
    toolkits: list[Toolkit],
    adapter,
    additional_tool_schemas: list[dict] | None,
    max_rounds: int,
    attachments: list | None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    send_kwargs = {"additional_tools": additional_tool_schemas}
    if attachments:
        send_kwargs["attachments"] = attachments
    async for event in _drive(
        engine,
        session,
        engine.send(session, message, **send_kwargs),
        toolkits,
        adapter,
        additional_tool_schemas,
        max_rounds,
    ):
        yield event


async def _drive(  # noqa: PLR0913
    engine: Engine,
    session: ConversationSession,
    first_response,
    toolkits: list[Toolkit],
    adapter,
    additional_tool_schemas: list[dict] | None,
    max_rounds: int,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    """Run tool rounds until the model stops calling tools or approval is needed.

    Tools that need approval are not run. Their PendingApproval events are
    yielded after the rest of the batch has run and been stored, and the
    turn ends there; resume_conversation_turn() continues it.
    """
    other_events, tool_events = await _split_events(first_response)
    for event in other_events:
        yield event

    for _round in range(max_rounds):
        if not tool_events:
            break

        pending: list[PendingApproval] = []
        results: list[tuple[str, Any, bool]] = []
        for response in tool_events:
            name, args = adapter.parse_tool_call(response.tool_use)
            tool_id = response.tool_use["id"]

            if _tool_requires_approval(name, session.workflow, toolkits):
                pending.append(
                    PendingApproval(tool_use_id=tool_id, tool_name=name, arguments=args)
                )
                continue

            yield response

            result, is_error = await sync_to_async(
                _execute_tool, thread_sensitive=True
            )(name, args, toolkits, session)
            results.append((tool_id, result, is_error))

        if pending:
            if results:
                await engine.append_tool_results(session, results)
            for approval in pending:
                yield approval
            return
        if not results:
            return

        other_events, tool_events = await _split_events(
            engine.submit_tool_results_batch(
                session, results, additional_tools=additional_tool_schemas
            )
        )
        for event in other_events:
            yield event


def unresolved_tool_calls(session: ConversationSession) -> list[dict]:
    """Tool calls in the session's history that have no result yet."""
    from django_ergo.conversation.history import SessionSource

    calls: dict[str, dict] = {}
    for message in SessionSource(session).messages():
        for block in message.blocks:
            if block["type"] == "tool_use":
                calls[block.get("id")] = block
            elif block["type"] == "tool_result":
                calls.pop(block.get("tool_use_id"), None)
    return list(calls.values())


async def resume_conversation_turn(  # noqa: PLR0913
    engine: Engine,
    session: ConversationSession,
    decisions: dict[str, bool],
    extra_tools: list[Toolkit] | None = None,
    max_rounds: int = MAX_TOOL_ROUNDS,
    context_builder: ContextBuilder | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    """Continue a turn that stopped on PendingApproval.

    ``decisions`` maps tool_use_id to True (approved) or False (denied).
    Approved calls run; denied or undecided ones that need approval get an
    error result telling the model the user declined. Then the turn carries
    on as usual.
    """
    adapter = engine.get_tool_adapter()
    toolkits = extra_tools or []
    schemas = _collect_toolkit_schemas(toolkits, adapter) if toolkits else None
    calls = await sync_to_async(unresolved_tool_calls, thread_sensitive=True)(session)
    if not calls:
        return

    results: list[tuple[str, Any, bool]] = []
    for call in calls:
        name, args, tool_id = call["name"], call.get("input") or {}, call["id"]
        needs = _tool_requires_approval(name, session.workflow, toolkits)
        if needs and not decisions.get(tool_id):
            results.append((tool_id, "The user declined this tool call.", True))
            continue
        result, is_error = await sync_to_async(_execute_tool, thread_sensitive=True)(
            name, args, toolkits, session, approved=True
        )
        results.append((tool_id, result, is_error))

    if context_builder is not None:
        built = await sync_to_async(context_builder.build, thread_sensitive=True)()
        engine.ephemeral_context = built.text
    try:
        async for event in _drive(
            engine,
            session,
            engine.submit_tool_results_batch(
                session, results, additional_tools=schemas
            ),
            toolkits,
            adapter,
            schemas,
            max_rounds,
        ):
            yield event
    finally:
        if context_builder is not None:
            engine.ephemeral_context = ""
