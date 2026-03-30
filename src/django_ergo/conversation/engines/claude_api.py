"""Claude API engine implementation using the Anthropic SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class ClaudeAPIEngine(Engine):
    """Engine implementation that uses the Anthropic Claude API directly."""

    engine_type = "claude"

    def __init__(self, config: dict):
        self.model = config.get("model", "claude-3-5-sonnet-20241022")
        self.api_key = config.get("api_key")
        self.max_tokens = config.get("max_tokens", 8192)
        self._client = None
        self._adapter = ClaudeToolAdapter()

    def _get_client(self):
        """Lazily initialize the Anthropic client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def get_tool_adapter(self) -> ClaudeToolAdapter:
        return self._adapter

    def reconstruct_messages(self, session) -> list[dict]:
        """Build Claude API message history from DB state."""

        messages = []
        for msg in session.claude_messages.prefetch_related("content_blocks").all():
            content = []
            for block in msg.content_blocks.all():
                if block.block_type == "text":
                    content.append({"type": "text", "text": block.text})
                elif block.block_type == "thinking":
                    content.append({"type": "thinking", "thinking": block.thinking})
                elif block.block_type == "tool_use":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.tool_use_id,
                            "name": block.tool_name,
                            "input": block.tool_input or {},
                        }
                    )
                elif block.block_type == "tool_result":
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_result_for,
                            "content": block.tool_result_content or "",
                            "is_error": block.is_error,
                        }
                    )
            messages.append({"role": msg.role, "content": content})
        return messages

    def get_tools_schema(self, workflow) -> list[dict]:
        """Convert workflow tools to Claude API tool format."""
        tools_config = workflow.get_tools_config() if workflow else {}
        enabled_tools = tools_config.get("enabled_tools", [])

        schemas = []
        for tool_name in enabled_tools:
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config is not None:
                schemas.append(self._adapter.to_engine_schema(tool_config))
        return schemas

    async def start_session(self, session) -> str:
        """No-op: the API is stateless, no server-side session to start."""
        return ""

    async def resume_session(self, session) -> None:
        """No-op: the API is stateless, reconstruct from DB on each call."""
        return

    async def close_session(self, session) -> None:
        """No-op: the API is stateless, nothing to clean up."""
        return

    async def send(self, session, message: str) -> AsyncIterator[EngineResponse]:
        # TODO: implement streaming send using self._get_client()
        msg = (
            "ClaudeAPIEngine.send() is not yet implemented. "
            "Use integration tests once the streaming implementation is ready."
        )
        raise NotImplementedError(msg)

    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        # TODO: implement tool result submission and continuation streaming
        msg = (
            "ClaudeAPIEngine.submit_tool_result() is not yet implemented. "
            "Use integration tests once the streaming implementation is ready."
        )
        raise NotImplementedError(msg)

    async def generate(
        self,
        prompt: str,
        workflow=None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        """One-shot generation without a session — useful for typed/structured outputs."""
        client = self._get_client()
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        sys_prompt = system or (workflow.instructions if workflow else None)
        if sys_prompt:
            kwargs["system"] = sys_prompt

        if response_model is not None:
            schema = response_model.model_json_schema()
            kwargs["tools"] = [
                {
                    "name": "structured_output",
                    "description": f"Return a {response_model.__name__} object",
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "structured_output"}
        elif workflow:
            tools = self.get_tools_schema(workflow)
            if tools:
                kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        if response_model is not None:
            for block in response.content:
                if block.type == "tool_use" and block.name == "structured_output":
                    parsed = response_model.model_validate(block.input)
                    return EngineResponse(
                        event_type="done",
                        raw={
                            "parsed": parsed,
                            "usage": {
                                "input_tokens": response.usage.input_tokens,
                                "output_tokens": response.usage.output_tokens,
                            },
                        },
                    )

        text = "".join(block.text for block in response.content if block.type == "text")
        return EngineResponse(
            event_type="done",
            raw={
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }
            },
            text=text,
        )
