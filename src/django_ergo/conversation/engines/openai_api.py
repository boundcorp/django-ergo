"""OpenAI API Engine implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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
        return self._get_client()

    def _get_client(self):
        """Return the OpenAI client, initialising it lazily."""
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

    async def _call_and_persist(
        self, session, seq: int
    ) -> AsyncIterator[EngineResponse]:
        """Rebuild history, call the API, persist the assistant reply, and yield responses."""
        import json

        from django_ergo.conversation.models import OpenAIMessage

        messages = self.reconstruct_messages(session)
        tools = self.get_tools_schema(session.workflow) if session.workflow else None

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

        await OpenAIMessage.objects.acreate(
            session=session,
            role="assistant",
            content=msg.content,
            tool_calls=[tc.model_dump() for tc in msg.tool_calls]
            if msg.tool_calls
            else None,
            sequence=seq,
            input_tokens=response.usage.prompt_tokens if response.usage else None,
            output_tokens=response.usage.completion_tokens if response.usage else None,
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

    async def send(self, session, message: str) -> AsyncIterator[EngineResponse]:
        """Persist the user message, call the API, and yield response events."""
        from django_ergo.conversation.models import OpenAIMessage

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session, role="user", content=message, sequence=seq
        )

        async for event in self._call_and_persist(session, seq + 1):
            yield event

    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
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

        async for event in self._call_and_persist(session, seq + 1):
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
