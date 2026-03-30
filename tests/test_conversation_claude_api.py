"""Tests for ClaudeAPIEngine."""

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.engines import ENGINE_REGISTRY
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ClaudeMessageRole
from django_ergo.conversation.models import ContentBlockType
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import EngineType
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
    return ClaudeAPIEngine(
        config={
            "model": "claude-3-5-sonnet-20241022",
            "api_key": "test-key",
            "max_tokens": 1024,
        }
    )


@pytest.fixture()
def user(db):
    return User.objects.create_user(
        username="claudetest",
        email="claude@example.com",
        password="testpass123",
    )


@pytest.fixture()
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type=EngineType.CLAUDE,
        transport_type=TransportType.API,
        status=SessionStatus.ACTIVE,
    )


@pytest.fixture()
def workflow(db):
    return Workflow.objects.create(
        name="Test Workflow",
        description="A test workflow",
        instructions="Be helpful.",
        tools_config={},
    )


# ---------------------------------------------------------------------------
# ENGINE_REGISTRY tests
# ---------------------------------------------------------------------------


class TestEngineRegistry:
    def test_claude_api_registered(self):
        assert ("claude", "api") in ENGINE_REGISTRY

    def test_claude_api_path(self):
        assert ENGINE_REGISTRY[("claude", "api")] == (
            "django_ergo.conversation.engines.claude_api.ClaudeAPIEngine"
        )

    def test_claude_cli_registered(self):
        assert ("claude", "cli") in ENGINE_REGISTRY

    def test_openai_api_registered(self):
        assert ("openai", "api") in ENGINE_REGISTRY


# ---------------------------------------------------------------------------
# ClaudeAPIEngine unit tests (no DB needed)
# ---------------------------------------------------------------------------


class TestClaudeAPIEngineInit:
    def test_engine_type(self, engine):
        assert engine.engine_type == "claude"

    def test_config_stored(self, engine):
        assert engine.model == "claude-3-5-sonnet-20241022"
        assert engine.api_key == "test-key"
        assert engine.max_tokens == 1024  # noqa: PLR2004

    def test_default_config(self):
        e = ClaudeAPIEngine(config={})
        assert e.model == "claude-3-5-sonnet-20241022"
        assert e.max_tokens == 8192  # noqa: PLR2004
        assert e.api_key is None

    def test_client_not_eagerly_initialized(self, engine):
        assert engine._client is None  # noqa: SLF001

    def test_get_tool_adapter_returns_claude_adapter(self, engine):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = engine.get_tool_adapter()
        assert isinstance(adapter, ClaudeToolAdapter)


# ---------------------------------------------------------------------------
# reconstruct_messages tests — require DB
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestReconstructMessages:
    def test_empty_session_returns_empty_list(self, engine, session):
        result = engine.reconstruct_messages(session)
        assert result == []

    def test_text_only_conversation(self, engine, session):
        user_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        ClaudeContentBlock.objects.create(
            message=user_msg,
            block_type=ContentBlockType.TEXT,
            sequence=0,
            text="Hello, Claude!",
        )

        assistant_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=1,
            stop_reason="end_turn",
        )
        ClaudeContentBlock.objects.create(
            message=assistant_msg,
            block_type=ContentBlockType.TEXT,
            sequence=0,
            text="Hello! How can I help?",
        )

        result = engine.reconstruct_messages(session)

        assert len(result) == 2  # noqa: PLR2004
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [{"type": "text", "text": "Hello, Claude!"}]
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == [
            {"type": "text", "text": "Hello! How can I help?"}
        ]

    def test_tool_use_and_tool_result_blocks(self, engine, session):
        # Assistant message with a tool_use block
        assistant_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=0,
            stop_reason="tool_use",
        )
        ClaudeContentBlock.objects.create(
            message=assistant_msg,
            block_type=ContentBlockType.TOOL_USE,
            sequence=0,
            tool_use_id="toolu_01abc",
            tool_name="search_kb",
            tool_input={"query": "django models"},
        )

        # User message with a tool_result block
        user_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.USER,
            sequence=1,
        )
        ClaudeContentBlock.objects.create(
            message=user_msg,
            block_type=ContentBlockType.TOOL_RESULT,
            sequence=0,
            tool_result_for="toolu_01abc",
            tool_result_content="Found 3 results",
            is_error=False,
        )

        result = engine.reconstruct_messages(session)

        assert len(result) == 2  # noqa: PLR2004

        tool_use_block = result[0]["content"][0]
        assert tool_use_block["type"] == "tool_use"
        assert tool_use_block["id"] == "toolu_01abc"
        assert tool_use_block["name"] == "search_kb"
        assert tool_use_block["input"] == {"query": "django models"}

        tool_result_block = result[1]["content"][0]
        assert tool_result_block["type"] == "tool_result"
        assert tool_result_block["tool_use_id"] == "toolu_01abc"
        assert tool_result_block["content"] == "Found 3 results"
        assert tool_result_block["is_error"] is False

    def test_tool_result_error_flag(self, engine, session):
        user_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        ClaudeContentBlock.objects.create(
            message=user_msg,
            block_type=ContentBlockType.TOOL_RESULT,
            sequence=0,
            tool_result_for="toolu_err",
            tool_result_content="Something went wrong",
            is_error=True,
        )

        result = engine.reconstruct_messages(session)
        assert result[0]["content"][0]["is_error"] is True

    def test_thinking_blocks(self, engine, session):
        assistant_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=0,
        )
        ClaudeContentBlock.objects.create(
            message=assistant_msg,
            block_type=ContentBlockType.THINKING,
            sequence=0,
            thinking="Let me reason step by step...",
        )
        ClaudeContentBlock.objects.create(
            message=assistant_msg,
            block_type=ContentBlockType.TEXT,
            sequence=1,
            text="Here is my answer.",
        )

        result = engine.reconstruct_messages(session)

        assert len(result) == 1
        content = result[0]["content"]
        assert len(content) == 2  # noqa: PLR2004
        assert content[0] == {
            "type": "thinking",
            "thinking": "Let me reason step by step...",
        }
        assert content[1] == {"type": "text", "text": "Here is my answer."}

    def test_tool_input_defaults_to_empty_dict_when_null(self, engine, session):
        assistant_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=0,
        )
        ClaudeContentBlock.objects.create(
            message=assistant_msg,
            block_type=ContentBlockType.TOOL_USE,
            sequence=0,
            tool_use_id="toolu_null",
            tool_name="no_args_tool",
            tool_input=None,
        )

        result = engine.reconstruct_messages(session)
        assert result[0]["content"][0]["input"] == {}

    def test_tool_result_content_defaults_to_empty_string_when_null(
        self, engine, session
    ):
        user_msg = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        ClaudeContentBlock.objects.create(
            message=user_msg,
            block_type=ContentBlockType.TOOL_RESULT,
            sequence=0,
            tool_result_for="toolu_null",
            tool_result_content=None,
            is_error=False,
        )

        result = engine.reconstruct_messages(session)
        assert result[0]["content"][0]["content"] == ""

    def test_message_ordering_by_sequence(self, engine, session):
        """Messages should be returned in sequence order."""
        msg2 = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=1,
        )
        ClaudeContentBlock.objects.create(
            message=msg2,
            block_type=ContentBlockType.TEXT,
            sequence=0,
            text="Second",
        )
        msg1 = ClaudeMessage.objects.create(
            session=session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        ClaudeContentBlock.objects.create(
            message=msg1,
            block_type=ContentBlockType.TEXT,
            sequence=0,
            text="First",
        )

        result = engine.reconstruct_messages(session)
        assert result[0]["content"][0]["text"] == "First"
        assert result[1]["content"][0]["text"] == "Second"


# ---------------------------------------------------------------------------
# get_tools_schema tests — require DB
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestGetToolsSchema:
    def test_empty_workflow_tools_config(self, engine, workflow):
        """Workflow with no enabled_tools returns empty list."""
        result = engine.get_tools_schema(workflow)
        assert result == []

    def test_none_workflow_returns_empty_list(self, engine):
        result = engine.get_tools_schema(None)
        assert result == []

    def test_enabled_tools_in_registry(self, engine, workflow):
        """Tools listed in enabled_tools that exist in the registry are returned."""
        # Register a temporary tool for this test
        test_tool = ToolConfig(
            name="_test_claude_api_tool",
            description="A test tool for ClaudeAPIEngine tests",
            parameters={
                "query": {
                    "type": "string",
                    "required": True,
                    "description": "Search query",
                }
            },
        )
        tool_registry._tools["_test_claude_api_tool"] = test_tool  # noqa: SLF001

        workflow.tools_config = {"enabled_tools": ["_test_claude_api_tool"]}
        workflow.save()

        try:
            result = engine.get_tools_schema(workflow)
            assert len(result) == 1
            schema = result[0]
            assert schema["name"] == "_test_claude_api_tool"
            assert schema["description"] == "A test tool for ClaudeAPIEngine tests"
            assert "input_schema" in schema
            assert "query" in schema["input_schema"]["properties"]
        finally:
            # Clean up the temporary tool
            tool_registry._tools.pop("_test_claude_api_tool", None)  # noqa: SLF001

    def test_unknown_tool_names_are_skipped(self, engine, workflow):
        """Tools that are not in the registry are silently skipped."""
        workflow.tools_config = {"enabled_tools": ["nonexistent_tool_xyz"]}
        workflow.save()

        result = engine.get_tools_schema(workflow)
        assert result == []

    def test_schema_uses_claude_format(self, engine, workflow):
        """Returned schema uses Claude input_schema format, not OpenAI function format."""
        test_tool = ToolConfig(
            name="_test_claude_format_tool",
            description="Format check tool",
            parameters={},
        )
        tool_registry._tools["_test_claude_format_tool"] = test_tool  # noqa: SLF001

        workflow.tools_config = {"enabled_tools": ["_test_claude_format_tool"]}
        workflow.save()

        try:
            result = engine.get_tools_schema(workflow)
            assert len(result) == 1
            # Claude format has "input_schema", not OpenAI's "function" wrapper
            assert "input_schema" in result[0]
            assert "function" not in result[0]
        finally:
            tool_registry._tools.pop("_test_claude_format_tool", None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# No-op lifecycle method tests
# ---------------------------------------------------------------------------


class TestLifecycleMethods:
    async def test_start_session_returns_empty_string(self, engine, session):
        result = await engine.start_session(session)
        assert result == ""

    async def test_resume_session_is_noop(self, engine, session):
        result = await engine.resume_session(session)
        assert result is None

    async def test_close_session_is_noop(self, engine, session):
        result = await engine.close_session(session)
        assert result is None

    async def test_send_raises_not_implemented(self, engine, session):
        with pytest.raises(NotImplementedError):
            await engine.send(session, "hello")

    async def test_submit_tool_result_raises_not_implemented(self, engine, session):
        with pytest.raises(NotImplementedError):
            await engine.submit_tool_result(session, "toolu_01", "result")
