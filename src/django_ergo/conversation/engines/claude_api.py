"""Claude API engine implementation using the Anthropic SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.telemetry import record_usage
from django_ergo.conversation.telemetry import trace_engine_call
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
        """Lazily initialize the Anthropic async client."""
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
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

    async def _process_response(
        self, session, seq: int
    ) -> AsyncIterator[EngineResponse]:
        """Call the API with current session history and persist + yield response blocks."""
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        with trace_engine_call(
            operation="send",
            engine_type=self.engine_type,
            model=self.model,
            session_id=str(session.id) if session else "",
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            messages = self.reconstruct_messages(session)
            tools = (
                self.get_tools_schema(session.workflow) if session.workflow else None
            )

            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
            }
            if session.workflow and session.workflow.instructions:
                kwargs["system"] = session.workflow.instructions
            if tools:
                kwargs["tools"] = tools

            client = self._get_client()
            response = await client.messages.create(**kwargs)

            record_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_creation=getattr(
                    response.usage, "cache_creation_input_tokens", None
                ),
                cache_read=getattr(response.usage, "cache_read_input_tokens", None),
            )

            assistant_msg = await ClaudeMessage.objects.acreate(
                session=session,
                role="assistant",
                sequence=seq,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model_name=self.model,
                cache_creation_input_tokens=getattr(
                    response.usage, "cache_creation_input_tokens", None
                ),
                cache_read_input_tokens=getattr(
                    response.usage, "cache_read_input_tokens", None
                ),
            )

            for block_seq, block in enumerate(response.content):
                if block.type == "text":
                    await ClaudeContentBlock.objects.acreate(
                        message=assistant_msg,
                        block_type="text",
                        sequence=block_seq,
                        text=block.text,
                    )
                    yield EngineResponse(
                        event_type="text", raw={"type": "text"}, text=block.text
                    )
                elif block.type == "tool_use":
                    await ClaudeContentBlock.objects.acreate(
                        message=assistant_msg,
                        block_type="tool_use",
                        sequence=block_seq,
                        tool_use_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input,
                    )
                    yield EngineResponse(
                        event_type="tool_use",
                        raw={"type": "tool_use"},
                        tool_use={
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        },
                    )
                elif block.type == "thinking":
                    await ClaudeContentBlock.objects.acreate(
                        message=assistant_msg,
                        block_type="thinking",
                        sequence=block_seq,
                        thinking=block.thinking,
                    )
                    yield EngineResponse(
                        event_type="thinking",
                        raw={"type": "thinking"},
                        thinking=block.thinking,
                    )

            yield EngineResponse(
                event_type="done", raw={"stop_reason": response.stop_reason}
            )

    async def send(self, session, message: str) -> AsyncIterator[EngineResponse]:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        seq = await session.claude_messages.acount()
        user_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=user_msg, block_type="text", sequence=0, text=message
        )

        async for event in self._process_response(session, seq + 1):
            yield event

    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        seq = await session.claude_messages.acount()
        result_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=result_msg,
            block_type="tool_result",
            sequence=0,
            tool_result_for=tool_use_id,
            tool_result_content=str(result),
            is_error=is_error,
        )

        async for event in self._process_response(session, seq + 1):
            yield event

    async def generate(
        self,
        prompt: str,
        workflow=None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        """One-shot generation without a session — useful for typed/structured outputs."""
        with trace_engine_call(
            operation="generate",
            engine_type=self.engine_type,
            model=self.model,
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            client = self._get_client()
            kwargs: dict[str, Any] = {
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

            record_usage(
                span,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_creation=getattr(
                    response.usage, "cache_creation_input_tokens", None
                ),
                cache_read=getattr(response.usage, "cache_read_input_tokens", None),
            )

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

            text = "".join(
                block.text for block in response.content if block.type == "text"
            )
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
