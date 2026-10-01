"""Structured calls as conversation sessions.

A structured call sends a prompt, lets the model use tools, and ends with a
validated output. Each call lives in a ``ConversationSession`` with
``mode="structured"``. Every user message to that session produces one
``StructuredOutput`` row, so a follow-up message ("make the title shorter")
gets a corrected output with the full history behind it.

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
    result = await run_structured_call(spec, user=user, message="Plan X")
    result.parsed  # Plan(...)

    handler = StructuredSession(spec)
    followup = await handler.send(result.session, "Drop step 3")

Output modes:

- ``response_model`` (a Pydantic model): the model gets a ``submit_output``
  tool whose input schema is the model's JSON schema. Validation errors go
  back to the model as tool errors so it can fix them.
- ``output_parser`` (a callable on the final text, ``json.loads`` by default):
  the final assistant text is parsed; a parse failure is sent back as a
  correction message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from asgiref.sync import sync_to_async
from pydantic import BaseModel
from pydantic import ValidationError

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.engine import SeededToolCall
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import SessionMode
from django_ergo.conversation.models import SessionStatus
from django_ergo.conversation.models import StructuredOutput
from django_ergo.conversation.models import StructuredOutputStatus
from django_ergo.conversation.runner import _collect_toolkit_schemas
from django_ergo.conversation.runner import _find_toolkit_for_tool
from django_ergo.conversation.runner import _record_kb_usage
from django_ergo.conversation.runner import _tool_requires_approval
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.toolkit import Toolkit
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_TOOL = "submit_output"
# Seconds to wait before each retry of a transient API failure.
RETRY_DELAYS = (1, 2, 4)
MAX_ERROR_CHARS = 4000

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
_SECRET_CONFIG_SUFFIXES = ("api_key", "_secret", "password")
_MAX_TOKENS_STOPS = {"max_tokens", "length"}


class StructuredCallError(RuntimeError):
    """Raised for misuse (wrong session mode, bad spec), not model failures.

    Model and tool failures are recorded on the StructuredOutput row.
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
    max_turns: int = 10
    max_tokens: int | None = None
    output_tool_name: str = DEFAULT_OUTPUT_TOOL

    def __post_init__(self):
        if self.response_model is not None and self.output_parser is not None:
            msg = "Pass response_model or output_parser, not both"
            raise StructuredCallError(msg)

    def parse_text(self, text: str) -> Any:
        parser = self.output_parser or json.loads
        return parser(text)


@dataclass
class StructuredCallResult:
    session: ConversationSession
    record: StructuredOutput
    parsed: Any = None

    @property
    def status(self) -> str:
        return self.record.status

    @property
    def ok(self) -> bool:
        return self.record.status == StructuredOutputStatus.COMPLETED

    @property
    def error(self) -> str:
        return self.record.error


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


def _redact_config(config: dict) -> dict:
    return {
        key: value
        for key, value in config.items()
        if not key.endswith(_SECRET_CONFIG_SUFFIXES)
    }


def _message_rows(session: ConversationSession):
    if session.engine_type == "openai":
        return session.openai_messages
    return session.claude_messages


def _dispatch_tool(
    name: str,
    args: dict,
    toolkits: list[Toolkit],
    session: ConversationSession,
) -> tuple[Any, bool]:
    """Run one tool call. Returns (result, is_error); never raises for tool failures."""
    toolkit = _find_toolkit_for_tool(toolkits, name)
    if toolkit is not None:
        try:
            return toolkit.execute_tool(name, args), False
        except Exception as e:  # noqa: BLE001 — any tool failure goes back to the model
            return str(e), True

    enabled = (
        session.workflow.get_tools_config().get("enabled_tools", [])
        if session.workflow
        else []
    )
    if name in enabled and tool_registry.get_tool(name) is not None:
        try:
            result = tool_registry.execute_tool(
                name=name, user=session.user, arguments=args, approved=True
            )
        except Exception as e:  # noqa: BLE001
            return str(e), True
        return result, False
    return f"Unknown tool: {name}", True


async def _run_pre_seeds(pre_seeds: list[PreSeedCall]) -> list[SeededToolCall]:
    calls = []
    for index, seed in enumerate(pre_seeds):
        try:
            result = await sync_to_async(seed.handler, thread_sensitive=True)(
                seed.tool_input
            )
        except Exception:
            log.warning("pre-seed %r raised; skipping", seed.tool_name, exc_info=True)
            continue
        calls.append(
            SeededToolCall(
                tool_use_id=f"preseed_{index}",
                name=seed.tool_name,
                input=seed.tool_input,
                result=result,
            )
        )
    return calls


async def _respond_with_retry(
    engine: Engine,
    session: ConversationSession,
    tool_schemas: list[dict] | None,
) -> list[EngineResponse]:
    """Call the model, retrying transient failures.

    respond() persists nothing until the API call succeeds, so a retry never
    duplicates history.
    """
    attempt = 0
    while True:
        try:
            return [event async for event in engine.respond(session, tool_schemas)]
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


async def _finalize(record: StructuredOutput, session: ConversationSession) -> None:
    rows = _message_rows(session)
    first = record.first_sequence or 0
    total = await rows.acount()
    record.last_sequence = total - 1 if total else None
    record.input_tokens = 0
    record.output_tokens = 0
    record.cache_creation_input_tokens = 0
    record.cache_read_input_tokens = 0
    async for row in rows.filter(sequence__gte=first, role="assistant"):
        record.input_tokens += row.input_tokens or 0
        record.output_tokens += row.output_tokens or 0
        record.cache_creation_input_tokens += (
            getattr(row, "cache_creation_input_tokens", None) or 0
        )
        record.cache_read_input_tokens += (
            getattr(row, "cache_read_input_tokens", None) or 0
        )
        if row.model_name:
            record.model_name = row.model_name
    record.error = record.error[:MAX_ERROR_CHARS]
    await record.asave()


def _fail(record: StructuredOutput, error: str, category: str = "other") -> None:
    record.status = StructuredOutputStatus.FAILED
    record.error = error
    record.error_category = category


async def run_structured_turn(  # noqa: C901, PLR0912, PLR0915
    engine: Engine,
    session: ConversationSession,
    message: str,
    spec: StructuredCallSpec,
) -> StructuredCallResult:
    """Send one message to a structured session and drive it to a validated output.

    Failures (API errors, turn limit, unparseable output) are recorded on the
    returned StructuredOutput rather than raised.
    """
    if session.mode != SessionMode.STRUCTURED:
        msg = f"Session {session.pk} is not a structured session"
        raise StructuredCallError(msg)

    rows = _message_rows(session)
    output_seq = await session.structured_outputs.acount()
    record = await StructuredOutput.objects.acreate(
        session=session,
        sequence=output_seq,
        request=message,
        first_sequence=await rows.acount(),
    )

    submit = (
        StructuredOutputToolkit(spec.response_model, spec.output_tool_name)
        if spec.response_model is not None
        else None
    )
    toolkits = [*spec.toolkits, *([submit] if submit else [])]
    adapter = engine.get_tool_adapter()
    tool_schemas = _collect_toolkit_schemas(toolkits, adapter) or None
    if spec.toolkits:
        await _record_kb_usage(session, spec.toolkits)

    await engine.append_user_message(session, message)
    if spec.pre_seeds and output_seq == 0:
        await engine.append_tool_exchange(session, await _run_pre_seeds(spec.pre_seeds))

    parsed = None
    finished = False
    for turn in range(spec.max_turns):
        record.turns_used = turn + 1
        try:
            events = await _respond_with_retry(engine, session, tool_schemas)
        except Exception as e:  # noqa: BLE001 — recorded on the row
            _fail(record, f"API call failed: {e}", _error_category(e))
            finished = True
            break

        tool_events = [e for e in events if e.event_type == "tool_use"]
        if tool_events:
            results = []
            for event in tool_events:
                name, args = adapter.parse_tool_call(event.tool_use)
                if _tool_requires_approval(name, session.workflow):
                    result = (
                        f"{name} requires approval, which structured calls "
                        "cannot request"
                    )
                    is_error = True
                else:
                    result, is_error = await sync_to_async(
                        _dispatch_tool, thread_sensitive=True
                    )(name, args, toolkits, session)
                results.append((event.tool_use["id"], result, is_error))
            await engine.append_tool_results(session, results)
            if submit is not None and submit.accepted is not None:
                parsed = submit.accepted
                record.output = parsed.model_dump(mode="json")
                record.status = StructuredOutputStatus.COMPLETED
                finished = True
                break
            continue

        stop = next(
            (
                e.raw.get("stop_reason") or e.raw.get("finish_reason")
                for e in events
                if e.event_type == "done"
            ),
            None,
        )
        if stop in _MAX_TOKENS_STOPS:
            _fail(
                record,
                "Model hit its max_tokens limit before finishing; "
                "raise StructuredCallSpec.max_tokens",
                "model",
            )
            finished = True
            break

        if submit is not None:
            await engine.append_user_message(
                session,
                f"You must call the {spec.output_tool_name} tool with your final "
                "answer. Do not answer in plain text.",
            )
            continue

        text = "".join(e.text for e in events if e.event_type == "text" and e.text)
        try:
            parsed = spec.parse_text(text)
        except Exception as e:  # noqa: BLE001 — fed back to the model
            await engine.append_user_message(
                session,
                f"Your response failed validation: {e}. "
                "Please correct your output and try again.",
            )
            continue
        record.output = parsed
        record.status = StructuredOutputStatus.COMPLETED
        finished = True
        break

    if not finished:
        record.status = StructuredOutputStatus.TURN_LIMITED
        record.error = f"No valid output after {spec.max_turns} turns"

    await _finalize(record, session)
    return StructuredCallResult(session=session, record=record, parsed=parsed)


class StructuredSession:
    """Handler for structured sessions built from one spec.

    ``start`` opens a new session and answers the first message; ``send``
    answers a follow-up in an existing session.
    """

    def __init__(
        self,
        spec: StructuredCallSpec,
        *,
        engine: Engine | None = None,
        engine_spec: EngineSpec | None = None,
        workflow=None,
    ):
        self.spec = spec
        self.engine = engine
        self.engine_spec = engine_spec
        self.workflow = workflow

    def _engine_spec(self) -> EngineSpec:
        base = self.engine_spec or get_default_engine_spec()
        config = dict(base.config)
        if self.spec.max_tokens is not None:
            config["max_tokens"] = self.spec.max_tokens
        return EngineSpec(base.engine_type, base.transport_type, config)

    def _engine_for(self, session: ConversationSession) -> Engine:
        if self.engine is not None:
            return self.engine
        spec = self._engine_spec()
        if (spec.engine_type, spec.transport_type) != (
            session.engine_type,
            session.transport_type,
        ):
            # Session predates a config change: rebuild from what it recorded.
            stored = session.metadata.get("structured", {}).get("engine_config", {})
            spec = EngineSpec(session.engine_type, session.transport_type, stored)
        return build_engine(spec)

    async def start(
        self,
        *,
        user,
        message: str,
        metadata: dict | None = None,
    ) -> StructuredCallResult:
        spec = self._engine_spec()
        engine_type = getattr(self.engine, "engine_type", None) or spec.engine_type
        response_model = self.spec.response_model
        session_metadata = dict(metadata or {})
        session_metadata["structured"] = {
            "kind": self.spec.kind,
            "response_model": (
                f"{response_model.__module__}.{response_model.__qualname__}"
                if response_model
                else None
            ),
            # Never persist credentials; they come from settings or the caller.
            "engine_config": _redact_config(spec.config),
        }
        session = await ConversationSession.objects.acreate(
            user=user,
            workflow=self.workflow,
            engine_type=engine_type,
            transport_type=spec.transport_type,
            status=SessionStatus.ACTIVE,
            mode=SessionMode.STRUCTURED,
            kind=self.spec.kind,
            system_prompt=self.spec.system_prompt,
            metadata=session_metadata,
        )
        engine = self._engine_for(session)
        session.session_id = await engine.start_session(session)
        await session.asave(update_fields=["session_id", "updated_at"])
        return await run_structured_turn(engine, session, message, self.spec)

    async def send(
        self, session: ConversationSession, message: str
    ) -> StructuredCallResult:
        engine = self._engine_for(session)
        await engine.resume_session(session)
        return await run_structured_turn(engine, session, message, self.spec)


async def run_structured_call(  # noqa: PLR0913
    spec: StructuredCallSpec,
    *,
    user,
    message: str,
    engine: Engine | None = None,
    engine_spec: EngineSpec | None = None,
    workflow=None,
    metadata: dict | None = None,
) -> StructuredCallResult:
    """Open a structured session and return the output for its first message."""
    handler = StructuredSession(
        spec, engine=engine, engine_spec=engine_spec, workflow=workflow
    )
    return await handler.start(user=user, message=message, metadata=metadata)
