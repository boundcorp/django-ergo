"""Tests for conversation pipelines — summarize, compact, custom."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.pipelines import COMPACT_SYSTEM
from django_ergo.conversation.pipelines import SUMMARIZE_SYSTEM
from django_ergo.conversation.pipelines import CompactedConversation
from django_ergo.conversation.pipelines import CompactedMessage
from django_ergo.conversation.pipelines import _format_conversation_as_text
from django_ergo.conversation.pipelines import compact_conversation
from django_ergo.conversation.pipelines import run_conversation_pipeline
from django_ergo.conversation.pipelines import summarize_conversation

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def session_with_messages(user):
    session = ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="completed",
    )
    m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0, block_type="text", sequence=0, text="What files are in /tmp?"
    )

    m1 = ClaudeMessage.objects.create(
        session=session, role="assistant", sequence=1, stop_reason="tool_use"
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="tool_use",
        sequence=0,
        tool_use_id="toolu_01",
        tool_name="Bash",
        tool_input={"command": "ls /tmp"},
    )

    m2 = ClaudeMessage.objects.create(session=session, role="user", sequence=2)
    ClaudeContentBlock.objects.create(
        message=m2,
        block_type="tool_result",
        sequence=0,
        tool_result_for="toolu_01",
        tool_result_content="file1.txt\nfile2.txt",
    )

    m3 = ClaudeMessage.objects.create(
        session=session, role="assistant", sequence=3, stop_reason="end_turn"
    )
    ClaudeContentBlock.objects.create(
        message=m3,
        block_type="text",
        sequence=0,
        text="There are 2 files in /tmp: file1.txt and file2.txt.",
    )
    return session


class TestFormatConversation:
    def test_text_messages(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]},
        ]
        result = _format_conversation_as_text(messages)
        assert "[msg #0 USER]: Hello" in result
        assert "[msg #1 ASSISTANT]: Hi there!" in result

    def test_tool_use_and_result(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "file.txt",
                        "is_error": False,
                    },
                ],
            },
        ]
        result = _format_conversation_as_text(messages)
        assert "[msg #0 ASSISTANT tool_call #1]: Bash(" in result
        assert "[msg #1 TOOL_RESULT #1]: file.txt" in result

    def test_tool_result_error(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "not found",
                        "is_error": True,
                    },
                ],
            },
        ]
        result = _format_conversation_as_text(messages)
        assert "[msg #0 TOOL_RESULT #?" in result
        assert "ERROR" in result

    def test_thinking_block(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me analyze..."},
                    {"type": "text", "text": "Here's my answer."},
                ],
            },
        ]
        result = _format_conversation_as_text(messages)
        assert "[msg #0 ASSISTANT thinking]: Let me analyze..." in result
        assert "[msg #0 ASSISTANT]: Here's my answer." in result

    def test_string_content(self):
        """OpenAI-style messages with plain string content."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = _format_conversation_as_text(messages)
        assert "[msg #0 USER]: Hello" in result
        assert "[msg #1 ASSISTANT]: Hi!" in result


class TestRunConversationPipeline:
    def test_pipeline_calls_generate(self, session_with_messages):
        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]},
        ]
        engine.generate = AsyncMock(
            return_value=EngineResponse(
                event_type="done",
                text="Summary of the conversation.",
            )
        )

        result = async_to_sync(run_conversation_pipeline)(
            session_with_messages,
            engine,
            system="Summarize this.",
        )

        engine.generate.assert_awaited_once()
        call_kwargs = engine.generate.call_args.kwargs
        assert "conversation transcript" in call_kwargs["prompt"].lower()
        assert "[msg #0 USER]: Hello" in call_kwargs["prompt"]
        assert call_kwargs["system"] == "Summarize this."
        assert call_kwargs["response_model"] is None
        assert result.text == "Summary of the conversation."

    def test_pipeline_with_response_model(self, session_with_messages):
        from pydantic import BaseModel

        class ActionItems(BaseModel):
            items: list[str]

        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Do X and Y"}]},
        ]
        parsed = ActionItems(items=["Do X", "Do Y"])
        engine.generate = AsyncMock(
            return_value=EngineResponse(
                event_type="done",
                raw={"parsed": parsed},
            )
        )

        result = async_to_sync(run_conversation_pipeline)(
            session_with_messages,
            engine,
            system="Extract action items.",
            response_model=ActionItems,
        )

        call_kwargs = engine.generate.call_args.kwargs
        assert call_kwargs["response_model"] is ActionItems
        assert result.raw["parsed"].items == ["Do X", "Do Y"]


class TestSummarizeConversation:
    def test_returns_text(self, session_with_messages):
        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ]
        engine.generate = AsyncMock(
            return_value=EngineResponse(
                event_type="done",
                text="The user greeted the assistant.",
            )
        )

        result = async_to_sync(summarize_conversation)(session_with_messages, engine)

        assert result == "The user greeted the assistant."
        call_kwargs = engine.generate.call_args.kwargs
        assert call_kwargs["system"] == SUMMARIZE_SYSTEM

    def test_empty_response(self, session_with_messages):
        engine = MagicMock()
        engine.reconstruct_messages.return_value = []
        engine.generate = AsyncMock(return_value=EngineResponse(event_type="done"))

        result = async_to_sync(summarize_conversation)(session_with_messages, engine)
        assert result == ""


class TestCompactConversation:
    def test_returns_compacted(self, session_with_messages):
        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "What files?"}]},
        ]
        compacted = CompactedConversation(
            title="File listing",
            messages=[
                CompactedMessage(role="user", content="Asked about files in /tmp"),
                CompactedMessage(
                    role="assistant", content="Found file1.txt and file2.txt"
                ),
            ],
            context_notes="User was exploring the /tmp directory.",
        )
        engine.generate = AsyncMock(
            return_value=EngineResponse(
                event_type="done",
                raw={"parsed": compacted},
            )
        )

        result = async_to_sync(compact_conversation)(session_with_messages, engine)

        expected_message_count = 2
        assert isinstance(result, CompactedConversation)
        assert result.title == "File listing"
        assert len(result.messages) == expected_message_count
        assert result.context_notes == "User was exploring the /tmp directory."
        call_kwargs = engine.generate.call_args.kwargs
        assert call_kwargs["system"] == COMPACT_SYSTEM
        assert call_kwargs["response_model"] is CompactedConversation


class TestRendererIntegration:
    def test_pipeline_uses_skeleton_by_default(self, session_with_messages):
        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ]
        engine.generate = AsyncMock(
            return_value=EngineResponse(event_type="done", text="Summary"),
        )

        async_to_sync(run_conversation_pipeline)(
            session_with_messages,
            engine,
            system="Summarize.",
        )

        call_kwargs = engine.generate.call_args.kwargs
        # Should use skeleton format (numbered messages)
        assert "[msg #0" in call_kwargs["prompt"]

    def test_pipeline_accepts_custom_renderer(self, session_with_messages):
        from django_ergo.conversation.renderer import ConversationRenderer

        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ]
        engine.generate = AsyncMock(
            return_value=EngineResponse(event_type="done", text="Summary"),
        )

        renderer = ConversationRenderer(detail="full")
        async_to_sync(run_conversation_pipeline)(
            session_with_messages,
            engine,
            system="Summarize.",
            renderer=renderer,
        )

        call_kwargs = engine.generate.call_args.kwargs
        # Full detail should be in prompt
        assert "[msg #0" in call_kwargs["prompt"]
