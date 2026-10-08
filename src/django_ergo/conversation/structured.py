"""Structured calls: a request in, a validated response out.

A structured call sends a prompt, lets the model use tools, and ends with a
validated response. Every call is a ``StructuredCall`` row with a ``kind``.

- **Standalone** (no session): the tool loop runs in memory and is stored on
  the row as ``transcript``. ``revise_structured_call(call, "fix X")``
  replays that transcript plus the correction as a new call whose
  ``parent`` is the original.
- **In a session**: pass ``session=``. The call becomes one turn of that
  conversation. Its messages go into the session like any chat turn, the
  row records their span, and the response is added as the turn's final
  assistant message so later chat turns, history tools and context
  builders see it. Sessions can mix structured calls and ordinary chat.

Usage::

    class Plan(BaseModel):
        title: str
        steps: list[str]

    spec = StructuredCallSpec(
        kind="planner",
        system_prompt="Plan the work.",
        response_model=Plan,
        toolkits=[my_toolkit],
    )
    result = await run_structured_call(spec, "Plan X", user=user)
    result.parsed  # Plan(...)
    fixed = await revise_structured_call(spec, result.call, "Drop step 3")

    # As a turn of an existing conversation:
    result = await run_structured_call(spec, "Plan it", session=session)

Response modes:

- ``response_model`` (a Pydantic model): the model gets a ``submit_output``
  tool whose input schema is the model's JSON schema. Validation errors go
  back to the model as tool errors so it can fix them.
- ``output_parser`` (a callable on the final text, ``json.loads`` by default):
  the final assistant text is parsed; a parse failure is sent back as a
  correction message.

Turn control: pass ``control=`` (anything with an async ``check()`` returning
a ``TurnSignal``) to steer or stop a call while it runs. The loop checks it
before every model call, so after a tool step's results are in::

    class Inbox:
        async def check(self) -> TurnSignal:
            if user_pressed_stop():
                return TurnSignal(stop=True)
            return TurnSignal(messages=[SteeringMessage(t) for t in new_messages()])

    result = await run_structured_call(spec, "Plan it", session=session, control=Inbox())

- ``messages`` go into the call as ordinary user messages (with any
  attachments), so the model sees them on its next step and they stay in the
  history like any other message.
- ``stop`` ends the call with status ``stopped``. A tool that is already
  running finishes first; the call stops at the next step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import get_args
from typing import get_origin

from asgiref.sync import sync_to_async
from pydantic import BaseModel
from pydantic import ValidationError

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.compaction import maybe_compact
from django_ergo.conversation.engine import SeededToolCall
from django_ergo.conversation.identity import system_identity
from django_ergo.conversation.images import is_ref
from django_ergo.conversation.images import prepare_messages
from django_ergo.conversation.images import storable_ref
from django_ergo.conversation.messages import anext_sequence
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.runner import PendingApproval
from django_ergo.conversation.runner import _collect_toolkit_schemas
from django_ergo.conversation.runner import _find_toolkit_for_tool
from django_ergo.conversation.runner import _record_kb_usage
from django_ergo.conversation.runner import _tool_requires_approval
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.tool_results import budget_chars
from django_ergo.conversation.tool_results import trim_tool_results
from django_ergo.conversation.toolkit import ApprovalPreview
from django_ergo.conversation.toolkit import Toolkit
from django_ergo.pricing import add_request_cost
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.attachments import Attachment
    from django_ergo.conversation.context import ContextBuilder
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_TOOL = "submit_output"
# Seconds to wait before each retry of a transient API failure.
RETRY_DELAYS = (1, 2, 4)
MAX_ERROR_CHARS = 4000
STOPPED_NOTICE = "Stopped by the user"

# Matched against exception class names (and their bases) so neither SDK
# has to be importable.
_TRANSIENT_ERRORS = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "OverloadedError",
    "ConnectError",
    "ReadTimeout",
}
_ERROR_CATEGORIES = {
    "AuthenticationError": "auth",
    "PermissionDeniedError": "auth",
    "APIConnectionError": "network",
    "APITimeoutError": "network",
    "RateLimitError": "network",
    "ConnectError": "network",
    "ReadTimeout": "network",
    "BadRequestError": "model",
    "InternalServerError": "model",
    "OverloadedError": "model",
    "UnprocessableEntityError": "model",
}
_MAX_TOKENS_STOPS = {"max_tokens", "length"}

_TRUNCATED_TOOL_CALL = (
    "Not run: your reply hit the output token limit before this tool call's "
    "arguments were complete. Nothing happened. Retry with smaller arguments, "
    "for example by splitting a large file or text across several calls."
)


def _submit_nudge(tool_name: str, events) -> str:
    """Ask for the output tool after a plain-text answer, keeping that answer.

    Without the text, models often resubmit an earlier turn's answer.
    """
    text = "".join(e.text for e in events if e.event_type == "text" and e.text)
    text = text.strip()
    if not text:
        return (
            f"You must call the {tool_name} tool with your final answer to the "
            "latest message. Do not answer in plain text."
        )
    return (
        f"Your last message was plain text, which isn't delivered. Call the "
        f"{tool_name} tool now with that answer to the latest message (not an "
        f"earlier one). Your plain text was:\n\n{text[:4000]}"
    )


class StructuredCallError(RuntimeError):
    """Raised for misuse (bad spec, revising across engines), not model failures.

    Model and tool failures are recorded on the StructuredCall row.
    """


@dataclass
class PreSeedCall:
    """A tool call run by the server and written into history before the first model call.

    The model sees the result as if it had called the tool itself. A handler
    that raises is logged and skipped.
    """

    tool_name: str
    tool_input: dict
    handler: Callable[[dict], Any]


@dataclass
class StructuredCallSpec:
    kind: str
    system_prompt: str = ""
    response_model: type[BaseModel] | None = None
    output_parser: Callable[[str], Any] | None = None
    toolkits: list[Toolkit] = field(default_factory=list)
    pre_seeds: list[PreSeedCall] = field(default_factory=list)
    # Seed every turn instead of once per session, for sessions whose model
    # calls only carry the current turn natively (window chats).
    pre_seed_each_turn: bool = False
    max_turns: int = 10
    # Spend the last allowed turn on the answer: only the output tool is
    # offered, with a note to report what was done and what's left, so a long
    # turn ends with a reply instead of TURN_LIMITED.
    wrap_up: bool = False
    max_tokens: int | None = None
    output_tool_name: str = DEFAULT_OUTPUT_TOOL

    def all_pre_seeds(self) -> list[PreSeedCall]:
        """The spec's own pre-seeds, then each toolkit's."""
        return [*self.pre_seeds, *(s for kit in self.toolkits for s in kit.pre_seeds())]

    def __post_init__(self):
        if self.response_model is not None and self.output_parser is not None:
            msg = "Pass response_model or output_parser, not both"
            raise StructuredCallError(msg)

    def parse_text(self, text: str) -> Any:
        parser = self.output_parser or json.loads
        return parser(text)


@dataclass
class SteeringMessage:
    """A message from the user that arrived while the call was running."""

    text: str
    attachments: list[Attachment] | None = None


@dataclass
class TurnSignal:
    """What a TurnControl wants at a step boundary.

    With ``stop``, any ``messages`` are still added to the history, then the call stops.
    """

    stop: bool = False
    messages: list[SteeringMessage] = field(default_factory=list)


class TurnControl(Protocol):
    """Checked before each model call; see the module docstring."""

    async def check(self) -> TurnSignal: ...


@dataclass
class StructuredCallResult:
    call: StructuredCall
    parsed: Any = None
    approvals: list[PendingApproval] = field(default_factory=list)

    @property
    def session(self) -> ConversationSession | None:
        return self.call.session

    @property
    def status(self) -> str:
        return self.call.status

    @property
    def ok(self) -> bool:
        return self.call.status == StructuredCallStatus.COMPLETED

    @property
    def error(self) -> str:
        return self.call.error


def _wants_container(annotation: Any) -> bool:
    """True when a field takes a list, dict or model and never a plain str."""
    args = get_args(annotation)
    if args and str not in args and get_origin(annotation) not in (list, dict):
        # Optional[...] and other unions: look through to the members.
        return any(_wants_container(a) for a in args if a is not type(None))
    origin = get_origin(annotation) or annotation
    return origin in (list, dict) or (
        isinstance(origin, type) and issubclass(origin, BaseModel)
    )


def _decode_json_fields(response_model: type[BaseModel], arguments: dict) -> dict:
    """Unwrap list, dict and model fields the model sent as JSON strings.

    Models sometimes pass ``"[\\"a\\", \\"b\\"]"`` where the schema asks for a
    list. Decoding it here saves a validation round trip that would resend
    the whole output.
    """
    if not isinstance(arguments, dict):
        return arguments
    fixed = dict(arguments)
    for name, info in response_model.model_fields.items():
        key = info.alias or name
        value = fixed.get(key)
        if not isinstance(value, str) or not _wants_container(info.annotation):
            continue
        try:
            decoded = json.loads(value)
        except ValueError:
            continue
        if isinstance(decoded, list | dict):
            fixed[key] = decoded
    return fixed


class StructuredOutputToolkit(Toolkit):
    """Exposes one tool whose input schema is a Pydantic model.

    A valid call is kept in ``accepted``; an invalid one raises ValueError so
    the runner returns the validation error to the model.
    """

    def __init__(self, response_model: type[BaseModel], tool_name: str):
        self.response_model = response_model
        self.tool_name = tool_name
        self.accepted: BaseModel | None = None

    def has_tool(self, tool_name: str) -> bool:
        return tool_name == self.tool_name

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        schema = self.response_model.model_json_schema()
        description = (
            f"Submit your final answer as a {self.response_model.__name__}. "
            "Call this exactly once, when you are done."
        )
        if isinstance(adapter, OpenAIToolAdapter):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "description": description,
                        "parameters": schema,
                    },
                }
            ]
        return [
            {
                "name": self.tool_name,
                "description": description,
                "input_schema": schema,
            }
        ]

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        arguments = _decode_json_fields(self.response_model, arguments)
        try:
            self.accepted = self.response_model.model_validate(arguments)
        except ValidationError as e:
            msg = (
                f"Output failed validation: {e}. "
                f"Fix the errors and call {self.tool_name} again."
            )
            raise ValueError(msg) from e
        return "Output accepted."

    def render_overview(self) -> str:
        return ""


def _error_category(exc: BaseException) -> str:
    for cls in type(exc).__mro__:
        if cls.__name__ in _ERROR_CATEGORIES:
            return _ERROR_CATEGORIES[cls.__name__]
    return "other"


def _is_transient(exc: BaseException) -> bool:
    return any(cls.__name__ in _TRANSIENT_ERRORS for cls in type(exc).__mro__)


def _dispatch_tool(
    name: str,
    args: dict,
    toolkits: list[Toolkit],
    user,
    workflow,
) -> tuple[Any, bool]:
    """Run one tool call. Returns (result, is_error); never raises for tool failures."""
    toolkit = _find_toolkit_for_tool(toolkits, name)
    if toolkit is not None:
        try:
            return toolkit.execute_tool(name, args), False
        except Exception as e:  # noqa: BLE001 — any tool failure goes back to the model
            return str(e), True

    enabled = workflow.get_tools_config().get("enabled_tools", []) if workflow else []
    if name in enabled and tool_registry.get_tool(name) is not None:
        try:
            result = tool_registry.execute_tool(
                name=name, user=user, arguments=args, approved=True
            )
        except Exception as e:  # noqa: BLE001
            return str(e), True
        return result, False
    return f"Unknown tool: {name}", True


async def _record_tools(call: StructuredCall, spec: StructuredCallSpec) -> None:
    """Keep the names of the tools this call could use, for inspection later."""
    adapter = ClaudeToolAdapter()

    def names():
        # The output tool is how the call returns its result, not a tool the
        # model chooses to use, so it isn't listed.
        return [
            schema["name"]
            for toolkit in spec.toolkits
            for schema in toolkit.get_tools_schema(adapter)
        ]

    tools = await sync_to_async(names)()
    if tools:
        call.metadata = {**(call.metadata or {}), "tools": tools}
        await call.asave(update_fields=["metadata"])


async def _run_pre_seeds(pre_seeds: list[PreSeedCall]) -> list[SeededToolCall]:
    calls = []
    # Unique per run: seeds repeat in every turn that pre-seeds, and a chat's tool ids should not.
    batch = uuid.uuid4().hex[:8]
    for index, seed in enumerate(pre_seeds):
        try:
            result = await sync_to_async(seed.handler, thread_sensitive=True)(
                seed.tool_input
            )
        except Exception:  # noqa: BLE001
            log.warning("pre-seed %r raised; skipping", seed.tool_name, exc_info=True)
            continue
        calls.append(
            SeededToolCall(
                tool_use_id=f"preseed_{batch}_{index}",
                name=seed.tool_name,
                input=seed.tool_input,
                result=result,
            )
        )
    return calls


async def _with_retry(make_call):
    """Await ``make_call()``, retrying transient failures.

    Neither transcript writes anything until the call succeeds, so a retry
    never duplicates history.
    """
    attempt = 0
    while True:
        try:
            return await make_call()
        except Exception as e:
            if not _is_transient(e) or attempt >= len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            attempt += 1
            log.warning(
                "structured call attempt %d failed, retrying in %ss: %s",
                attempt,
                delay,
                e,
            )
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Transcripts: where a call's messages go
# ---------------------------------------------------------------------------


class _MemoryTranscript:
    """A standalone call's messages, kept in memory and saved on the row."""

    def __init__(self, engine: Engine, call: StructuredCall, history: list[dict]):
        self.engine = engine
        self.call = call
        self.messages = list(history)

    async def append_user(self, text: str, attachments=None, *, system=False) -> None:
        self.messages.append(self.engine.user_message(text, attachments))

    async def append_tool_exchange(self, calls: list[SeededToolCall]) -> None:
        self.messages.extend(self.engine.tool_exchange_messages(calls))

    async def append_tool_results(self, results) -> None:
        self.messages.extend(self.engine.tool_result_messages(results))

    async def append_response_text(self, text: str) -> None:
        self.messages.append(self.engine.assistant_text_message(text))

    async def respond(self, tool_schemas, note: str = "") -> list[EngineResponse]:
        system = "\n\n".join(p for p in (self.call.system_prompt, note) if p)
        # Older large tool results become stubs and image references become
        # image parts (only the latest few of each); self.messages keeps all.
        messages = trim_tool_results(
            self.messages,
            keep=getattr(self.engine, "tool_results_in_context", None),
            max_chars=budget_chars(self.engine),
        )
        messages = await sync_to_async(prepare_messages, thread_sensitive=True)(
            messages, getattr(self.engine, "engine_type", "")
        )
        completion = await self.engine.complete(
            messages, system=system, tools=tool_schemas
        )
        self.messages.append(completion.message)
        call = self.call
        call.input_tokens += completion.input_tokens
        call.output_tokens += completion.output_tokens
        call.cache_creation_input_tokens += completion.cache_creation_input_tokens
        call.cache_read_input_tokens += completion.cache_read_input_tokens
        call.reasoning_tokens += completion.reasoning_tokens
        call.model_name = completion.model or call.model_name
        add_request_cost(call, call.model_name, completion)
        await _save_usage(call)
        return completion.events

    async def finish(self) -> None:
        self.call.transcript = _storable(self.messages)


class _SessionTranscript:
    """An in-session call: messages go into the session's tables."""

    def __init__(
        self, engine: Engine, call: StructuredCall, extra_system: tuple[str, list[dict]]
    ):
        self.engine = engine
        self.call = call
        self.session = call.session
        self.extra_system, self.sections = extra_system

    def _rows(self):
        return self.session.messages

    async def append_user(self, text: str, attachments=None, *, system=False) -> None:
        # Ergo's own nudges are stored as authored by Ergo, not the chat's user.
        identity = {"author": system_identity()} if system else {}
        await self.engine.append_user_message(
            self.session, text, attachments, **identity
        )

    async def append_initial_user(self, text: str, attachments=None) -> None:
        metadata = self.call.metadata or {}
        identity = {
            key: metadata[f"message_{key}"]
            for key in ("author", "provenance")
            if f"message_{key}" in metadata
        }
        await self.engine.append_user_message(
            self.session, text, attachments, **identity
        )

    async def append_tool_exchange(self, calls: list[SeededToolCall]) -> None:
        await self.engine.append_tool_exchange(self.session, calls)

    async def append_tool_results(self, results) -> None:
        await self.engine.append_tool_results(self.session, results)

    async def append_response_text(self, text: str) -> None:
        await self.engine.append_assistant_text(self.session, text)

    async def respond(self, tool_schemas, note: str = "") -> list[EngineResponse]:
        # The spec's instructions (and any note) apply to this turn only.
        self.engine.ephemeral_context = "\n\n".join(
            p for p in (self.extra_system, note) if p
        )
        self.engine.context_sections = self.sections
        self.engine.last_request_info = None
        before = await anext_sequence(self.session)
        try:
            return [
                event async for event in self.engine.respond(self.session, tool_schemas)
            ]
        finally:
            info = self.engine.last_request_info
            if info is not None and "context" not in (self.call.metadata or {}):
                self.call.metadata = {**(self.call.metadata or {}), "context": info}
            self.engine.ephemeral_context = ""
            self.engine.context_sections = []
            # Count this request now, so a long turn's usage shows as it goes and a
            # turn that crashes or pauses for approval keeps what it used.
            await self._add_usage(since=before)

    async def finish(self) -> None:
        call = self.call
        following = await anext_sequence(self.session)
        call.last_sequence = following - 1 if following else None

    async def _add_usage(self, since: int) -> None:
        call = self.call
        async for row in self._rows().filter(sequence__gte=since, role="assistant"):
            call.input_tokens += row.input_tokens or 0
            call.output_tokens += row.output_tokens or 0
            call.cache_creation_input_tokens += (
                getattr(row, "cache_creation_input_tokens", None) or 0
            )
            call.cache_read_input_tokens += (
                getattr(row, "cache_read_input_tokens", None) or 0
            )
            call.reasoning_tokens += getattr(row, "reasoning_tokens", None) or 0
            if row.model_name:
                call.model_name = row.model_name
            add_request_cost(call, row.model_name or call.model_name, row)
        await _save_usage(call)


USAGE_FIELDS = [
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_tokens",
    "model_name",
    "cost_usd",
    "metadata",
    "updated_at",
]


async def _save_usage(call: StructuredCall) -> None:
    """Save a call's running usage and cost (it's saved in full when it ends)."""
    if call.pk is not None:
        await call.asave(update_fields=USAGE_FIELDS)


def _storable(messages: list[dict]) -> list[dict]:
    """Messages for the transcript field, with attachment bytes left out."""
    stored = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            message = {**message, "content": [_strip_bytes(b) for b in content]}  # noqa: PLW2901
        stored.append(message)
    return stored


def _strip_bytes(block: dict) -> dict:
    kind = block.get("type")
    if is_ref(block):
        return storable_ref(block)
    if isinstance(block.get("content"), list):  # a tool_result's text and images
        return {**block, "content": [_strip_bytes(b) for b in block["content"]]}
    if kind in {"image", "document"} and block.get("source", {}).get("type") == (
        "base64"
    ):
        return {"type": "text", "text": f"[{kind} attachment, not stored]"}
    if kind == "image_url" and block["image_url"]["url"].startswith("data:"):
        return {"type": "text", "text": "[image attachment, not stored]"}
    if kind in {"file", "input_audio"}:
        return {"type": "text", "text": f"[{kind} attachment, not stored]"}
    return block


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _fail(call: StructuredCall, error: str, category: str = "other") -> None:
    call.status = StructuredCallStatus.FAILED
    call.error = error
    call.error_category = category


def _stop(call: StructuredCall) -> None:
    call.status = StructuredCallStatus.STOPPED
    call.error = STOPPED_NOTICE


def _wrap_up_note(spec: StructuredCallSpec) -> str:
    return (
        f"This is your last step for this turn ({spec.max_turns} steps used). "
        f"No other tools are available now. Call {spec.output_tool_name} with what "
        'you finished, what is left, and that the user can say "continue" to '
        "carry on."
    )


def _response_text(parsed: Any, response: Any) -> str:
    """How a response reads as the turn's final assistant message."""
    as_message = getattr(parsed, "as_message", None)
    if callable(as_message):
        return as_message()
    return json.dumps(response, indent=2)


@dataclass
class _Run:
    """Everything one call's loop needs, so it can stop and resume."""

    engine: Engine
    transcript: Any
    call: StructuredCall
    spec: StructuredCallSpec
    user: Any
    workflow: Any
    allow_approvals: bool
    control: TurnControl | None = None

    def __post_init__(self):
        spec = self.spec
        self.submit = (
            StructuredOutputToolkit(spec.response_model, spec.output_tool_name)
            if spec.response_model is not None
            else None
        )
        self.toolkits = [*spec.toolkits, *([self.submit] if self.submit else [])]
        self.adapter = self.engine.get_tool_adapter()

    @property
    def tool_schemas(self) -> list[dict] | None:
        # Rebuilt for every model call: a toolkit's tools can change mid-turn
        # (loading a skill adds its tools to the next call).
        return _collect_toolkit_schemas(self.toolkits, self.adapter) or None

    def needs_approval(self, name: str) -> bool:
        return _tool_requires_approval(name, self.workflow, self.toolkits)

    async def run_tool(self, name: str, args: dict) -> tuple[Any, bool]:
        return await sync_to_async(_dispatch_tool, thread_sensitive=True)(
            name, args, self.toolkits, self.user, self.workflow
        )

    async def approval_preview(self, name: str, args: dict) -> ApprovalPreview | None:
        toolkit = _find_toolkit_for_tool(self.toolkits, name)
        preview = getattr(toolkit, "approval_preview", None)
        if not callable(preview):
            return None
        try:
            return await sync_to_async(preview, thread_sensitive=True)(name, args)
        except Exception:  # noqa: BLE001 -- preview errors must never permit execution
            return ApprovalPreview(
                "Preview failed before approval; this tool call will not run.",
                is_error=True,
            )

    async def steer(self) -> bool:
        """Add any steering messages to the call; True when the control says stop."""
        if self.control is None:
            return False
        signal = await self.control.check()
        for message in signal.messages:
            await self.transcript.append_user(message.text, message.attachments)
        return signal.stop


async def _loop(run: _Run) -> StructuredCallResult:
    """Run the call to its end; a crash marks it FAILED instead of leaving it in progress."""
    try:
        return await _run_loop(run)
    except BaseException as e:
        call = run.call
        if call.status == StructuredCallStatus.IN_PROGRESS:
            _fail(call, f"Crashed: {e!r}"[:MAX_ERROR_CHARS], "crash")
            try:
                await call.asave()
            except Exception:
                log.exception("Could not record the crash of call %s", call.pk)
        raise


async def _run_loop(run: _Run) -> StructuredCallResult:  # noqa: C901, PLR0912, PLR0915
    call, spec, transcript = run.call, run.spec, run.transcript
    parsed = None
    approvals: list[PendingApproval] = []
    finished = False
    while call.turns_used < spec.max_turns:
        if await run.steer():
            _stop(call)
            finished = True
            break
        call.turns_used += 1
        schemas, note = run.tool_schemas, ""
        if (
            spec.wrap_up
            and run.submit is not None
            and call.turns_used == spec.max_turns
        ):
            schemas = _collect_toolkit_schemas([run.submit], run.adapter) or None
            note = _wrap_up_note(spec)
        try:
            events = await _with_retry(
                lambda schemas=schemas, note=note: transcript.respond(schemas, note)
            )
        except Exception as e:  # noqa: BLE001 — recorded on the row
            _fail(call, f"API call failed: {e}", _error_category(e))
            finished = True
            break

        stop = next(
            (
                e.raw.get("stop_reason") or e.raw.get("finish_reason")
                for e in events
                if e.event_type == "done"
            ),
            None,
        )
        tool_events = [e for e in events if e.event_type == "tool_use"]
        if tool_events:
            results = []
            for event in tool_events:
                name, args = run.adapter.parse_tool_call(event.tool_use)
                tool_id = event.tool_use["id"]
                if stop in _MAX_TOKENS_STOPS:
                    # The reply was cut off, so this call's arguments may be
                    # incomplete (often {}): never run or ask approval for it.
                    results.append((tool_id, _TRUNCATED_TOOL_CALL, True))
                    continue
                if run.needs_approval(name):
                    if run.allow_approvals:
                        preview = await run.approval_preview(name, args)
                        approvals.append(
                            PendingApproval(
                                tool_use_id=tool_id,
                                tool_name=name,
                                arguments=args,
                                preview=preview.text if preview else "",
                                preview_error=preview.is_error if preview else False,
                            )
                        )
                        continue
                    result, is_error = (
                        f"{name} requires approval, which this call cannot request",
                        True,
                    )
                else:
                    result, is_error = await run.run_tool(name, args)
                results.append((tool_id, result, is_error))
            if results:
                await transcript.append_tool_results(results)
            if approvals:
                call.status = StructuredCallStatus.AWAITING_APPROVAL
                call.metadata = {
                    **call.metadata,
                    "pending_approvals": [
                        {
                            "id": a.tool_use_id,
                            "name": a.tool_name,
                            "input": a.arguments,
                            **({"preview": a.preview} if a.preview else {}),
                            **({"preview_error": True} if a.preview_error else {}),
                        }
                        for a in approvals
                    ],
                }
                finished = True
                break
            if run.submit is not None and run.submit.accepted is not None:
                parsed = run.submit.accepted
                call.response = parsed.model_dump(mode="json")
                call.status = StructuredCallStatus.COMPLETED
                # Close the turn with the response as plain assistant text,
                # so chat turns and history readers see the answer.
                await transcript.append_response_text(
                    _response_text(parsed, call.response)
                )
                finished = True
                break
            continue

        if stop in _MAX_TOKENS_STOPS:
            _fail(
                call,
                "Model hit its max_tokens limit before finishing; "
                "raise StructuredCallSpec.max_tokens",
                "model",
            )
            finished = True
            break

        if run.submit is not None:
            await transcript.append_user(
                _submit_nudge(spec.output_tool_name, events), system=True
            )
            continue

        text = "".join(e.text for e in events if e.event_type == "text" and e.text)
        try:
            parsed = spec.parse_text(text)
        except Exception as e:  # noqa: BLE001 — fed back to the model
            await transcript.append_user(
                f"Your response failed validation: {e}. "
                "Please correct your output and try again.",
                system=True,
            )
            continue
        call.response = parsed
        call.status = StructuredCallStatus.COMPLETED
        finished = True
        break

    if not finished:
        call.status = StructuredCallStatus.TURN_LIMITED
        call.error = f"No valid output after {spec.max_turns} turns"

    await transcript.finish()
    call.error = call.error[:MAX_ERROR_CHARS]
    await call.asave()
    return StructuredCallResult(call=call, parsed=parsed, approvals=approvals)


def _engine(spec: StructuredCallSpec, engine_spec: EngineSpec | None) -> Engine:
    base = engine_spec or get_default_engine_spec()
    config = dict(base.config)
    if spec.max_tokens is not None:
        config["max_tokens"] = spec.max_tokens
    return build_engine(EngineSpec(base.engine_type, base.transport_type, config))


async def _session_engine(
    spec: StructuredCallSpec, session: ConversationSession, engine: Engine | None
) -> Engine:
    if engine is None:
        from django_ergo.conversation.manager import SessionManager

        engine = await SessionManager().get_engine(session)
        if spec.max_tokens is not None:
            engine.max_tokens = spec.max_tokens
    # Any engine can take a session's next turn (messages are engine-neutral);
    # note the one this turn runs on.
    engine_type = getattr(engine, "engine_type", "") or session.engine_type
    transport = getattr(engine, "transport_type", "") or session.transport_type
    if (engine_type, transport) != (session.engine_type, session.transport_type):
        session.engine_type, session.transport_type = engine_type, transport
        await session.asave(update_fields=["engine_type", "transport_type"])
    return engine


async def _extra_system(
    spec: StructuredCallSpec, context_builder
) -> tuple[str, list[dict]]:
    parts = []
    sections = []
    if context_builder is not None:
        built = await sync_to_async(context_builder.build, thread_sensitive=True)()
        parts.append(built.text)
        sections = [
            {
                "title": section.title,
                "tokens": section.tokens,
                "complete": section.complete,
                "text": section.body[:20_000],
            }
            for section in built.sections
        ]
    parts.append(spec.system_prompt)
    return "\n\n".join(p for p in parts if p), sections


async def _session_people(session: ConversationSession):
    def load():
        return session.user, session.workflow

    return await sync_to_async(load, thread_sensitive=True)()


async def run_structured_call(  # noqa: PLR0913
    spec: StructuredCallSpec,
    message: str,
    *,
    user=None,
    session: ConversationSession | None = None,
    engine: Engine | None = None,
    engine_spec: EngineSpec | None = None,
    workflow=None,
    metadata: dict | None = None,
    attachments: list[Attachment] | None = None,
    parent: StructuredCall | None = None,
    context_builder: ContextBuilder | None = None,
    allow_approvals: bool = False,
    control: TurnControl | None = None,
) -> StructuredCallResult:
    """Make one structured call, standalone or as a turn of ``session``.

    Failures (API errors, turn limit, unparseable output) are recorded on the
    returned call rather than raised. With ``allow_approvals``, a tool that
    needs approval pauses the call (status ``awaiting_approval``) and
    ``resume_structured_call`` continues it; otherwise such tools are
    refused. ``context_builder`` (sessions only) adds its context to every
    model call of this turn. ``control`` can steer or stop the call between
    steps (see the module docstring).
    """
    pre_seeds = await sync_to_async(spec.all_pre_seeds, thread_sensitive=True)()
    if session is not None:
        active = await _session_engine(spec, session, engine)
        await maybe_compact(session, active)
        from django_ergo.conversation.compaction import latest_compaction

        current = await sync_to_async(latest_compaction)(session)
        seeded = session.structured_calls.filter(kind=spec.kind, metadata__seeded=True)
        if current:
            seeded = seeded.filter(first_sequence__gt=current.upto_sequence)
        seed = bool(pre_seeds) and (
            spec.pre_seed_each_turn or not await seeded.aexists()
        )
        metadata = {**(metadata or {}), **({"seeded": True} if seed else {})}
        call = await StructuredCall.objects.acreate(
            kind=spec.kind,
            user_id=session.user_id,
            session=session,
            parent=parent,
            request=message,
            engine_type=session.engine_type,
            first_sequence=await anext_sequence(session),
            metadata=metadata or {},
        )
        if spec.toolkits:
            await _record_kb_usage(session, spec.toolkits)
        transcript = _SessionTranscript(
            active, call, await _extra_system(spec, context_builder)
        )
        session_user, workflow = await _session_people(session)
        user = session_user
    else:
        active = engine or _engine(spec, engine_spec)
        history = list(parent.transcript) if parent is not None else []
        seed = not history
        call = await StructuredCall.objects.acreate(
            kind=spec.kind,
            user=user,
            parent=parent,
            request=message,
            engine_type=getattr(active, "engine_type", ""),
            system_prompt=spec.system_prompt,
            metadata=metadata or {},
        )
        transcript = _MemoryTranscript(active, call, history)

    run = _Run(active, transcript, call, spec, user, workflow, allow_approvals, control)
    await _record_tools(call, spec)
    if session is not None:
        await transcript.append_initial_user(message, attachments)
    else:
        await transcript.append_user(message, attachments)
    if seed and pre_seeds:
        await transcript.append_tool_exchange(await _run_pre_seeds(pre_seeds))
    return await _loop(run)


async def resume_structured_call(  # noqa: PLR0913
    spec: StructuredCallSpec,
    call: StructuredCall,
    decisions: dict[str, bool],
    *,
    engine: Engine | None = None,
    engine_spec: EngineSpec | None = None,
    workflow=None,
    context_builder: ContextBuilder | None = None,
    control: TurnControl | None = None,
) -> StructuredCallResult:
    """Continue a call that stopped for approval.

    ``decisions`` maps tool_use_id to True (approved) or False (denied).
    Approved tools run; the rest go back to the model as declined.
    ``control`` works as in ``run_structured_call``.
    """
    if call.status != StructuredCallStatus.AWAITING_APPROVAL:
        msg = f"Call {call.pk} is not waiting for approval"
        raise StructuredCallError(msg)
    # Claim it, so a second answer (a double tap, two devices) can't run the tools again.
    claimed = await StructuredCall.objects.filter(
        pk=call.pk, status=StructuredCallStatus.AWAITING_APPROVAL
    ).aupdate(status=StructuredCallStatus.IN_PROGRESS)
    if not claimed:
        msg = f"Call {call.pk} was already answered"
        raise StructuredCallError(msg)
    call.status = StructuredCallStatus.IN_PROGRESS
    pending = (call.metadata or {}).get("pending_approvals", [])

    if call.session_id is not None:
        session = await ConversationSession.objects.aget(pk=call.session_id)
        call.session = session
        active = await _session_engine(spec, session, engine)
        transcript = _SessionTranscript(
            active, call, await _extra_system(spec, context_builder)
        )
        user, workflow = await _session_people(session)
    else:
        active = engine or _engine(spec, engine_spec)
        transcript = _MemoryTranscript(active, call, call.transcript)
        user = await sync_to_async(lambda: call.user, thread_sensitive=True)()

    run = _Run(
        active,
        transcript,
        call,
        spec,
        user,
        workflow,
        allow_approvals=True,
        control=control,
    )
    results = []
    for item in pending:
        if decisions.get(item["id"]) and not item.get("preview_error"):
            result, is_error = await run.run_tool(item["name"], item["input"])
        elif item.get("preview_error"):
            result, is_error = "The preview failed; the tool was not executed.", True
        else:
            result, is_error = "The user declined this tool call.", True
        results.append((item["id"], result, is_error))
    await transcript.append_tool_results(results)
    call.status = StructuredCallStatus.IN_PROGRESS
    call.metadata = {k: v for k, v in call.metadata.items() if k != "pending_approvals"}
    return await _loop(run)


async def revise_structured_call(  # noqa: PLR0913
    spec: StructuredCallSpec,
    call: StructuredCall,
    message: str,
    *,
    engine: Engine | None = None,
    engine_spec: EngineSpec | None = None,
    workflow=None,
    attachments: list[Attachment] | None = None,
) -> StructuredCallResult:
    """Correct a call with a follow-up message ("make the title shorter").

    The result is a new call whose ``parent`` is ``call``. A standalone call
    replays its transcript; a call inside a session simply adds another
    structured turn to that session.
    """
    if call.session_id is not None:
        session = await ConversationSession.objects.aget(pk=call.session_id)
        return await run_structured_call(
            spec,
            message,
            session=session,
            engine=engine,
            parent=call,
            attachments=attachments,
        )
    active = engine or _engine(spec, engine_spec)
    if call.engine_type and call.engine_type != getattr(active, "engine_type", ""):
        msg = (
            f"Call {call.pk} ran on {call.engine_type}; its transcript can't be "
            f"replayed on {active.engine_type}"
        )
        raise StructuredCallError(msg)
    user = await sync_to_async(lambda: call.user, thread_sensitive=True)()
    return await run_structured_call(
        spec,
        message,
        user=user,
        engine=active,
        workflow=workflow,
        attachments=attachments,
        parent=call,
    )


def render_call(call: StructuredCall) -> str:
    """Short text form of a call, for logs and history readers."""
    body = json.dumps(call.response) if call.response is not None else call.error
    return f"[{call.kind} {call.status}] {call.request[:80]!r} -> {body}"
