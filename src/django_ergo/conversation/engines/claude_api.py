"""Claude API engine implementation using the Anthropic SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.attachments import Attachment
from django_ergo.conversation.attachments import attachments_by_sequence
from django_ergo.conversation.attachments import claude_block
from django_ergo.conversation.attachments import save_attachments
from django_ergo.conversation.compaction import apply_native_window
from django_ergo.conversation.compaction import latest_compaction
from django_ergo.conversation.compaction import render_summary_message
from django_ergo.conversation.engine import Completion
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engine import SeededToolCall
from django_ergo.conversation.engine import session_system_prompt
from django_ergo.conversation.telemetry import record_usage
from django_ergo.conversation.telemetry import trace_engine_call
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def claude_message_dict(msg) -> dict:
    """Convert a ClaudeMessage row (with content_blocks) to an API message dict."""
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
    return {"role": msg.role, "content": content}


class ClaudeAPIEngine(Engine):
    """Engine implementation that uses the Anthropic Claude API directly."""

    engine_type = "claude"

    def __init__(self, config: dict):
        self.model = config.get("model", "claude-3-5-sonnet-20241022")
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url")
        self.max_tokens = config.get("max_tokens", 8192)
        self._client = None
        self._adapter = ClaudeToolAdapter()

    def _get_client(self):
        """Lazily initialize the Anthropic async client."""
        if self._client is None:
            import anthropic

            kwargs = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    def get_tool_adapter(self) -> ClaudeToolAdapter:
        return self._adapter

    def history_rows(self, session, after_sequence: int | None = None) -> list:
        """Return [(ClaudeMessage, message dict), ...] in sequence order."""
        rows = session.claude_messages.prefetch_related("content_blocks")
        if after_sequence is not None:
            rows = rows.filter(sequence__gt=after_sequence)
        attachments = attachments_by_sequence(session)
        result = []
        for msg in rows:
            message = claude_message_dict(msg)
            if msg.sequence in attachments:
                # Attachments go before the text, as Anthropic recommends.
                message["content"] = [
                    *(claude_block(row) for row in attachments[msg.sequence]),
                    *message["content"],
                ]
            result.append((msg, message))
        return result

    def reconstruct_messages(self, session) -> list[dict]:
        """Build Claude API message history from DB state.

        When the session has been compacted, the latest summary replaces the
        messages it covers.
        """
        compaction = latest_compaction(session)
        after = compaction.upto_sequence if compaction else None
        messages = [message for _, message in self.history_rows(session, after)]
        if compaction:
            messages.insert(
                0,
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": render_summary_message(compaction)}
                    ],
                },
            )
        return apply_native_window(session, messages)

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
        self, session, seq: int, additional_tools: list[dict] | None = None
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
            from asgiref.sync import sync_to_async

            messages = await sync_to_async(
                self.reconstruct_messages, thread_sensitive=True
            )(session)
            tools = (
                self.get_tools_schema(session.workflow) if session.workflow else None
            )
            if additional_tools:
                tools = (tools or []) + additional_tools

            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
            }
            system = "\n\n".join(
                part
                for part in (
                    session_system_prompt(session),
                    getattr(self, "ephemeral_context", ""),
                )
                if part
            )
            if system:
                kwargs["system"] = system
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

    async def append_user_message(
        self,
        session,
        message: str,
        attachments: list[Attachment] | None = None,
    ) -> None:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        seq = await session.claude_messages.acount()
        user_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=user_msg, block_type="text", sequence=0, text=message
        )
        if attachments:
            await save_attachments(session, seq, attachments)

    async def append_tool_exchange(self, session, calls: list[SeededToolCall]) -> None:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        if not calls:
            return
        seq = await session.claude_messages.acount()
        call_msg = await ClaudeMessage.objects.acreate(
            session=session, role="assistant", sequence=seq, stop_reason="tool_use"
        )
        result_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq + 1
        )
        for block_seq, call in enumerate(calls):
            await ClaudeContentBlock.objects.acreate(
                message=call_msg,
                block_type="tool_use",
                sequence=block_seq,
                tool_use_id=call.tool_use_id,
                tool_name=call.name,
                tool_input=call.input,
            )
            await ClaudeContentBlock.objects.acreate(
                message=result_msg,
                block_type="tool_result",
                sequence=block_seq,
                tool_result_for=call.tool_use_id,
                tool_result_content=str(call.result),
                is_error=call.is_error,
            )

    async def respond(
        self, session, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        seq = await session.claude_messages.acount()
        async for event in self._process_response(session, seq, additional_tools):
            yield event

    async def send(
        self,
        session,
        message: str,
        additional_tools: list[dict] | None = None,
        attachments: list[Attachment] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        await self.append_user_message(session, message, attachments)
        async for event in self.respond(session, additional_tools):
            yield event

    async def submit_tool_result(  # noqa: PLR0913
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
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

        async for event in self._process_response(session, seq + 1, additional_tools):
            yield event

    async def _persist_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> None:
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

    async def append_assistant_text(self, session, text: str) -> None:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        seq = await session.claude_messages.acount()
        msg = await ClaudeMessage.objects.acreate(
            session=session,
            role="assistant",
            sequence=seq,
            stop_reason="end_turn",
            model_name=self.model,
        )
        await ClaudeContentBlock.objects.acreate(
            message=msg, block_type="text", sequence=0, text=text
        )

    # -- Sessionless calls ------------------------------------------------

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str = "",
        tools: list[dict] | None = None,
    ) -> Completion:
        with trace_engine_call(
            operation="complete",
            engine_type=self.engine_type,
            model=self.model,
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = tools
            response = await self._get_client().messages.create(**kwargs)
            usage = response.usage
            record_usage(
                span,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation=getattr(usage, "cache_creation_input_tokens", None),
                cache_read=getattr(usage, "cache_read_input_tokens", None),
            )

        content: list[dict] = []
        events: list[EngineResponse] = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
                events.append(
                    EngineResponse(
                        event_type="text", raw={"type": "text"}, text=block.text
                    )
                )
            elif block.type == "tool_use":
                call = {"id": block.id, "name": block.name, "input": block.input}
                content.append({"type": "tool_use", **call})
                events.append(
                    EngineResponse(
                        event_type="tool_use", raw={"type": "tool_use"}, tool_use=call
                    )
                )
            elif block.type == "thinking":
                content.append({"type": "thinking", "thinking": block.thinking})
                events.append(
                    EngineResponse(
                        event_type="thinking",
                        raw={"type": "thinking"},
                        thinking=block.thinking,
                    )
                )
        events.append(
            EngineResponse(event_type="done", raw={"stop_reason": response.stop_reason})
        )
        return Completion(
            message={"role": "assistant", "content": content},
            events=events,
            model=self.model,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_creation_input_tokens=(
                getattr(usage, "cache_creation_input_tokens", None) or 0
            ),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None)
            or 0,
        )

    def user_message(self, text: str, attachments: list | None = None) -> dict:
        blocks = [claude_block(a) for a in attachments or []]
        return {"role": "user", "content": [*blocks, {"type": "text", "text": text}]}

    def assistant_text_message(self, text: str) -> dict:
        return {"role": "assistant", "content": [{"type": "text", "text": text}]}

    def tool_exchange_messages(self, calls: list[SeededToolCall]) -> list[dict]:
        if not calls:
            return []
        return [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": c.tool_use_id,
                        "name": c.name,
                        "input": c.input,
                    }
                    for c in calls
                ],
            },
            *self.tool_result_messages(
                [(c.tool_use_id, c.result, c.is_error) for c in calls]
            ),
        ]

    def tool_result_messages(self, results: list[tuple[str, Any, bool]]) -> list[dict]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": str(result),
                        "is_error": is_error,
                    }
                    for tool_use_id, result, is_error in results
                ],
            }
        ]

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
