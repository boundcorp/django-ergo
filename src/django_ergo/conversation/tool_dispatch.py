"""Tool dispatch shared by both turn loops (``runner`` and ``structured``).

Tools come only from toolkits (``conversation.toolkit.Toolkit``): a bot's
``@bot_tool`` functions, skills, plugins and the KB and history toolkits all
are one. A tool call runs in the first toolkit that has it, and any exception
it raises goes back to the model as an error result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING
from typing import Any

from asgiref.sync import sync_to_async

from django_ergo.signals import tool_call_finished

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit


log = logging.getLogger(__name__)

# Only the dispatcher that durably acquired a claim can enter that invocation.
current_tool_invocation = ContextVar("current_tool_invocation", default=None)


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

    def __init__(
        self,
        message: str,
        *,
        category: str = "invalid_argument",
        retryable: bool = False,
        state_changed: str = "no",
        next_action: str = "",
        **fields: Any,
    ) -> None:
        if retryable and state_changed != "no":
            msg = "retryable requires state_changed='no'"
            raise ValueError(msg)
        super().__init__(message)
        self.fields = fields
        self.category = category
        self.retryable = retryable
        self.state_changed = state_changed
        self.next_action = next_action

    def as_dict(self) -> dict:
        return {
            **self.fields,
            "error": str(self),
            "category": self.category,
            "retryable": self.retryable,
            "state_changed": self.state_changed,
            "next_action": self.next_action,
        }


def dispatch_tool(name: str, args: dict, toolkits: list[Toolkit]) -> tuple[Any, bool]:
    """Run one tool call. Returns (result, is_error); never raises for tool failures."""
    toolkit = find_toolkit(toolkits, name)
    if toolkit is None:
        _tool_finished("unknown", is_error=True, duration=0.0)
        return f"Unknown tool: {name}", True
    start = time.perf_counter()
    result, is_error = _execute(toolkit, name, args)
    _tool_finished(name, is_error=is_error, duration=time.perf_counter() - start)
    return result, is_error


def _execute(toolkit: Toolkit, name: str, args: dict) -> tuple[Any, bool]:
    try:
        return toolkit.execute_tool(name, args), False
    except ToolError as e:
        return json.dumps(e.as_dict(), ensure_ascii=False), True
    except PermissionError:
        error = ToolError(
            "Permission denied",
            category="permission_denied",
            next_action="Stop; do not route around access control.",
        )
    except (TimeoutError, ConnectionError):
        error = ToolError(
            "Upstream connection failed; the outcome is unknown",
            category="upstream_unavailable",
            state_changed="unknown",
        )
    except ValueError as e:
        return str(e), True
    except Exception as e:  # Hide internals from the model.
        # requests is optional; a raised requests exception implies it is installed.
        try:
            import requests
        except ImportError:
            requests = None
        if requests is not None and isinstance(e, requests.RequestException):
            error = ToolError(
                "Upstream request failed; the outcome is unknown",
                category="upstream_unavailable",
                state_changed="unknown",
            )
            return json.dumps(error.as_dict(), ensure_ascii=False), True
        log.exception("Tool %s failed", name)
        error = ToolError(
            f"{name} failed with an internal error ({type(e).__name__}); the outcome is unknown",
            category="internal",
            state_changed="unknown",
        )
    return json.dumps(error.as_dict(), ensure_ascii=False), True


def _tool_finished(name: str, *, is_error: bool, duration: float) -> None:
    tool_call_finished.send_robust(
        sender=None, tool=name, is_error=is_error, duration=duration
    )


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

    token = current_tool_invocation.set((str(call.pk), tool_id, signature))
    try:
        result, is_error = dispatch_tool(name, args, toolkits)
    finally:
        current_tool_invocation.reset(token)
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
