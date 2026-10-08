"""Claude API engine implementation using the Anthropic SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.attachments import attachments_by_sequence
from django_ergo.conversation.attachments import claude_block
from django_ergo.conversation.compaction import apply_native_window
from django_ergo.conversation.compaction import latest_compaction
from django_ergo.conversation.compaction import render_summary_message
from django_ergo.conversation.engine import Completion
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engine import SeededToolCall
from django_ergo.conversation.engine import session_system_prompt
from django_ergo.conversation.identity import attributed_text
from django_ergo.conversation.images import attachment_ref
from django_ergo.conversation.images import memory_result
from django_ergo.conversation.images import prepare_messages
from django_ergo.conversation.messages import StoredMessagesMixin
from django_ergo.conversation.messages import add_message
from django_ergo.conversation.messages import tool_result_content  # noqa: F401
from django_ergo.conversation.messages import tool_use_block
from django_ergo.conversation.request_context import prepare_turn_context
from django_ergo.conversation.telemetry import record_usage
from django_ergo.conversation.telemetry import trace_engine_call
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def attachment_block(row) -> dict:
    """A user-message attachment: images as references (sent by prepare_messages)."""
    return attachment_ref(row) if row.kind == "image" else claude_block(row)


def without_unsigned_thinking(messages: list[dict]) -> list[dict]:
    """Drop thinking the API wouldn't take back: stored thinking has no
    signature (e.g. thinking from the Claude Code engine)."""

    def keep(block: dict) -> bool:
        return block.get("type") != "thinking" or bool(block.get("signature"))

    return [
        {**m, "content": [b for b in m["content"] if keep(b)]}
        if isinstance(m.get("content"), list)
        else m
        for m in messages
    ]


def claude_message_dict(msg, *, include_attribution: bool = True) -> dict:
    """Convert a SessionMessage row (with content_blocks) to an API message dict."""
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
    if include_attribution and msg.role == "user":
        for block in content:
            if block["type"] == "text":
                block["text"] = attributed_text(
                    block["text"],
                    getattr(msg, "author", {}),
                    getattr(msg, "provenance", {}),
                )
                break
    return {"role": msg.role, "content": content}


class ClaudeAPIEngine(StoredMessagesMixin, Engine):
    """Engine implementation that uses the Anthropic Claude API directly."""

    engine_type = "claude"
    transport_type = "api"

    def __init__(self, config: dict):
        self.context_window = int(
            config.get("context_window")
            or (1_000_000 if str(config.get("model", "")).endswith("[1m]") else 200_000)
        )
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
        """Return [(SessionMessage, message dict), ...] in sequence order."""
        rows = session.messages.prefetch_related("content_blocks")
        if after_sequence is not None:
            rows = rows.filter(sequence__gt=after_sequence)
        attachments = attachments_by_sequence(session)
        result = []
        for msg in rows:
            message = claude_message_dict(msg)
            if msg.sequence in attachments:
                # Attachments go before the text, as Anthropic recommends.
                message["content"] = [
                    *(attachment_block(row) for row in attachments[msg.sequence]),
                    *message["content"],
                ]
            # The API refuses empty text blocks and empty messages (e.g. a
            # files-only message, or an empty reply stored by another engine).
            content = [
                b for b in message["content"] if b["type"] != "text" or b.get("text")
            ]
            if content:
                result.append((msg, {**message, "content": content}))
        return result

    def reconstruct_messages(self, session) -> list[dict]:
        """Build Claude API message history from DB state.

        When the session has been compacted, the latest summary replaces the
        messages it covers.
        """
        compaction = latest_compaction(session)
        after = compaction.upto_sequence if compaction else None
        rows = self.history_rows(session, after)
        messages = [message for _, message in rows]
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
        messages = prepare_turn_context(
            self, session, apply_native_window(session, messages), rows, compaction
        )
        return prepare_messages(messages, "claude")

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

    async def _call(
        self, session, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Call the API with current session history and persist + yield response blocks."""
        with trace_engine_call(
            operation="send",
            engine_type=self.engine_type,
            model=self.model,
            session_id=str(session.id) if session else "",
            transport_type=self.transport_type,
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
                "messages": without_unsigned_thinking(messages),
            }
            system = session_system_prompt(session)
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

            blocks: list[dict] = []
            events: list[EngineResponse] = []
            for block in response.content:
                if block.type == "text":
                    blocks.append({"block_type": "text", "text": block.text})
                    events.append(
                        EngineResponse(
                            event_type="text", raw={"type": "text"}, text=block.text
                        )
                    )
                elif block.type == "tool_use":
                    blocks.append(tool_use_block(block.id, block.name, block.input))
                    events.append(
                        EngineResponse(
                            event_type="tool_use",
                            raw={"type": "tool_use"},
                            tool_use={
                                "id": block.id,
                                "name": block.name,
                                "input": block.input,
                            },
                        )
                    )
                elif block.type == "thinking":
                    blocks.append(
                        {"block_type": "thinking", "thinking": block.thinking}
                    )
                    events.append(
                        EngineResponse(
                            event_type="thinking",
                            raw={"type": "thinking"},
                            thinking=block.thinking,
                        )
                    )
            await add_message(
                session,
                "assistant",
                blocks,
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
            for event in events:
                yield event

            yield EngineResponse(
                event_type="done", raw={"stop_reason": response.stop_reason}
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
            transport_type=self.transport_type,
            max_tokens=self.max_tokens,
        ) as span:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": without_unsigned_thinking(messages),
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
        blocks = [attachment_block(a) for a in attachments or []]
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
                        "content": memory_result(result),
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
            transport_type=self.transport_type,
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
