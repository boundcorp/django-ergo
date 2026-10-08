"""OpenAI API Engine implementation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.attachments import attachments_by_sequence
from django_ergo.conversation.attachments import openai_part
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
from django_ergo.conversation.messages import tool_use_block
from django_ergo.conversation.request_context import prepare_turn_context
from django_ergo.conversation.telemetry import record_usage
from django_ergo.conversation.telemetry import trace_engine_call
from django_ergo.openai_options import DEFAULT_OPENAI_MODEL
from django_ergo.openai_options import chat_options
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _detail(details, name: str) -> int:
    if isinstance(details, dict):
        return _count(details.get(name))
    return _count(getattr(details, name, None))


def usage_parts(usage) -> dict:
    """OpenAI usage split the way it's billed (and the way Claude reports it):
    ``input_tokens`` is the uncached rest of the prompt, ``cache_read_input_tokens``
    and ``cache_creation_input_tokens`` are cached input and cache writes (both inside
    OpenAI's ``prompt_tokens``), and ``reasoning_tokens`` is the part of
    ``output_tokens`` spent reasoning. All None without usage."""
    if usage is None:
        return dict.fromkeys(
            (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
                "reasoning_tokens",
            )
        )
    prompt = _count(getattr(usage, "prompt_tokens", None)) or _count(
        getattr(usage, "input_tokens", None)
    )
    output = _count(getattr(usage, "completion_tokens", None)) or _count(
        getattr(usage, "output_tokens", None)
    )
    details = getattr(usage, "prompt_tokens_details", None) or getattr(
        usage, "input_tokens_details", None
    )
    out_details = getattr(usage, "completion_tokens_details", None) or getattr(
        usage, "output_tokens_details", None
    )
    cached = _detail(details, "cached_tokens") if details else 0
    written = _detail(details, "cache_write_tokens") if details else 0
    return {
        "input_tokens": max(0, prompt - cached - written),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": written,
        "output_tokens": output,
        "reasoning_tokens": _detail(out_details, "reasoning_tokens")
        if out_details
        else 0,
    }


def usage_tokens(usage) -> tuple[int | None, int | None, int | None]:
    """(uncached input, cached input, output) tokens from an OpenAI usage block (see usage_parts)."""
    parts = usage_parts(usage)
    return (
        parts["input_tokens"],
        parts["cache_read_input_tokens"],
        parts["output_tokens"],
    )


def attachment_part(row, *, audio_input: bool = False) -> dict:
    """A user-message attachment: images as references (sent by prepare_messages)."""
    if row.kind == "image":
        return attachment_ref(row)
    return openai_part(row, audio_input=audio_input)


def openai_message_dicts(msg, attachments=(), *, audio_input: bool = False) -> list:
    """Render a SessionMessage row (plus its attachments) as OpenAI message dicts.

    Tool results become ``tool`` messages (images as list content, which
    prepare_messages moves into a user message), ahead of any user text. An
    assistant's tool calls become ``tool_calls``; thinking is left out, as
    OpenAI can't take another model's reasoning back.
    """
    blocks = list(msg.content_blocks.all())
    texts = [b.text or "" for b in blocks if b.block_type == "text"]
    if msg.role == "assistant":
        calls = [
            {
                "id": b.tool_use_id,
                "type": "function",
                "function": {
                    "name": b.tool_name,
                    "arguments": json.dumps(b.tool_input or {}),
                },
            }
            for b in blocks
            if b.block_type == "tool_use"
        ]
        text = "\n\n".join(t for t in texts if t)
        if not (text or calls):
            return []
        entry: dict = {"role": "assistant", "content": text or None}
        if calls:
            entry["tool_calls"] = calls
        return [entry]
    out = [
        {
            "role": "tool",
            "tool_call_id": b.tool_result_for,
            "content": b.tool_result_content or "",
            **({"is_error": True} if b.is_error else {}),
        }
        for b in blocks
        if b.block_type == "tool_result"
    ]
    if texts or attachments:
        text = "\n\n".join(texts)
        text = attributed_text(
            text, getattr(msg, "author", {}), getattr(msg, "provenance", {})
        )
        content = text
        if attachments:
            content = [
                *([{"type": "text", "text": text}] if text else []),
                *(attachment_part(row, audio_input=audio_input) for row in attachments),
            ]
        out.append({"role": "user", "content": content})
    return out


class OpenAIAPIEngine(StoredMessagesMixin, Engine):
    """Engine implementation using the OpenAI API SDK."""

    engine_type = "openai"

    def __init__(self, config: dict):
        self.config = config
        self.context_window = int(
            config.get("context_window")
            or (1_000_000 if str(config.get("model", "")).endswith("[1m]") else 200_000)
        )
        self.model = config.get("model", DEFAULT_OPENAI_MODEL)
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url")
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 4096)
        # Reasoning models only; GPT-6 tool calls always use "none".
        self.reasoning_effort = config.get("reasoning_effort")
        # Send audio attachments natively (needs an audio-capable model);
        # otherwise their transcript is sent as text.
        self.audio_input = config.get("audio_input", False)
        self._client = None
        self._adapter = OpenAIToolAdapter()

    @property
    def client(self):
        """Lazy-initialise the OpenAI client."""
        return self._get_client()

    def _options(self, *, tools: bool) -> dict:
        return chat_options(
            self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            tools=tools,
        )

    def _get_client(self):
        """Return the OpenAI client, initialising it lazily."""
        if self._client is None:
            import openai

            kwargs = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    def get_tool_adapter(self) -> OpenAIToolAdapter:
        return self._adapter

    def history_rows(self, session, after_sequence: int | None = None) -> list:
        """Return [(SessionMessage, message dict), ...] in sequence order.

        A row of several tool results gives one entry per result.
        """
        rows = session.messages.prefetch_related("content_blocks")
        if after_sequence is not None:
            rows = rows.filter(sequence__gt=after_sequence)
        attachments = attachments_by_sequence(session)
        return [
            (msg, message)
            for msg in rows
            for message in openai_message_dicts(
                msg, attachments.get(msg.sequence, ()), audio_input=self.audio_input
            )
        ]

    def reconstruct_messages(self, session) -> list[dict]:
        """Build the OpenAI message list: the system prompt, then the history.

        When the session has been compacted, the latest summary replaces the
        messages it covers.
        """
        compaction = latest_compaction(session)
        after = compaction.upto_sequence if compaction else None
        rows = self.history_rows(session, after)
        messages = [message for _, message in rows]
        if compaction:
            messages.insert(
                0, {"role": "user", "content": render_summary_message(compaction)}
            )
        if system := session_system_prompt(session):
            messages.insert(0, {"role": "system", "content": system})
        messages = prepare_turn_context(
            self, session, apply_native_window(session, messages), rows, compaction
        )
        return prepare_messages(messages, "openai")

    def get_tools_schema(self, workflow) -> list[dict]:
        """Return all registered tools converted to OpenAI function-calling format."""
        tools = tool_registry.list_tools()
        return [self._adapter.to_engine_schema(tool) for tool in tools]

    async def start_session(self, session) -> str:
        """No-op: the system prompt is sent from the session on every call."""
        return str(session.id)

    async def resume_session(self, session) -> None:
        """No-op — OpenAI API is stateless; history is reconstructed from DB."""

    async def close_session(self, session) -> None:
        """No-op — OpenAI API is stateless."""

    async def _call(
        self, session, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Rebuild history, call the API, persist the assistant reply, and yield responses."""
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

            kwargs: dict = {
                "model": self.model,
                "messages": messages,
                **self._options(tools=bool(tools)),
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            response = await self._get_client().chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message

            record_usage(
                span,
                input_tokens=response.usage.prompt_tokens if response.usage else None,
                output_tokens=response.usage.completion_tokens
                if response.usage
                else None,
            )

            blocks = (
                [{"block_type": "text", "text": msg.content}] if msg.content else []
            )
            blocks += [
                tool_use_block(
                    tc.id, tc.function.name, json.loads(tc.function.arguments or "{}")
                )
                for tc in msg.tool_calls or []
            ]
            await add_message(
                session,
                "assistant",
                blocks,
                stop_reason="tool_use" if msg.tool_calls else choice.finish_reason,
                model_name=self.model,
                **usage_parts(response.usage),
            )

            if msg.content:
                yield EngineResponse(event_type="text", raw={}, text=msg.content)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    yield EngineResponse(
                        event_type="tool_use",
                        raw=tc.model_dump(),
                        tool_use={
                            "id": tc.id,
                            "name": tc.function.name,
                            "input": json.loads(tc.function.arguments),
                        },
                    )

            yield EngineResponse(
                event_type="done", raw={"finish_reason": choice.finish_reason}
            )

    # -- Sessionless calls ------------------------------------------------

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str = "",
        tools: list[dict] | None = None,
    ) -> Completion:
        import json

        with trace_engine_call(
            operation="complete",
            engine_type=self.engine_type,
            model=self.model,
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            prefix = [{"role": "system", "content": system}] if system else []
            kwargs: dict = {
                "model": self.model,
                "messages": [*prefix, *messages],
                **self._options(tools=bool(tools)),
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            response = await self._get_client().chat.completions.create(**kwargs)
            usage = response.usage
            record_usage(
                span,
                input_tokens=usage.prompt_tokens if usage else None,
                output_tokens=usage.completion_tokens if usage else None,
            )

        choice = response.choices[0]
        msg = choice.message
        message: dict = {"role": "assistant", "content": msg.content}
        events: list[EngineResponse] = []
        if msg.content:
            events.append(EngineResponse(event_type="text", raw={}, text=msg.content))
        if msg.tool_calls:
            message["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]
            events.extend(
                EngineResponse(
                    event_type="tool_use",
                    raw=tc.model_dump(),
                    tool_use={
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments),
                    },
                )
                for tc in msg.tool_calls
            )
        events.append(
            EngineResponse(
                event_type="done", raw={"finish_reason": choice.finish_reason}
            )
        )
        return Completion(
            message=message,
            events=events,
            model=self.model,
            **{k: v or 0 for k, v in usage_parts(usage).items()},
        )

    def user_message(self, text: str, attachments: list | None = None) -> dict:
        if not attachments:
            return {"role": "user", "content": text}
        return {
            "role": "user",
            "content": [
                *([{"type": "text", "text": text}] if text else []),
                *(
                    attachment_part(a, audio_input=self.audio_input)
                    for a in attachments
                ),
            ],
        }

    def tool_exchange_messages(self, calls: list[SeededToolCall]) -> list[dict]:
        import json

        if not calls:
            return []
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": c.tool_use_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.input)},
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
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": memory_result(result),
                **({"is_error": True} if is_error else {}),
            }
            for tool_use_id, result, is_error in results
        ]

    async def generate(
        self,
        prompt: str,
        workflow=None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        """One-shot generation without a session — useful for typed/structured outputs."""
        import json

        with trace_engine_call(
            operation="generate",
            engine_type=self.engine_type,
            model=self.model,
            transport_type="api",
            max_tokens=self.max_tokens,
        ) as span:
            messages = []
            sys_prompt = system or (workflow.instructions if workflow else None)
            if sys_prompt:
                messages.append({"role": "system", "content": sys_prompt})
            messages.append({"role": "user", "content": prompt})

            kwargs = {"model": self.model, "messages": messages}

            if response_model is not None:
                schema = response_model.model_json_schema()
                kwargs["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": "structured_output",
                            "description": f"Return a {response_model.__name__} object",
                            "parameters": schema,
                        },
                    }
                ]
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": "structured_output"},
                }
            elif workflow:
                tools = self.get_tools_schema(workflow)
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
            kwargs.update(self._options(tools="tools" in kwargs))

            response = await self._get_client().chat.completions.create(**kwargs)
            choice = response.choices[0]
            msg = choice.message

            record_usage(
                span,
                input_tokens=response.usage.prompt_tokens if response.usage else None,
                output_tokens=response.usage.completion_tokens
                if response.usage
                else None,
            )

            if response_model is not None and msg.tool_calls:
                tc = msg.tool_calls[0]
                raw_args = json.loads(tc.function.arguments)
                parsed = response_model.model_validate(raw_args)
                return EngineResponse(
                    event_type="done",
                    raw={
                        "parsed": parsed,
                        "usage": {
                            "prompt_tokens": response.usage.prompt_tokens,
                            "completion_tokens": response.usage.completion_tokens,
                        },
                    },
                )

            return EngineResponse(
                event_type="done",
                raw={
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                    }
                },
                text=msg.content,
            )
