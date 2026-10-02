"""OpenAI API Engine implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.db import models

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.attachments import Attachment
from django_ergo.conversation.attachments import attachments_by_sequence
from django_ergo.conversation.attachments import openai_part
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
from django_ergo.openai_options import DEFAULT_OPENAI_MODEL
from django_ergo.openai_options import chat_options
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def usage_parts(usage) -> dict:
    """OpenAI usage in Ergo's terms (Claude's): ``input_tokens`` is the uncached part of
    the prompt, with cache reads and writes counted apart; ``reasoning_tokens`` is the
    part of ``output_tokens`` spent reasoning. OpenAI reports cached tokens inside
    ``prompt_tokens``, so they're taken out of it here."""
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "reasoning_tokens": None,
        }
    prompt = _count(getattr(usage, "prompt_tokens", None))
    if prompt is None:
        prompt = _count(getattr(usage, "input_tokens", None))  # Responses API naming
    output = _count(getattr(usage, "completion_tokens", None))
    if output is None:
        output = _count(getattr(usage, "output_tokens", None))
    details = getattr(usage, "prompt_tokens_details", None) or getattr(
        usage, "input_tokens_details", None
    )
    cached = (_count(getattr(details, "cached_tokens", None)) or 0) if details else 0
    written = (
        (_count(getattr(details, "cache_write_tokens", None)) or 0) if details else 0
    )
    out_details = getattr(usage, "completion_tokens_details", None) or getattr(
        usage, "output_tokens_details", None
    )
    reasoning = (
        (_count(getattr(out_details, "reasoning_tokens", None)) or 0)
        if out_details
        else 0
    )
    return {
        "input_tokens": max(0, prompt - cached - written)
        if prompt is not None
        else None,
        "output_tokens": output,
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def telemetry_usage(usage) -> dict:
    parts = usage_parts(usage)
    return {
        "input_tokens": parts["input_tokens"],
        "output_tokens": parts["output_tokens"],
        "cache_creation": parts["cache_creation_input_tokens"],
        "cache_read": parts["cache_read_input_tokens"],
    }


def openai_message_dict(msg, attachments=(), *, audio_input: bool = False) -> dict:
    """Convert an OpenAIMessage row (plus its attachments) to an API message dict."""
    content = msg.content
    if attachments:
        content = [
            *([{"type": "text", "text": msg.content}] if msg.content else []),
            *(openai_part(row, audio_input=audio_input) for row in attachments),
        ]
    entry = {"role": msg.role, "content": content}
    if msg.tool_calls:
        entry["tool_calls"] = msg.tool_calls
    if msg.tool_call_id:
        entry["tool_call_id"] = msg.tool_call_id
    return entry


class OpenAIAPIEngine(Engine):
    """Engine implementation using the OpenAI API SDK."""

    engine_type = "openai"

    def __init__(self, config: dict):
        self.config = config
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
        """Return [(OpenAIMessage, message dict), ...] in sequence order.

        System messages are always included, whatever after_sequence says.
        """
        rows = session.openai_messages.all()
        if after_sequence is not None:
            rows = rows.filter(
                models.Q(sequence__gt=after_sequence) | models.Q(role="system")
            )
        attachments = attachments_by_sequence(session)
        return [
            (
                msg,
                openai_message_dict(
                    msg,
                    attachments.get(msg.sequence, ()),
                    audio_input=self.audio_input,
                ),
            )
            for msg in rows
        ]

    def reconstruct_messages(self, session) -> list[dict]:
        """Build OpenAI message list from DB-stored OpenAIMessage rows.

        When the session has been compacted, the latest summary replaces the
        messages it covers, after any system message.
        """
        compaction = latest_compaction(session)
        after = compaction.upto_sequence if compaction else None
        messages = [message for _, message in self.history_rows(session, after)]
        if compaction:
            position = sum(1 for m in messages if m["role"] == "system")
            messages.insert(
                position,
                {"role": "user", "content": render_summary_message(compaction)},
            )
        return apply_native_window(session, messages)

    def get_tools_schema(self, workflow) -> list[dict]:
        """Return all registered tools converted to OpenAI function-calling format."""
        tools = tool_registry.list_tools()
        return [self._adapter.to_engine_schema(tool) for tool in tools]

    async def start_session(self, session) -> str:
        """Optionally inject a system message from workflow instructions."""
        from asgiref.sync import sync_to_async

        from django_ergo.conversation.models import OpenAIMessage
        from django_ergo.conversation.models import OpenAIMessageRole

        system = session_system_prompt(session)
        if system:
            await sync_to_async(OpenAIMessage.objects.create)(
                session=session,
                role=OpenAIMessageRole.SYSTEM,
                content=system,
                sequence=0,
            )
        return str(session.id)

    async def resume_session(self, session) -> None:
        """No-op — OpenAI API is stateless; history is reconstructed from DB."""

    async def close_session(self, session) -> None:
        """No-op — OpenAI API is stateless."""

    async def _call_and_persist(
        self, session, seq: int, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Rebuild history, call the API, persist the assistant reply, and yield responses."""
        import json

        from django_ergo.conversation.models import OpenAIMessage

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
            if extra := getattr(self, "ephemeral_context", ""):
                position = sum(1 for m in messages if m["role"] == "system")
                messages.insert(position, {"role": "system", "content": extra})
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

            record_usage(span, **telemetry_usage(response.usage))

            await OpenAIMessage.objects.acreate(
                session=session,
                role="assistant",
                content=msg.content,
                tool_calls=[tc.model_dump() for tc in msg.tool_calls]
                if msg.tool_calls
                else None,
                sequence=seq,
                **usage_parts(response.usage),
                model_name=self.model,
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

    async def append_user_message(
        self,
        session,
        message: str,
        attachments: list[Attachment] | None = None,
    ) -> None:
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session, role="user", content=message, sequence=seq
        )
        if attachments:
            await save_attachments(session, seq, attachments)

    async def append_tool_exchange(self, session, calls: list[SeededToolCall]) -> None:
        import json

        from django_ergo.conversation.models import OpenAIMessage

        if not calls:
            return
        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session,
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": call.tool_use_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.input),
                    },
                }
                for call in calls
            ],
            sequence=seq,
        )
        for offset, call in enumerate(calls, start=1):
            await OpenAIMessage.objects.acreate(
                session=session,
                role="tool",
                content=str(call.result),
                tool_call_id=call.tool_use_id,
                sequence=seq + offset,
            )

    async def respond(
        self, session, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        seq = await session.openai_messages.acount()
        async for event in self._call_and_persist(session, seq, additional_tools):
            yield event

    async def send(
        self,
        session,
        message: str,
        additional_tools: list[dict] | None = None,
        attachments: list[Attachment] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        """Persist the user message, call the API, and yield response events."""
        await self.append_user_message(session, message, attachments)
        async for event in self.respond(session, additional_tools):
            yield event

    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        """Persist the tool result, call the API again, and yield response events."""
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session,
            role="tool",
            content=str(result),
            tool_call_id=tool_use_id,
            sequence=seq,
        )

        async for event in self._call_and_persist(session, seq + 1, additional_tools):
            yield event

    async def _persist_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> None:
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session,
            role="tool",
            content=str(result),
            tool_call_id=tool_use_id,
            sequence=seq,
        )

    async def submit_tool_results_batch(
        self,
        session,
        results: list[tuple[str, Any, bool]],
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        for tool_use_id, result, _is_error in results:
            await OpenAIMessage.objects.acreate(
                session=session,
                role="tool",
                content=str(result),
                tool_call_id=tool_use_id,
                sequence=seq,
            )
            seq += 1

        async for event in self._call_and_persist(session, seq, additional_tools):
            yield event

    async def append_assistant_text(self, session, text: str) -> None:
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session,
            role="assistant",
            content=text,
            sequence=seq,
            model_name=self.model,
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
            record_usage(span, **telemetry_usage(usage))

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
                *(openai_part(a, audio_input=self.audio_input) for a in attachments),
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
            {"role": "tool", "tool_call_id": tool_use_id, "content": str(result)}
            for tool_use_id, result, _is_error in results
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

            record_usage(span, **telemetry_usage(response.usage))

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
