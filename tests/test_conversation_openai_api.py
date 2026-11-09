"""Tests for OpenAIAPIEngine."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import EngineType
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.models import SessionStatus
from django_ergo.conversation.models import TransportType
from django_ergo.models import Workflow

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    return OpenAIAPIEngine(
        config={
            "model": "gpt-4o",
            "api_key": "test-key",
            "temperature": 0.5,
            "max_tokens": 1024,
        }
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="testuser_openai",
        email="openai@example.com",
        password="testpass123",
    )


@pytest.fixture
def workflow(db):
    return Workflow.objects.create(
        name="OpenAI Test Workflow",
        description="A workflow for testing OpenAI engine",
        instructions="You are a helpful assistant.",
        tools_config={},
    )


@pytest.fixture
def session(db, user):
    return ConversationSession.objects.create(
        user=user,
        engine_type=EngineType.OPENAI,
        transport_type=TransportType.API,
        status=SessionStatus.ACTIVE,
    )


@pytest.fixture
def session_with_workflow(db, user, workflow):
    return ConversationSession.objects.create(
        user=user,
        workflow=workflow,
        engine_type=EngineType.OPENAI,
        transport_type=TransportType.API,
        status=SessionStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# Basic engine attribute tests (no DB needed)
# ---------------------------------------------------------------------------


class TestOpenAIAPIEngineAttributes:
    def test_engine_type(self, engine):
        assert engine.engine_type == "openai"

    def test_config_stored(self, engine):
        expected_temperature = 0.5
        expected_max_tokens = 1024
        assert engine.model == "gpt-4o"
        assert engine.api_key == "test-key"
        assert engine.temperature == expected_temperature
        assert engine.max_tokens == expected_max_tokens

    def test_default_max_tokens(self):
        assert OpenAIAPIEngine(config={}).max_tokens == 32000

    def test_get_tool_adapter_returns_openai_adapter(self, engine):
        from django_ergo.conversation.adapters import OpenAIToolAdapter

        assert isinstance(engine.get_tool_adapter(), OpenAIToolAdapter)

    def test_client_is_lazy(self, engine):
        """Client must not be initialised at construction time."""
        assert engine.get_tool_adapter() is not None  # engine is usable
        assert vars(engine).get("_client") is None

    def test_send_is_async_generator(self, engine):
        """send() must be an async generator function (real implementation)."""
        import inspect

        assert inspect.isasyncgenfunction(engine.send)

    def test_submit_tool_result_is_async_generator(self, engine):
        """submit_tool_result() must be an async generator function (real implementation)."""
        import inspect

        assert inspect.isasyncgenfunction(engine.submit_tool_result)


# ---------------------------------------------------------------------------
# reconstruct_messages tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReconstructMessages:
    def test_empty_session(self, engine, session):
        messages = engine.reconstruct_messages(session)
        assert messages == []

    def test_text_only_conversation(self, engine, session):
        _msg(session, 0, "user", {"block_type": "text", "text": "Hello!"})
        _msg(session, 1, "assistant", {"block_type": "text", "text": "Hi there!"})

        messages = engine.reconstruct_messages(session)

        assert messages == [
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]

    def test_tool_calls_and_tool_response(self, engine, session):
        _msg(session, 0, "user", {"block_type": "text", "text": "Search for test"})
        _msg(
            session,
            1,
            "assistant",
            {"block_type": "thinking", "thinking": "Claude's, not sent"},
            {
                "block_type": "tool_use",
                "tool_use_id": "call_abc",
                "tool_name": "search_kb",
                "tool_input": {"query": "test"},
            },
        )
        _msg(
            session,
            2,
            "user",
            {
                "block_type": "tool_result",
                "tool_result_for": "call_abc",
                "tool_result_content": "Found 3 results",
            },
            {
                "block_type": "tool_result",
                "tool_result_for": "call_def",
                "tool_result_content": "Nothing",
            },
            {"block_type": "text", "text": "Also, hurry"},
        )

        messages = engine.reconstruct_messages(session)

        assert messages == [
            {"role": "user", "content": "Search for test"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "search_kb",
                            "arguments": '{"query": "test"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "Found 3 results"},
            {"role": "tool", "tool_call_id": "call_def", "content": "Nothing"},
            {"role": "user", "content": "Also, hurry"},
        ]

    def test_system_prompt_comes_from_the_session(self, engine, session):
        session.system_prompt = "You are a helpful assistant."
        session.save()
        _msg(session, 0, "user", {"block_type": "text", "text": "Hi"})

        messages = engine.reconstruct_messages(session)

        assert messages == [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hi"},
        ]

    def test_ordering_preserved(self, engine, session):
        """Messages must come back in sequence order."""
        # Insert deliberately out of sequence order
        _msg(session, 2, "assistant", {"block_type": "text", "text": "Response"})
        _msg(session, 1, "user", {"block_type": "text", "text": "Question"})

        messages = engine.reconstruct_messages(session)

        assert [m["role"] for m in messages] == ["user", "assistant"]


def _msg(session, sequence, role, *blocks):
    message = SessionMessage.objects.create(
        session=session, role=role, sequence=sequence
    )
    for i, block in enumerate(blocks):
        MessageBlock.objects.create(message=message, sequence=i, **block)
    return message


# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestStartSession:
    def test_start_session_without_workflow_returns_session_id(self, engine, session):
        from asgiref.sync import async_to_sync

        session_id = async_to_sync(engine.start_session)(session)
        assert session_id == str(session.id)

    def test_start_session_stores_no_system_message(
        self, engine, session_with_workflow
    ):
        from asgiref.sync import async_to_sync

        async_to_sync(engine.start_session)(session_with_workflow)
        assert not session_with_workflow.messages.exists()


class TestUsageTokens:
    """OpenAI counts cached tokens inside prompt_tokens; Ergo splits them out."""

    def test_splits_cached_tokens_out_of_the_prompt(self):
        from types import SimpleNamespace

        from django_ergo.conversation.engines.openai_api import usage_tokens

        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=50,
            prompt_tokens_details=SimpleNamespace(cached_tokens=800),
        )
        assert usage_tokens(usage) == (200, 800, 50)

    def test_without_details_or_usage(self):
        from types import SimpleNamespace

        from django_ergo.conversation.engines.openai_api import usage_tokens

        assert usage_tokens(SimpleNamespace(prompt_tokens=7, completion_tokens=3)) == (
            7,
            0,
            3,
        )
        assert usage_tokens(
            SimpleNamespace(
                prompt_tokens=7,
                completion_tokens=3,
                prompt_tokens_details={"cached_tokens": 4},
            )
        ) == (3, 4, 3)
        assert usage_tokens(None) == (None, None, None)


@pytest.mark.parametrize(
    ("arguments", "expected", "invalid"),
    [
        ('{"text":"é"}', {"text": "é"}, False),
        ('{"text":"é', {}, True),
        ("", {}, False),
        ("[1]", {}, True),
    ],
)
async def test_complete_preserves_argument_counts_on_length_stop(
    engine, arguments, expected, invalid
):
    from types import SimpleNamespace

    call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="write", arguments=arguments),
        model_dump=lambda: {
            "id": "call_1",
            "type": "function",
            "function": {"name": "write", "arguments": arguments},
        },
    )
    response = SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content=None, tool_calls=[call]),
            )
        ],
    )

    async def create(kwargs):
        return response

    engine._create_completion = create
    result = await engine.complete([{"role": "user", "content": "go"}])
    assert result.events[0].tool_use == {
        "id": "call_1",
        "name": "write",
        "input": expected,
        "argument_chars": len(arguments),
        **({"invalid_arguments": True} if invalid else {}),
    }
    assert result.events[-1].raw == {"finish_reason": "length"}
    stored = result.message["tool_calls"][0]["function"]["arguments"]
    assert stored == ("{}" if invalid else arguments)
