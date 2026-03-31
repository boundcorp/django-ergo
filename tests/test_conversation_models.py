"""
Tests for conversation storage models:
ConversationSession, ClaudeMessage, ClaudeContentBlock, OpenAIMessage
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ClaudeMessageRole
from django_ergo.conversation.models import ContentBlockType
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import EngineType
from django_ergo.conversation.models import OpenAIMessage
from django_ergo.conversation.models import OpenAIMessageRole
from django_ergo.conversation.models import SessionStatus
from django_ergo.conversation.models import TransportType
from django_ergo.models import Workflow

User = get_user_model()


class ConversationSessionTestCase(TestCase):
    """Tests for ConversationSession model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.workflow = Workflow.objects.create(
            name="Test Workflow",
            description="A test workflow",
            instructions="Be helpful.",
            tools_config={},
        )

    def test_session_creation_all_fields(self):
        """Test creating a session with all fields set."""
        session = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.API,
            session_id="sess-abc123",
            status=SessionStatus.ACTIVE,
            metadata={"key": "value"},
        )
        self.assertIsNotNone(session.id)
        self.assertEqual(session.user, self.user)
        self.assertEqual(session.engine_type, "claude")
        self.assertEqual(session.transport_type, "api")
        self.assertEqual(session.session_id, "sess-abc123")
        self.assertEqual(session.status, "active")
        self.assertEqual(session.metadata, {"key": "value"})
        self.assertIsNone(session.workflow)
        self.assertIsNotNone(session.created_at)
        self.assertIsNotNone(session.updated_at)

    def test_session_with_workflow_fk(self):
        """Test session with a workflow foreign key."""
        session = ConversationSession.objects.create(
            user=self.user,
            workflow=self.workflow,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.CLI,
            status=SessionStatus.ACTIVE,
        )
        self.assertEqual(session.workflow, self.workflow)
        self.assertIn(session, self.workflow.conversation_sessions.all())

    def test_session_workflow_set_null_on_delete(self):
        """Test that workflow is set to null when workflow is deleted."""
        session = ConversationSession.objects.create(
            user=self.user,
            workflow=self.workflow,
            engine_type=EngineType.OPENAI,
            transport_type=TransportType.API,
            status=SessionStatus.ACTIVE,
        )
        self.workflow.delete()
        session.refresh_from_db()
        self.assertIsNone(session.workflow)

    def test_session_ordering_most_recent_first(self):
        """Test that sessions are ordered by -created_at (most recent first)."""
        session1 = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.API,
            status=SessionStatus.COMPLETED,
        )
        session2 = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.OPENAI,
            transport_type=TransportType.API,
            status=SessionStatus.ACTIVE,
        )
        sessions = list(ConversationSession.objects.all())
        # Most recent (session2) should come first
        self.assertEqual(sessions[0].id, session2.id)
        self.assertEqual(sessions[1].id, session1.id)

    def test_session_default_session_id_and_metadata(self):
        """Test default values for session_id and metadata."""
        session = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.CLI,
            status=SessionStatus.ACTIVE,
        )
        self.assertEqual(session.session_id, "")
        self.assertEqual(session.metadata, {})

    def test_all_engine_types(self):
        """Test both engine_type choices."""
        for engine in [EngineType.CLAUDE, EngineType.OPENAI]:
            session = ConversationSession.objects.create(
                user=self.user,
                engine_type=engine,
                transport_type=TransportType.API,
                status=SessionStatus.ACTIVE,
            )
            self.assertEqual(session.engine_type, engine)

    def test_all_session_statuses(self):
        """Test all status choices."""
        for status in [
            SessionStatus.ACTIVE,
            SessionStatus.PAUSED,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
        ]:
            session = ConversationSession.objects.create(
                user=self.user,
                engine_type=EngineType.CLAUDE,
                transport_type=TransportType.API,
                status=status,
            )
            self.assertEqual(session.status, status)

    def test_session_str(self):
        """Test __str__ representation."""
        session = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.API,
            status=SessionStatus.ACTIVE,
        )
        self.assertIn("claude", str(session))
        self.assertIn("active", str(session))


class ClaudeMessageTestCase(TestCase):
    """Tests for ClaudeMessage model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.session = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.API,
            status=SessionStatus.ACTIVE,
        )

    def test_claude_message_creation(self):
        """Test creating a ClaudeMessage with all fields."""
        msg = ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        self.assertIsNotNone(msg.id)
        self.assertEqual(msg.session, self.session)
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.sequence, 0)
        self.assertIsNone(msg.stop_reason)
        self.assertIsNone(msg.input_tokens)
        self.assertIsNone(msg.output_tokens)

    def test_claude_message_with_usage_tokens(self):
        """Test creating a ClaudeMessage with token usage data."""
        msg = ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=1,
            stop_reason="end_turn",
            input_tokens=100,
            output_tokens=250,
        )
        self.assertEqual(msg.stop_reason, "end_turn")
        self.assertEqual(msg.input_tokens, 100)
        self.assertEqual(msg.output_tokens, 250)

    def test_claude_message_cost_tracking_fields(self):
        """Test creating a ClaudeMessage with cost tracking fields."""
        msg = ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=1,
            stop_reason="end_turn",
            input_tokens=500,
            output_tokens=300,
            model_name="claude-3-5-sonnet-20241022",
            cache_creation_input_tokens=100,
            cache_read_input_tokens=50,
        )
        self.assertEqual(msg.model_name, "claude-3-5-sonnet-20241022")
        self.assertEqual(msg.cache_creation_input_tokens, 100)
        self.assertEqual(msg.cache_read_input_tokens, 50)

    def test_claude_message_cost_tracking_fields_default_null(self):
        """Test that cost tracking fields default to None."""
        msg = ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        self.assertIsNone(msg.model_name)
        self.assertIsNone(msg.cache_creation_input_tokens)
        self.assertIsNone(msg.cache_read_input_tokens)

    def test_claude_message_ordering_by_sequence(self):
        """Test that messages are ordered by sequence."""
        ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=2,
        )
        ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.USER,
            sequence=1,
        )
        messages = list(ClaudeMessage.objects.filter(session=self.session))
        self.assertEqual(messages[0].sequence, 0)
        self.assertEqual(messages[1].sequence, 1)
        self.assertEqual(messages[2].sequence, 2)

    def test_claude_message_cascade_delete(self):
        """Test that messages are deleted when session is deleted."""
        ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.USER,
            sequence=0,
        )
        session_id = self.session.id
        self.session.delete()
        self.assertEqual(ClaudeMessage.objects.filter(session_id=session_id).count(), 0)

    def test_claude_message_both_roles(self):
        """Test both message roles."""
        for i, role in enumerate([ClaudeMessageRole.USER, ClaudeMessageRole.ASSISTANT]):
            msg = ClaudeMessage.objects.create(
                session=self.session,
                role=role,
                sequence=i,
            )
            self.assertEqual(msg.role, role)


class ClaudeContentBlockTestCase(TestCase):
    """Tests for ClaudeContentBlock model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.session = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.CLAUDE,
            transport_type=TransportType.API,
            status=SessionStatus.ACTIVE,
        )
        self.message = ClaudeMessage.objects.create(
            session=self.session,
            role=ClaudeMessageRole.ASSISTANT,
            sequence=0,
        )

    def test_text_content_block(self):
        """Test creating a text content block."""
        block = ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TEXT,
            sequence=0,
            text="Hello, how can I help you?",
        )
        self.assertEqual(block.block_type, "text")
        self.assertEqual(block.text, "Hello, how can I help you?")
        self.assertIsNone(block.thinking)
        self.assertIsNone(block.tool_use_id)
        self.assertFalse(block.is_error)

    def test_thinking_content_block(self):
        """Test creating a thinking content block."""
        block = ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.THINKING,
            sequence=0,
            thinking="Let me reason through this step by step...",
        )
        self.assertEqual(block.block_type, "thinking")
        self.assertEqual(block.thinking, "Let me reason through this step by step...")
        self.assertIsNone(block.text)

    def test_tool_use_content_block(self):
        """Test creating a tool_use content block."""
        block = ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TOOL_USE,
            sequence=1,
            tool_use_id="toolu_01abc",
            tool_name="search_web",
            tool_input={"query": "django models"},
        )
        self.assertEqual(block.block_type, "tool_use")
        self.assertEqual(block.tool_use_id, "toolu_01abc")
        self.assertEqual(block.tool_name, "search_web")
        self.assertEqual(block.tool_input, {"query": "django models"})

    def test_tool_result_content_block(self):
        """Test creating a tool_result content block."""
        block = ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TOOL_RESULT,
            sequence=2,
            tool_result_for="toolu_01abc",
            tool_result_content=[{"type": "text", "text": "Search results here"}],
            is_error=False,
        )
        self.assertEqual(block.block_type, "tool_result")
        self.assertEqual(block.tool_result_for, "toolu_01abc")
        self.assertEqual(
            block.tool_result_content,
            [{"type": "text", "text": "Search results here"}],
        )
        self.assertFalse(block.is_error)

    def test_tool_result_with_error(self):
        """Test a tool_result block that signals an error."""
        block = ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TOOL_RESULT,
            sequence=3,
            tool_result_for="toolu_01abc",
            tool_result_content=[{"type": "text", "text": "Tool failed"}],
            is_error=True,
        )
        self.assertTrue(block.is_error)

    def test_content_block_ordering_within_message(self):
        """Test that content blocks are ordered by sequence within a message."""
        ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TEXT,
            sequence=2,
            text="Third",
        )
        ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.THINKING,
            sequence=0,
            thinking="First",
        )
        ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TOOL_USE,
            sequence=1,
            tool_use_id="toolu_x",
            tool_name="my_tool",
            tool_input={},
        )
        blocks = list(ClaudeContentBlock.objects.filter(message=self.message))
        self.assertEqual(blocks[0].sequence, 0)
        self.assertEqual(blocks[1].sequence, 1)
        self.assertEqual(blocks[2].sequence, 2)

    def test_content_block_cascade_delete_from_message(self):
        """Test that content blocks are deleted when message is deleted."""
        ClaudeContentBlock.objects.create(
            message=self.message,
            block_type=ContentBlockType.TEXT,
            sequence=0,
            text="Hello",
        )
        message_id = self.message.id
        self.message.delete()
        self.assertEqual(
            ClaudeContentBlock.objects.filter(message_id=message_id).count(), 0
        )


class OpenAIMessageTestCase(TestCase):
    """Tests for OpenAIMessage model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.session = ConversationSession.objects.create(
            user=self.user,
            engine_type=EngineType.OPENAI,
            transport_type=TransportType.API,
            status=SessionStatus.ACTIVE,
        )

    def test_user_message_creation(self):
        """Test creating a user OpenAI message."""
        msg = OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.USER,
            content="What is the weather today?",
            sequence=0,
        )
        self.assertIsNotNone(msg.id)
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "What is the weather today?")
        self.assertIsNone(msg.tool_calls)
        self.assertIsNone(msg.tool_call_id)
        self.assertIsNone(msg.function_name)
        self.assertIsNone(msg.input_tokens)
        self.assertIsNone(msg.output_tokens)

    def test_assistant_message_with_tool_calls(self):
        """Test creating an assistant message that contains tool_calls."""
        tool_calls = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"location": "NYC"}',
                },
            }
        ]
        msg = OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.ASSISTANT,
            content=None,
            tool_calls=tool_calls,
            sequence=1,
            input_tokens=50,
            output_tokens=120,
        )
        self.assertEqual(msg.role, "assistant")
        self.assertIsNone(msg.content)
        self.assertEqual(msg.tool_calls, tool_calls)
        self.assertEqual(msg.input_tokens, 50)
        self.assertEqual(msg.output_tokens, 120)

    def test_openai_message_model_name_field(self):
        """Test creating an OpenAIMessage with the model_name cost tracking field."""
        msg = OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.ASSISTANT,
            content="Hello!",
            sequence=1,
            input_tokens=10,
            output_tokens=5,
            model_name="gpt-4o",
        )
        self.assertEqual(msg.model_name, "gpt-4o")

    def test_openai_message_model_name_defaults_null(self):
        """Test that model_name defaults to None on OpenAIMessage."""
        msg = OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.USER,
            content="Hi there",
            sequence=0,
        )
        self.assertIsNone(msg.model_name)

    def test_tool_response_message(self):
        """Test creating a tool response message."""
        msg = OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.TOOL,
            content='{"temperature": 72, "unit": "F"}',
            tool_call_id="call_abc123",
            function_name="get_weather",
            sequence=2,
        )
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.tool_call_id, "call_abc123")
        self.assertEqual(msg.function_name, "get_weather")

    def test_system_message(self):
        """Test creating a system message."""
        msg = OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.SYSTEM,
            content="You are a helpful assistant.",
            sequence=0,
        )
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.content, "You are a helpful assistant.")

    def test_openai_message_ordering_by_sequence(self):
        """Test that OpenAI messages are ordered by sequence."""
        OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.ASSISTANT,
            content="Response",
            sequence=2,
        )
        OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.SYSTEM,
            content="System prompt",
            sequence=0,
        )
        OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.USER,
            content="User question",
            sequence=1,
        )
        messages = list(OpenAIMessage.objects.filter(session=self.session))
        self.assertEqual(messages[0].sequence, 0)
        self.assertEqual(messages[1].sequence, 1)
        self.assertEqual(messages[2].sequence, 2)

    def test_openai_message_cascade_delete(self):
        """Test that OpenAI messages are deleted when session is deleted."""
        OpenAIMessage.objects.create(
            session=self.session,
            role=OpenAIMessageRole.USER,
            content="Hello",
            sequence=0,
        )
        session_id = self.session.id
        self.session.delete()
        self.assertEqual(OpenAIMessage.objects.filter(session_id=session_id).count(), 0)

    def test_all_openai_roles(self):
        """Test all OpenAI message role choices."""
        for i, role in enumerate(
            [
                OpenAIMessageRole.USER,
                OpenAIMessageRole.ASSISTANT,
                OpenAIMessageRole.SYSTEM,
                OpenAIMessageRole.TOOL,
            ]
        ):
            msg = OpenAIMessage.objects.create(
                session=self.session,
                role=role,
                content="test",
                sequence=i,
            )
            self.assertEqual(msg.role, role)
