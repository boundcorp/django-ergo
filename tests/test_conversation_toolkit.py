# tests/test_conversation_toolkit.py
"""Tests for ChatWithHistoryToolkit scoped tools."""

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def session_a(user):
    s = ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="completed",
        metadata={"slug": "red-fox-jumps"},
    )
    m0 = ClaudeMessage.objects.create(session=s, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0, block_type="text", sequence=0, text="List files"
    )
    m1 = ClaudeMessage.objects.create(
        session=s, role="assistant", sequence=1, stop_reason="tool_use"
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="tool_use",
        sequence=0,
        tool_use_id="toolu_01",
        tool_name="Bash",
        tool_input={"command": "ls /tmp"},
    )
    m2 = ClaudeMessage.objects.create(session=s, role="user", sequence=2)
    ClaudeContentBlock.objects.create(
        message=m2,
        block_type="tool_result",
        sequence=0,
        tool_result_for="toolu_01",
        tool_result_content="file1.txt\nfile2.txt",
    )
    m3 = ClaudeMessage.objects.create(
        session=s, role="assistant", sequence=3, stop_reason="end_turn"
    )
    ClaudeContentBlock.objects.create(
        message=m3,
        block_type="text",
        sequence=0,
        text="Found 2 files.",
    )
    return s


@pytest.fixture()
def session_b(user):
    s = ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="completed",
    )
    m0 = ClaudeMessage.objects.create(session=s, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0, block_type="text", sequence=0, text="Explain auth"
    )
    m1 = ClaudeMessage.objects.create(
        session=s, role="assistant", sequence=1, stop_reason="end_turn"
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="thinking",
        sequence=0,
        thinking="Let me think about auth...",
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="text",
        sequence=1,
        text="Auth uses JWT tokens.",
    )
    return s


@pytest.fixture()
def toolkit(session_a, session_b):
    return ChatWithHistoryToolkit(sessions=[session_a, session_b])


class TestRenderOverview:
    def test_includes_all_sessions(self, toolkit, session_a, session_b):
        overview = toolkit.render_overview()
        assert str(session_a.id) in overview
        assert str(session_b.id) in overview
        assert "List files" in overview
        assert "Explain auth" in overview

    def test_uses_skeleton_by_default(self, toolkit):
        overview = toolkit.render_overview()
        # Skeleton omits thinking
        assert "Let me think" not in overview
        # Skeleton summarizes tool calls
        assert "[tool_call #1:" in overview


class TestHasTool:
    def test_recognizes_own_tools(self, toolkit):
        assert toolkit.has_tool("history_view_conversation") is True
        assert toolkit.has_tool("history_get_tool_call") is True
        assert toolkit.has_tool("history_get_message_range") is True
        assert toolkit.has_tool("history_get_thinking") is True

    def test_rejects_unknown_tools(self, toolkit):
        assert toolkit.has_tool("search_kb") is False
        assert toolkit.has_tool("view_conversation") is False


class TestExecuteViewConversation:
    def test_view_skeleton(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_view_conversation", {"session_id": str(session_a.id)}
        )
        assert "List files" in result
        assert "[tool_call #1:" in result

    def test_view_full(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_view_conversation",
            {"session_id": str(session_a.id), "detail": "full"},
        )
        assert "file1.txt" in result

    def test_invalid_session(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "history_view_conversation", {"session_id": "nonexistent"}
            )


class TestExecuteGetToolCall:
    def test_get_tool_call(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_tool_call",
            {"session_id": str(session_a.id), "tool_call_number": 1},
        )
        assert "Bash" in result
        assert "ls /tmp" in result
        assert "file1.txt" in result

    def test_invalid_tool_call_number(self, toolkit, session_a):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "history_get_tool_call",
                {"session_id": str(session_a.id), "tool_call_number": 99},
            )


class TestExecuteGetMessageRange:
    def test_get_range(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_message_range",
            {"session_id": str(session_a.id), "start": 0, "end": 1},
        )
        assert "List files" in result
        assert "Bash" in result

    def test_single_message(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_message_range",
            {"session_id": str(session_a.id), "start": 3, "end": 3},
        )
        assert "Found 2 files" in result


class TestExecuteGetThinking:
    def test_get_thinking(self, toolkit, session_b):
        result = toolkit.execute_tool(
            "history_get_thinking",
            {"session_id": str(session_b.id), "message_number": 1},
        )
        assert "Let me think about auth" in result

    def test_no_thinking_in_message(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_thinking",
            {"session_id": str(session_a.id), "message_number": 0},
        )
        assert "no thinking" in result.lower() or result == ""


class TestGetToolsSchema:
    def test_returns_tool_configs(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        tool_names = [s["name"] for s in schemas]
        assert "history_view_conversation" in tool_names
        assert "history_get_tool_call" in tool_names
        assert "history_get_message_range" in tool_names
        assert "history_get_thinking" in tool_names
