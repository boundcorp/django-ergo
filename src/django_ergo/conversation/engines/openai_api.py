"""OpenAI API Engine implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.db import models

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.compaction import latest_compaction
from django_ergo.conversation.compaction import render_summary_message
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engine import SeededToolCall
from django_ergo.conversation.engine import session_system_prompt
from django_ergo.conversation.telemetry import record_usage
from django_ergo.conversation.telemetry import trace_engine_call
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def openai_message_dict(msg) -> dict:
    """Convert an OpenAIMessage row to an API message dict."""
    entry = {"role": msg.role, "content": msg.content}
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
        self.model = config.get("model", "gpt-4o")
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url")
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 4096)
        self._client = None
        self._adapter = OpenAIToolAdapter()

    @property
    def client(self):
        """Lazy-initialise the OpenAI client."""
        return self._get_client()

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
        return [(msg, openai_message_dict(msg)) for msg in rows]

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
            tools = (
                self.get_tools_schema(session.workflow) if session.workflow else None
            )
            if additional_tools:
                tools = (tools or []) + additional_tools

            kwargs: dict = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
            }
            if self.max_tokens:
                kwargs["max_tokens"] = self.max_tokens
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

            await OpenAIMessage.objects.acreate(
                session=session,
                role="assistant",
                content=msg.content,
                tool_calls=[tc.model_dump() for tc in msg.tool_calls]
                if msg.tool_calls
                else None,
                sequence=seq,
                input_tokens=response.usage.prompt_tokens if response.usage else None,
                output_tokens=response.usage.completion_tokens
                if response.usage
                else None,
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

    async def append_user_message(self, session, message: str) -> None:
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session, role="user", content=message, sequence=seq
        )

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
        self, session, message: str, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Persist the user message, call the API, and yield response events."""
        await self.append_user_message(session, message)
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

            kwargs = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
            }
            if self.max_tokens:
                kwargs["max_tokens"] = self.max_tokens

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
