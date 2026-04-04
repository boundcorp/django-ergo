"""Tests for KB pipelines — absorb_conversation."""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.models import ConversationSession
from django_ergo.kb_pipelines import ABSORB_SYSTEM
from django_ergo.kb_pipelines import absorb_conversation
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def source_session(user):
    session = ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="completed",
    )
    from django_ergo.conversation.models import ClaudeContentBlock
    from django_ergo.conversation.models import ClaudeMessage

    m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0,
        block_type="text",
        sequence=0,
        text="I prefer morning deployments and use pytest for testing.",
    )
    m1 = ClaudeMessage.objects.create(
        session=session,
        role="assistant",
        sequence=1,
        stop_reason="end_turn",
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="text",
        sequence=1,
        text="Got it, I'll remember your preferences.",
    )
    return session


@pytest.fixture()
def target_kb(user):
    kb = Knowledgebase.objects.create(
        name="Personal Notes",
        description="Facts about the user's preferences and habits",
        owner_id=str(user.id),
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="0",
        title="Tools",
        content="The user likes VS Code.",
    )
    return kb


class TestAbsorbSystem:
    def test_prompt_template_has_placeholders(self):
        assert "{kb_name}" in ABSORB_SYSTEM
        assert "{kb_description}" in ABSORB_SYSTEM
        assert "{kb_toc}" in ABSORB_SYSTEM

    def test_prompt_can_be_formatted(self, target_kb):
        formatted = ABSORB_SYSTEM.format(
            kb_name=target_kb.name,
            kb_description=target_kb.description,
            kb_toc=target_kb.get_table_of_contents(),
        )
        assert "Personal Notes" in formatted
        assert "preferences and habits" in formatted


class TestAbsorbConversation:
    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_returns_suggest_toolkit(self, mock_runner, source_session, target_kb):
        from django_ergo.kb_suggest_toolkit import KBSuggestToolkit

        async def fake_runner(*args, **kwargs):
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    tk.execute_tool(
                        "kb_suggest_create",
                        {
                            "title": "Deploy Preferences",
                            "content": "User prefers morning deployments.",
                        },
                    )
            yield EngineResponse(event_type="done", text="Absorbed.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        result = async_to_sync(absorb_conversation)(
            source_session,
            target_kb,
            engine,
        )

        assert isinstance(result, KBSuggestToolkit)
        suggestions = result.get_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["action"] == "create"
        assert suggestions[0]["title"] == "Deploy Preferences"

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_creates_absorption_session(self, mock_runner, source_session, target_kb):
        async def fake_runner(*args, **kwargs):
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        initial_count = ConversationSession.objects.count()
        async_to_sync(absorb_conversation)(source_session, target_kb, engine)
        assert ConversationSession.objects.count() == initial_count + 1

        absorption_session = ConversationSession.objects.order_by("-created_at").first()
        assert absorption_session.metadata.get("absorption_source") == str(
            source_session.id
        )

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_prompt_includes_conversation_transcript(
        self, mock_runner, source_session, target_kb
    ):
        captured_message = {}

        async def fake_runner(engine, session, message, **kwargs):
            captured_message["msg"] = message
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        async_to_sync(absorb_conversation)(source_session, target_kb, engine)

        assert "morning deployments" in captured_message["msg"]

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_prompt_includes_kb_context(self, mock_runner, source_session, target_kb):
        captured_message = {}

        async def fake_runner(engine, session, message, **kwargs):
            captured_message["msg"] = message
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        async_to_sync(absorb_conversation)(source_session, target_kb, engine)

        msg = captured_message["msg"]
        assert "Personal Notes" in msg
        assert "preferences and habits" in msg

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_custom_system_prompt(self, mock_runner, source_session, target_kb):
        captured_message = {}

        async def fake_runner(engine, session, message, **kwargs):
            captured_message["msg"] = message
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        async_to_sync(absorb_conversation)(
            source_session,
            target_kb,
            engine,
            system="Only extract deployment preferences.",
        )

        msg = captured_message["msg"]
        assert "Only extract deployment preferences" in msg

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_passes_both_toolkits(self, mock_runner, source_session, target_kb):
        captured_tools = {}

        async def fake_runner(engine, session, message, extra_tools=None):
            captured_tools["tools"] = extra_tools
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        async_to_sync(absorb_conversation)(source_session, target_kb, engine)

        tools = captured_tools["tools"]
        expected_toolkit_count = 2
        assert len(tools) == expected_toolkit_count
        tool_types = {type(t).__name__ for t in tools}
        assert "KBSuggestToolkit" in tool_types
        assert "KBToolkit" in tool_types

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_no_suggestions_returns_empty(self, mock_runner, source_session, target_kb):
        async def fake_runner(*args, **kwargs):
            yield EngineResponse(event_type="done", text="Nothing to absorb.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
            "name": "mock"
        }

        result = async_to_sync(absorb_conversation)(
            source_session,
            target_kb,
            engine,
        )
        assert result.get_suggestions() == []
