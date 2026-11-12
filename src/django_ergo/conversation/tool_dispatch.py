"""Tool dispatch shared by both turn loops (``runner`` and ``structured``).

Tools come only from toolkits (``conversation.toolkit.Toolkit``): a bot's
``@bot_tool`` functions, skills, plugins and the KB and history toolkits all
are one. A tool call runs in the first toolkit that has it, and any exception
it raises goes back to the model as an error result.
"""

from __future__ import annotations

import hashlib
import json
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


class ToolError(ValueError):
    """A tool failure that names what it concerned.

    The model gets a JSON object (``error`` plus ``fields``) instead of bare
    text, so a client can show the target of a failed call as it does for a
    successful one.
    """

    def __init__(self, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.fields = fields


def dispatch_tool(name: str, args: dict, toolkits: list[Toolkit]) -> tuple[Any, bool]:
    """Run one tool call. Returns (result, is_error); never raises for tool failures."""
    toolkit = find_toolkit(toolkits, name)
    if toolkit is None:
        return f"Unknown tool: {name}", True
    try:
        return toolkit.execute_tool(name, args), False
    except ToolError as e:
        return json.dumps({"error": str(e), **e.fields}, ensure_ascii=False), True
    except Exception as e:  # noqa: BLE001 — any tool failure goes back to the model
        return str(e), True


def dispatch_tool_once(
    call, tool_id: str, name: str, args: dict, toolkits: list[Toolkit]
) -> tuple[Any, bool]:
    """Durably claim one provider invocation before side effects; never replay an uncertain write.

    Opt-in by the caller, not a grant of tools or approval. Approval checks still
    happen before dispatch. A crash after claim leaves an explicitly unknown outcome.
    """
    from django.db import transaction

    from django_ergo.conversation.models import StructuredCall

    signature = hashlib.sha256(
        json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with transaction.atomic():
        row = StructuredCall.objects.select_for_update().get(pk=call.pk)
        ledger = dict((row.metadata or {}).get("tool_invocations", {}))
        prior = ledger.get(tool_id)
        if prior:
            if prior["signature"] != signature:
                return (
                    "Refused: this tool invocation ID was already used with different arguments.",
                    True,
                )
            if prior["status"] == "completed":
                return prior["result"], prior["is_error"]
            return (
                "Tool invocation already claimed; outcome unknown. Do not retry the external action automatically.",
                True,
            )
        ledger[tool_id] = {"signature": signature, "status": "claimed"}
        call.metadata = {**row.metadata, "tool_invocations": ledger}
        row.metadata = call.metadata
        row.save(update_fields=["metadata"])

    result, is_error = dispatch_tool(name, args, toolkits)
    # Replay uses the same normal tool-result representation where JSON supports it.
    # Rich results retain a bounded textual receipt instead of replaying side effects.
    try:
        receipt = json.loads(json.dumps(result))
    except (TypeError, ValueError):
        receipt = str(result)
    with transaction.atomic():
        row = StructuredCall.objects.select_for_update().get(pk=call.pk)
        ledger = dict((row.metadata or {}).get("tool_invocations", {}))
        ledger[tool_id] = {
            "signature": signature,
            "status": "completed",
            "result": receipt,
            "is_error": is_error,
        }
        call.metadata = {**row.metadata, "tool_invocations": ledger}
        row.metadata = call.metadata
        row.save(update_fields=["metadata"])
    return result, is_error


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
