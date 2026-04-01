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


class TransportFailover(Exception):  # noqa: N818
    """Raised when a transport fails and should be swapped."""

    def __init__(self, original: str, fallback: str, reason: str):
        self.original = original
        self.fallback = fallback
        msg = f"Transport failover {original} -> {fallback}: {reason}"
        super().__init__(msg)


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
