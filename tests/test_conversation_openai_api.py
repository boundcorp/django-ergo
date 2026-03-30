"""Tests for OpenAIAPIEngine."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import EngineType
from django_ergo.conversation.models import OpenAIMessage
from django_ergo.conversation.models import OpenAIMessageRole
from django_ergo.conversation.models import SessionStatus
from django_ergo.conversation.models import TransportType
from django_ergo.models import Workflow
from django_ergo.tools import ToolConfig
from django_ergo.tools import tool_registry

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    return OpenAIAPIEngine(
        config={
            "model": "gpt-4o",
            "api_key": "test-key",
            "temperature": 0.5,
            "max_tokens": 1024,
        }
    )


@pytest.fixture()
def user(db):
    return User.objects.create_user(
        username="testuser_openai",
        email="openai@example.com",
        password="testpass123",
    )


@pytest.fixture()
def workflow(db):
    return Workflow.objects.create(
        name="OpenAI Test Workflow",
        description="A workflow for testing OpenAI engine",
        instructions="You are a helpful assistant.",
        tools_config={},
    )


@pytest.fixture()
def session(db, user):
    return ConversationSession.objects.create(
        user=user,
        engine_type=EngineType.OPENAI,
        transport_type=TransportType.API,
        status=SessionStatus.ACTIVE,
    )


@pytest.fixture()
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


@pytest.mark.django_db()
class TestReconstructMessages:
    def test_empty_session(self, engine, session):
        messages = engine.reconstruct_messages(session)
        assert messages == []

    def test_text_only_conversation(self, engine, session):
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.USER,
            content="Hello!",
            sequence=0,
        )
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.ASSISTANT,
            content="Hi there!",
            sequence=1,
        )

        messages = engine.reconstruct_messages(session)

        expected_count = 2
        assert len(messages) == expected_count
        assert messages[0] == {"role": "user", "content": "Hello!"}
        assert messages[1] == {"role": "assistant", "content": "Hi there!"}

    def test_tool_calls_and_tool_response(self, engine, session):
        tool_calls = [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "search_kb", "arguments": '{"query": "test"}'},
            }
        ]
        # User turn
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.USER,
            content="Search for test",
            sequence=0,
        )
        # Assistant calls a tool
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.ASSISTANT,
            content=None,
            tool_calls=tool_calls,
            sequence=1,
        )
        # Tool result
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.TOOL,
            content="Found 3 results",
            tool_call_id="call_abc",
            sequence=2,
        )

        messages = engine.reconstruct_messages(session)

        expected_count = 3
        assert len(messages) == expected_count

        # User message
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Search for test"
        assert "tool_calls" not in messages[0]
        assert "tool_call_id" not in messages[0]

        # Assistant with tool_calls
        assert messages[1]["role"] == "assistant"
        assert messages[1]["tool_calls"] == tool_calls

        # Tool response
        assert messages[2]["role"] == "tool"
        assert messages[2]["content"] == "Found 3 results"
        assert messages[2]["tool_call_id"] == "call_abc"

    def test_system_message_included(self, engine, session):
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.SYSTEM,
            content="You are a helpful assistant.",
            sequence=0,
        )
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.USER,
            content="Hi",
            sequence=1,
        )

        messages = engine.reconstruct_messages(session)

        expected_count = 2
        assert len(messages) == expected_count
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are a helpful assistant."
        assert messages[1]["role"] == "user"

    def test_ordering_preserved(self, engine, session):
        """Messages must come back in sequence order."""
        # Insert deliberately out of sequence order
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.ASSISTANT,
            content="Response",
            sequence=2,
        )
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.SYSTEM,
            content="System",
            sequence=0,
        )
        OpenAIMessage.objects.create(
            session=session,
            role=OpenAIMessageRole.USER,
            content="Question",
            sequence=1,
        )

        messages = engine.reconstruct_messages(session)

        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"


# ---------------------------------------------------------------------------
# get_tools_schema tests
# ---------------------------------------------------------------------------


class TestGetToolsSchema:
    def test_get_tools_schema_returns_list(self, engine, workflow):
        schema = engine.get_tools_schema(workflow)
        assert isinstance(schema, list)

    def test_get_tools_schema_format(self, engine, workflow):
        """Each tool must follow OpenAI function-calling format."""
        tool_name = "_test_openai_schema_tool"
        test_tool = ToolConfig(
            name=tool_name,
            description="A test tool",
            parameters={
                "query": {
                    "type": "string",
                    "required": True,
                    "description": "Search query",
                }
            },
        )
        # Insert directly via public-facing dict obtained through vars()
        tools_store = vars(tool_registry)["_tools"]
        tools_store[tool_name] = test_tool

        try:
            schema = engine.get_tools_schema(workflow)
            tool_schemas = {s["function"]["name"]: s for s in schema}
            assert tool_name in tool_schemas

            entry = tool_schemas[tool_name]
            assert entry["type"] == "function"
            assert entry["function"]["description"] == "A test tool"
            assert "query" in entry["function"]["parameters"]["properties"]
            assert "query" in entry["function"]["parameters"]["required"]
        finally:
            del tools_store[tool_name]


# ---------------------------------------------------------------------------
# start_session tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestStartSession:
    def test_start_session_without_workflow_returns_session_id(self, engine, session):
        from asgiref.sync import async_to_sync

        session_id = async_to_sync(engine.start_session)(session)
        assert session_id == str(session.id)

    def test_start_session_without_workflow_no_system_message(self, engine, session):
        from asgiref.sync import async_to_sync

        async_to_sync(engine.start_session)(session)
        assert OpenAIMessage.objects.filter(session=session).count() == 0

    def test_start_session_with_workflow_creates_system_message(
        self, engine, session_with_workflow
    ):
        from asgiref.sync import async_to_sync

        async_to_sync(engine.start_session)(session_with_workflow)
        system_msgs = OpenAIMessage.objects.filter(
            session=session_with_workflow,
            role=OpenAIMessageRole.SYSTEM,
        )
        assert system_msgs.count() == 1
        assert system_msgs.first().content == "You are a helpful assistant."
        assert system_msgs.first().sequence == 0
