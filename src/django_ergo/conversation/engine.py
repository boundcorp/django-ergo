"""Engine protocol ABC and shared types."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class EngineResponse:
    """Yielded from Engine.send() — wraps engine-native events."""

    event_type: str  # "text", "tool_use", "thinking", "done", "error"
    raw: dict = field(default_factory=dict)
    text: str | None = None
    tool_use: dict | None = None  # {"id": ..., "name": ..., "input": ...}
    thinking: str | None = None


@dataclass
class SeededToolCall:
    """A tool call executed outside the model and written into session history.

    Used for pre-seeding: the model sees the call and its result as if it had
    made the call itself, saving a round trip for data the caller already has.
    """

    tool_use_id: str
    name: str
    input: dict
    result: Any
    is_error: bool = False


@dataclass
class Completion:
    """One model call made by Engine.complete(): nothing is stored.

    ``message`` is the assistant reply as an engine-native message dict,
    ready to append to the messages list for the next call.
    """

    message: dict
    events: list[EngineResponse]
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_tokens: int = 0  # included in output_tokens


def session_system_prompt(session) -> str:
    """Return the effective system prompt: the session's own, else the workflow's."""
    own = getattr(session, "system_prompt", "") or ""
    if own:
        return own
    workflow = getattr(session, "workflow", None)
    return (workflow.instructions if workflow else "") or ""


class Engine(ABC):
    """Abstract engine protocol. All engines implement this interface."""

    engine_type: str
    # Extra system text for the current turn (e.g. a ContextBuilder's output).
    # Sent with every model call while set, never stored.
    ephemeral_context: str = ""
    # Large tool results each model call carries in full; None uses
    # DJANGO_ERGO["TOOL_RESULTS_IN_CONTEXT"] (see conversation.tool_results).
    tool_results_in_context: int | None = None

    @abstractmethod
    async def start_session(self, session) -> str:
        """Start a new session. Returns engine-native session ID."""

    @abstractmethod
    async def resume_session(self, session) -> None:
        """Resume an existing session from DB state."""

    @abstractmethod
    async def send(
        self, session, message: str, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Send a message, yield streaming responses."""

    @abstractmethod
    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        """Submit a tool result, yield assistant continuation."""

    async def submit_tool_results_batch(
        self,
        session,
        results: list[tuple[str, Any, bool]],
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        """Submit multiple tool results and yield assistant continuation.

        Args:
            results: list of (tool_use_id, result, is_error) tuples.

        Default implementation submits one at a time (last one triggers API call).
        Engines that require batched submission (e.g. OpenAI) should override.
        """
        for i, (tool_use_id, result, is_error) in enumerate(results):
            if i < len(results) - 1:
                await self._persist_tool_result(session, tool_use_id, result, is_error)
            else:
                async for event in self.submit_tool_result(
                    session, tool_use_id, result, is_error, additional_tools
                ):
                    yield event

    async def append_tool_results(
        self, session, results: list[tuple[str, Any, bool]]
    ) -> None:
        """Persist tool results without calling the model.

        Args:
            results: list of (tool_use_id, result, is_error) tuples.
        """
        for tool_use_id, result, is_error in results:
            await self._persist_tool_result(session, tool_use_id, result, is_error)

    async def _persist_tool_result(  # noqa: B027
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> None:
        """Persist a tool result without triggering a new API call.

        Engines should override this if they have different persistence logic.
        """

    async def append_user_message(
        self, session, message: str, attachments: list | None = None
    ) -> None:
        """Persist a user message (and any attachments) without calling the model."""
        msg = f"{type(self).__name__} does not support append_user_message"
        raise NotImplementedError(msg)

    async def append_tool_exchange(self, session, calls: list[SeededToolCall]) -> None:
        """Persist an assistant tool-call turn and its results without calling the model."""
        msg = f"{type(self).__name__} does not support append_tool_exchange"
        raise NotImplementedError(msg)

    async def respond(
        self, session, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Call the model on the current history and yield its response.

        Unlike send(), this persists no new user input, so a failed call can
        be retried without duplicating history.
        """
        msg = f"{type(self).__name__} does not support respond"
        raise NotImplementedError(msg)
        yield  # pragma: no cover

    async def append_assistant_text(self, session, text: str) -> None:
        """Persist an assistant text message without calling the model."""
        msg = f"{type(self).__name__} does not support append_assistant_text"
        raise NotImplementedError(msg)

    # -- Sessionless calls ------------------------------------------------
    # These build and send engine-native message lists without touching the
    # database, for work that doesn't belong to a session (standalone
    # structured calls).

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str = "",
        tools: list[dict] | None = None,
    ) -> Completion:
        """Call the model on ``messages`` and return its reply. Stores nothing."""
        msg = f"{type(self).__name__} does not support complete"
        raise NotImplementedError(msg)

    def user_message(self, text: str, attachments: list | None = None) -> dict:
        """An engine-native user message, with any in-memory attachments."""
        msg = f"{type(self).__name__} does not support user_message"
        raise NotImplementedError(msg)

    def assistant_text_message(self, text: str) -> dict:
        return {"role": "assistant", "content": text}

    def tool_exchange_messages(self, calls: list[SeededToolCall]) -> list[dict]:
        """Engine-native messages for tool calls the model didn't make itself."""
        msg = f"{type(self).__name__} does not support tool_exchange_messages"
        raise NotImplementedError(msg)

    def tool_result_messages(self, results: list[tuple[str, Any, bool]]) -> list[dict]:
        """Engine-native messages carrying (tool_use_id, result, is_error) results."""
        msg = f"{type(self).__name__} does not support tool_result_messages"
        raise NotImplementedError(msg)

    @abstractmethod
    def get_tools_schema(self, workflow) -> list[dict]:
        """Convert ergo tools to engine-native tool format."""

    def history_rows(self, session, after_sequence: int | None = None) -> list:
        """Return [(message row, engine-native message dict), ...] in order.

        Unlike reconstruct_messages(), this ignores compaction. Compaction
        uses it to read the messages it folds.
        """
        msg = f"{type(self).__name__} does not support history_rows"
        raise NotImplementedError(msg)

    @abstractmethod
    def reconstruct_messages(self, session) -> list[dict]:
        """Build engine-native message history from DB."""

    @abstractmethod
    async def close_session(self, session) -> None:
        """Clean up resources."""

    @abstractmethod
    def get_tool_adapter(self):
        """Return the ToolAdapter for this engine."""

    async def generate(
        self,
        prompt: str,
        workflow=None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        """One-shot generation. Override in subclasses that support it."""
        msg = "This engine does not support one-shot generation"
        raise NotImplementedError(msg)
