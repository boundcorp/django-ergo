"""An opt-in hard budget for one model turn.

Without a budget nothing here runs and a turn behaves as it always did. With
one (``StructuredCallSpec.budget``, built by ``apply_budget``) the turn runner
enforces, server-side and per request:

- ``max_turns``: model requests in the turn.
- ``max_input_tokens`` / ``max_output_tokens``: totals over the turn. Output is
  held by the provider (every request is sent with ``max_tokens`` no larger
  than what is left, reasoning included). Input can't be held by a provider,
  so before each request the engine refuses to send a payload whose UTF-8
  size (plus ``INPUT_OVERHEAD_TOKENS`` for what a provider adds) exceeds what
  is left: no tokenizer makes fewer than one token per byte, so that is an
  upper bound, and the provider-reported usage of every finished request is
  what counts toward the total.
- ``deadline_seconds``: wall clock from the start of the turn. It is the
  request timeout of each model request, with an asyncio backstop, and is
  checked before every request and after every tool step. A tool that is
  already running can't be interrupted, so allow only tools that return
  promptly.
- ``allowed_tools``: the only tools the model is offered or may run. Anything
  else is refused. Tools that need approval are refused too (a budgeted turn
  never pauses for a person, so there is nothing to resume without the budget).
- No worker or agent starts, no thread sends (``forbid``), checked where those
  happen, so even an allowed tool can't do them.
- No automatic retries: neither this library's nor the SDK's.

Engines opt in with ``supports_request_limits``; a turn on one that can't hold
the limits (the subscription CLIs) fails before any model request. When a limit
is hit the turn stops cleanly: a tool call the model already made is answered
as not run, and the call ends ``failed`` with ``error_category == "budget"``,
an error that says what was reached and what was used, and the numbers in
``call.metadata["budget"]``. The budget travels with a thread message in its
metadata (``attach`` / ``from_metadata``).
"""

from __future__ import annotations

import asyncio
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.tool_dispatch import ToolError
from django_ergo.conversation.tool_dispatch import collect_toolkit_schemas
from django_ergo.conversation.tool_dispatch import find_toolkit
from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.models import StructuredCall
    from django_ergo.conversation.structured import PreSeedCall
    from django_ergo.conversation.structured import StructuredCallSpec

METADATA_KEY = "turn_budget"
ERROR_CATEGORY = "budget"
# Errors this library writes itself (no model or exception text in them), so a
# caller may relay them as the reason a budgeted request didn't finish.
STOPPED_PREFIX = "Stopped by the request budget"
NOT_RUN_PREFIX = "Not run:"
# Tokens a provider adds around a request that the payload's bytes don't show
# (chat template, tool-use preamble). Generous: the JSON syntax of every message
# and schema already counts as bytes the model never sees.
INPUT_OVERHEAD_TOKENS = 1000
# How long past the request timeout the asyncio backstop waits.
DEADLINE_GRACE_SECONDS = 2.0
_FIELDS = {
    "max_turns",
    "max_input_tokens",
    "max_output_tokens",
    "deadline_seconds",
    "allowed_tools",
    "model",
    "expires_at",
}


class BudgetExceeded(Exception):  # noqa: N818 -- a stop, not an error of the caller
    """A request, tool call or start would break the active budget.

    ``limit`` names it: max_turns, input_tokens, output_tokens, deadline,
    engine, model, action or expired.
    """

    def __init__(self, message: str, *, limit: str):
        super().__init__(message)
        self.limit = limit


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"Budget {name} must be a whole number of at least 1"
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class TurnBudget:
    """The limits of one budgeted turn. ``allowed_tools`` may be empty (no tools).

    ``model``: when set, the engine must be on exactly that model, so a figure
    priced for it stays true. ``expires_at`` (epoch seconds): a delegated turn
    that hasn't started by then is not run.
    """

    max_turns: int
    max_input_tokens: int
    max_output_tokens: int
    deadline_seconds: float
    allowed_tools: frozenset[str] = frozenset()
    model: str = ""
    expires_at: float | None = None

    def __post_init__(self):
        _positive_int("max_turns", self.max_turns)
        _positive_int("max_input_tokens", self.max_input_tokens)
        _positive_int("max_output_tokens", self.max_output_tokens)
        deadline = self.deadline_seconds
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, int | float)
            or deadline <= 0
        ):
            msg = "Budget deadline_seconds must be a positive number"
            raise ValueError(msg)
        if not all(isinstance(t, str) and t for t in self.allowed_tools):
            msg = "Budget allowed_tools must be tool names"
            raise ValueError(msg)
        if not isinstance(self.model, str):
            msg = "Budget model must be a string"
            raise ValueError(msg)  # noqa: TRY004
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int | float)
        ):
            msg = "Budget expires_at must be a time in epoch seconds"
            raise ValueError(msg)

    def as_dict(self) -> dict:
        data = {
            "max_turns": self.max_turns,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deadline_seconds": self.deadline_seconds,
            "allowed_tools": sorted(self.allowed_tools),
            "model": self.model,
        }
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at
        return data

    @classmethod
    def from_dict(cls, data: Any) -> TurnBudget:
        """Parse a stored or configured budget. Strict: an unknown key or a bad
        value raises ValueError instead of being dropped, so a malformed budget
        can never become a looser one."""
        if not isinstance(data, dict):
            msg = "A budget must be a mapping"
            raise ValueError(msg)  # noqa: TRY004
        if unknown := sorted(set(data) - _FIELDS):
            msg = f"Unknown budget keys: {', '.join(map(str, unknown))}"
            raise ValueError(msg)
        tools = data.get("allowed_tools", [])
        if isinstance(tools, str) or not isinstance(
            tools, list | tuple | set | frozenset
        ):
            msg = "Budget allowed_tools must be a list of tool names"
            raise ValueError(msg)  # noqa: TRY004
        missing = [
            k
            for k in (
                "max_turns",
                "max_input_tokens",
                "max_output_tokens",
                "deadline_seconds",
            )
            if k not in data
        ]
        if missing:
            msg = f"Budget is missing {', '.join(missing)}"
            raise ValueError(msg)
        return cls(
            max_turns=data["max_turns"],
            max_input_tokens=data["max_input_tokens"],
            max_output_tokens=data["max_output_tokens"],
            deadline_seconds=data["deadline_seconds"],
            allowed_tools=frozenset(tools),
            model=data.get("model", ""),
            expires_at=data.get("expires_at"),
        )


def attach(metadata: dict | None, budget: TurnBudget) -> dict:
    """``metadata`` for a thread message that must run under ``budget``."""
    return {**(metadata or {}), METADATA_KEY: budget.as_dict()}


def from_metadata(metadata: dict | None) -> TurnBudget | None:
    """The budget a thread message carries; None when it carries none.

    A budget that is present but invalid raises ValueError: the caller must not
    run the message unbudgeted."""
    if not metadata or METADATA_KEY not in metadata:
        return None
    return TurnBudget.from_dict(metadata[METADATA_KEY])


@dataclass(frozen=True)
class RequestLimits:
    """What an engine holds one model request to (set by the turn runner)."""

    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: float


def input_upper_bound(request: dict) -> int:
    """An upper bound on the input tokens of a provider request payload."""
    size = len(json.dumps(request, ensure_ascii=False, default=str).encode())
    return size + INPUT_OVERHEAD_TOKENS


def enforce_request_limits(
    limits: RequestLimits, request: dict, token_key: str
) -> dict:
    """Check a provider request against ``limits``, clamp its output tokens in place
    (``token_key``) and return the client options (no retries, a timeout) to send it with."""
    if limits.max_output_tokens < 1:
        msg = "no output tokens are left in the request budget"
        raise BudgetExceeded(msg, limit="output_tokens")
    if limits.timeout_seconds <= 0:
        msg = "no time is left in the request budget"
        raise BudgetExceeded(msg, limit="deadline")
    estimate = input_upper_bound(request)
    if estimate > limits.max_input_tokens:
        msg = (
            f"the next request could use up to {estimate} input tokens "
            f"(its size plus {INPUT_OVERHEAD_TOKENS}), over the {limits.max_input_tokens} left"
        )
        raise BudgetExceeded(msg, limit="input_tokens")
    wanted = request.get(token_key)
    request[token_key] = (
        min(wanted, limits.max_output_tokens) if wanted else limits.max_output_tokens
    )
    return {"max_retries": 0, "timeout": limits.timeout_seconds}


async def with_deadline(limits: RequestLimits, request):
    """Await a provider request for at most the time left plus a short grace.

    The client's own timeout is the first limit; this is the backstop. It sits around
    the network wait only, so cancelling it never interrupts storing a response."""
    return await asyncio.wait_for(
        request, timeout=limits.timeout_seconds + DEADLINE_GRACE_SECONDS
    )


def _is_timeout(exc: BaseException) -> bool:
    return any(
        cls.__name__ in {"TimeoutError", "APITimeoutError"} for cls in type(exc).__mro__
    )


class BudgetMeter:
    """One budgeted turn's running totals; the turn runner asks it before and after each request."""

    def __init__(self, budget: TurnBudget, clock: Callable[[], float] = time.monotonic):
        self.budget = budget
        self._clock = clock
        self.started = clock()
        self.stopped = ""
        self.refused_tools: list[str] = []
        self.violations: list[str] = []
        self.tools: Toolkit | None = None

    # -- totals ------------------------------------------------------------

    def elapsed(self) -> float:
        return self._clock() - self.started

    def remaining_seconds(self) -> float:
        return self.budget.deadline_seconds - self.elapsed()

    @staticmethod
    def input_used(call: StructuredCall) -> int:
        """Prompt tokens the provider reported for the turn, cached ones included."""
        return (
            (call.input_tokens or 0)
            + (call.cache_creation_input_tokens or 0)
            + (call.cache_read_input_tokens or 0)
        )

    def remaining(self, call: StructuredCall) -> RequestLimits:
        return RequestLimits(
            max_input_tokens=self.budget.max_input_tokens - self.input_used(call),
            max_output_tokens=self.budget.max_output_tokens - (call.output_tokens or 0),
            timeout_seconds=self.remaining_seconds(),
        )

    # -- checks ------------------------------------------------------------

    def before_request(
        self, call: StructuredCall, engine: Engine
    ) -> BudgetExceeded | None:
        """Why the turn can't make another model request, or None."""
        if not getattr(engine, "supports_request_limits", False):
            msg = f"the {getattr(engine, 'engine_type', 'configured')} engine can't hold a request to a budget"
            return BudgetExceeded(msg, limit="engine")
        wanted = self.budget.model
        if wanted and getattr(engine, "model", "") != wanted:
            msg = f"the engine is on {getattr(engine, 'model', '') or 'an unnamed model'}, not the budgeted {wanted}"
            return BudgetExceeded(msg, limit="model")
        left = self.remaining(call)
        if left.max_input_tokens < 1:
            msg = f"{self.input_used(call)} of {self.budget.max_input_tokens} input tokens used"
            return BudgetExceeded(msg, limit="input_tokens")
        if left.max_output_tokens < 1:
            msg = f"{call.output_tokens} of {self.budget.max_output_tokens} output tokens used"
            return BudgetExceeded(msg, limit="output_tokens")
        if left.timeout_seconds <= 0:
            msg = f"the {self.budget.deadline_seconds:g}-second deadline passed"
            return BudgetExceeded(msg, limit="deadline")
        return None

    def after_request(self, call: StructuredCall) -> BudgetExceeded | None:
        """Whether a finished request went past a limit: its response is not used."""
        if self.input_used(call) > self.budget.max_input_tokens:
            msg = f"{self.input_used(call)} input tokens used, over the {self.budget.max_input_tokens} allowed"
            return BudgetExceeded(msg, limit="input_tokens")
        if self.remaining_seconds() < 0:
            msg = f"the {self.budget.deadline_seconds:g}-second deadline passed during the request"
            return BudgetExceeded(msg, limit="deadline")
        return None

    async def respond(
        self, transcript, engine: Engine, call: StructuredCall, schemas, note: str
    ):
        """``transcript.respond`` once, with the engine held to what is left: no retry,
        and a deadline that raises BudgetExceeded instead of a provider timeout error."""
        limits = self.remaining(call)
        engine.request_limits = limits
        try:
            return await transcript.respond(schemas, note)
        except Exception as exc:
            if isinstance(exc, BudgetExceeded):
                # Refused by the engine before anything was sent: not a model turn used.
                call.turns_used = max(call.turns_used - 1, 0)
                raise
            if _is_timeout(exc):
                msg = f"the {self.budget.deadline_seconds:g}-second deadline passed during a request"
                raise BudgetExceeded(msg, limit="deadline") from exc
            raise
        finally:
            engine.request_limits = None

    # -- outcome -----------------------------------------------------------

    def summary(self, call: StructuredCall) -> dict:
        """What the turn used against its limits, for ``call.metadata["budget"]``."""
        return {
            "limits": self.budget.as_dict(),
            "used": {
                "model_turns": call.turns_used,
                "input_tokens": self.input_used(call),
                "output_tokens": call.output_tokens or 0,
                "seconds": round(self.elapsed(), 3),
            },
            "stopped_by": self.stopped,
            "refused_tools": sorted(
                set(self.refused_tools) | set(getattr(self.tools, "refused", []))
            ),
            "refused_actions": list(self.violations),
        }

    def explain(self, call: StructuredCall, stop: BudgetExceeded) -> str:
        """The error of a call a limit ended: what was reached, what was used, and that
        there is no final answer. Written here, not by the model."""
        used = self.summary(call)["used"]
        return (
            f"{STOPPED_PREFIX} ({stop.limit}): {stop}. Used {used['model_turns']} model turns, "
            f"{used['input_tokens']} input and {used['output_tokens']} output tokens in {used['seconds']:g}s. "
            "The request did not finish and has no final answer."
        )


def relayable(error: str) -> bool:
    """Whether ``error`` is one this library wrote for a budgeted request (see ``STOPPED_PREFIX``)."""
    return error.startswith((STOPPED_PREFIX, NOT_RUN_PREFIX))


# The meter of the budgeted turn running in this context, if any: tools run in
# a thread that inherits it, so the guards below see the turn that called them.
current_meter: ContextVar[BudgetMeter | None] = ContextVar(
    "ergo_current_budget_meter", default=None
)


def forbid(action: str) -> None:
    """Refuse ``action`` ("start a worker") when a budgeted turn is running; a no-op otherwise."""
    meter = current_meter.get()
    if meter is not None:
        meter.violations.append(action)
        msg = f"Refused: a budgeted request can't {action}."
        raise BudgetExceeded(msg, limit="action")


def budgeted(call):
    """Decorator for a coroutine that takes a ``StructuredCallSpec`` first.

    A spec with a budget runs with its allowlist applied and its meter active for the whole
    call, so ``forbid`` and the deadline cover the setup too (context, pre-seeds), not only
    the model loop. A spec without one is a pass-through: a nested call without a budget keeps
    the budget of the turn it runs inside."""

    @wraps(call)
    async def wrapper(spec, *args, **kwargs):
        if spec.budget is None:
            return await call(spec, *args, **kwargs)
        spec = allowlisted(spec)
        token = current_meter.set(BudgetMeter(spec.budget))
        try:
            return await call(spec, *args, **kwargs)
        finally:
            current_meter.reset(token)

    return wrapper


def _schema_name(schema: dict) -> str:
    return schema.get("name") or (schema.get("function") or {}).get("name") or ""


class AllowlistToolkit(Toolkit):
    """Only ``allowed`` tools of ``toolkits``: offered to the model, and run.

    A tool outside the list that does exist is refused with a message saying so
    (and recorded), not run and not reported as unknown.
    """

    def __init__(self, toolkits: list[Toolkit], allowed: frozenset[str]):
        self.toolkits = list(toolkits)
        self.allowed = frozenset(allowed)
        self.refused: list[str] = []

    def has_tool(self, tool_name: str) -> bool:
        return find_toolkit(self.toolkits, tool_name) is not None

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        return [
            s
            for s in collect_toolkit_schemas(self.toolkits, adapter)
            if _schema_name(s) in self.allowed
        ]

    def execute_tool(self, tool_name: str, arguments: dict) -> Any:
        if tool_name not in self.allowed:
            self.refused.append(tool_name)
            offered = ", ".join(sorted(self.allowed)) or "none"
            msg = f"{tool_name} is not available in this budgeted request. Tools you may use: {offered}."
            raise ToolError(msg)
        toolkit = find_toolkit(self.toolkits, tool_name)
        if toolkit is None:
            msg = f"Unknown tool: {tool_name}"
            raise ValueError(msg)
        return toolkit.execute_tool(tool_name, arguments)

    def requires_approval(self, tool_name: str) -> bool:
        toolkit = find_toolkit(self.toolkits, tool_name)
        checker = getattr(toolkit, "requires_approval", None)
        return (
            tool_name in self.allowed and callable(checker) and bool(checker(tool_name))
        )

    def render_overview(self) -> str:
        return ""

    def pre_seeds(self) -> list[PreSeedCall]:
        return [
            seed
            for kit in self.toolkits
            for seed in kit.pre_seeds()
            if seed.tool_name in self.allowed
        ]

    def get_bound_knowledgebases(self) -> list[tuple]:
        return [kb for kit in self.toolkits for kb in kit.get_bound_knowledgebases()]


def budget_note(budget: TurnBudget) -> str:
    tools = ", ".join(sorted(budget.allowed_tools)) or "none"
    return (
        "This is a budgeted background request. You have at most "
        f"{budget.max_turns} model steps, {budget.max_output_tokens} output tokens in all and "
        f"{budget.deadline_seconds:g} seconds. Tools you may use: {tools}. You can't start workers or "
        "agents, message other chats or ask for approval. Answer from what you can read now, in a "
        "few sentences, and say plainly anything you couldn't do."
    )


def apply_budget(spec: StructuredCallSpec, budget: TurnBudget) -> StructuredCallSpec:
    """Put ``spec`` under ``budget`` (see ``allowlisted`` for its tools). Returns ``spec``."""
    spec.budget = budget
    spec.wrap_up = (
        False  # the answer is the model's, so a budget stop must be reported as one
    )
    spec.system_prompt = "\n\n".join(
        p for p in (spec.system_prompt, budget_note(budget)) if p
    )
    return spec


def allowlisted(spec: StructuredCallSpec) -> StructuredCallSpec:
    """``spec`` with its toolkits narrowed to its budget's allowed tools.

    The turn runner calls this itself, so a budgeted spec can't skip the
    allowlist. Without a budget, or when already narrowed, ``spec`` is returned as is."""
    budget = spec.budget
    if budget is None or (
        len(spec.toolkits) == 1
        and isinstance(spec.toolkits[0], AllowlistToolkit)
        and spec.toolkits[0].allowed == budget.allowed_tools
    ):
        return spec
    return replace(
        spec, toolkits=[AllowlistToolkit(spec.toolkits, budget.allowed_tools)]
    )
