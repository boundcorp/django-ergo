"""OpenAI API Engine implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.engine import Engine
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.engine import EngineResponse


class OpenAIAPIEngine(Engine):
    """Engine implementation using the OpenAI API SDK."""

    engine_type = "openai"

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get("model", "gpt-4o")
        self.api_key = config.get("api_key")
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 4096)
        self._client = None
        self._adapter = OpenAIToolAdapter()

    @property
    def client(self):
        """Lazy-initialise the OpenAI client."""
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(api_key=self.api_key)
        return self._client

    def get_tool_adapter(self) -> OpenAIToolAdapter:
        return self._adapter

    def reconstruct_messages(self, session) -> list[dict]:
        """Build OpenAI message list from DB-stored OpenAIMessage rows."""
        messages = []
        for msg in session.openai_messages.all():
            entry = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            messages.append(entry)
        return messages

    def get_tools_schema(self, workflow) -> list[dict]:
        """Return all registered tools converted to OpenAI function-calling format."""
        tools = tool_registry.list_tools()
        return [self._adapter.to_engine_schema(tool) for tool in tools]

    async def start_session(self, session) -> str:
        """Optionally inject a system message from workflow instructions."""
        from asgiref.sync import sync_to_async

        from django_ergo.conversation.models import OpenAIMessage
        from django_ergo.conversation.models import OpenAIMessageRole

        if session.workflow and session.workflow.instructions:
            await sync_to_async(OpenAIMessage.objects.create)(
                session=session,
                role=OpenAIMessageRole.SYSTEM,
                content=session.workflow.instructions,
                sequence=0,
            )
        return str(session.id)

    async def resume_session(self, session) -> None:
        """No-op — OpenAI API is stateless; history is reconstructed from DB."""

    async def close_session(self, session) -> None:
        """No-op — OpenAI API is stateless."""

    async def send(self, session, message: str) -> AsyncIterator[EngineResponse]:
        """Not yet implemented — integration tests cover this separately."""
        msg = "OpenAIAPIEngine.send() is not yet implemented"
        raise NotImplementedError(msg)

    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        """Not yet implemented — integration tests cover this separately."""
        msg = "OpenAIAPIEngine.submit_tool_result() is not yet implemented"
        raise NotImplementedError(msg)
