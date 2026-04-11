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


class Engine(ABC):
    """Abstract engine protocol. All engines implement this interface."""

    engine_type: str

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
    async def submit_tool_result(  # noqa: PLR0913
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

    @abstractmethod
    def get_tools_schema(self, workflow) -> list[dict]:
        """Convert ergo tools to engine-native tool format."""

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
