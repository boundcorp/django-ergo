"""
Tests for WorkflowEngine functionality - Hybrid Approach.

Uses dual-tier testing:
- Fast unit tests for core logic (mocked, default)
- Real API tests for OpenAI integration (when TEST_OPENAI=true)
"""

import json
import pytest
from unittest.mock import patch, Mock, MagicMock
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta

from django_ergo.models import (
    UserChat, ChatMessage, Workflow, MessageType, MessageRole, Knowledgebase
)
from django_ergo.workflow_engine import (
    WorkflowEngine, WorkflowContext, ApprovalRequest,
    tool_approval_requested, workflow_paused, workflow_resumed
)
from django_ergo.tools import tool_registry
from tests.openai_test_utils import openai_mocked, openai_real, save_openai_fixture

User = get_user_model()


class TestWorkflowEngineCore(TestCase):
    """Fast unit tests for core WorkflowEngine functionality (no OpenAI API)."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.workflow = Workflow.objects.create(
            name="Test Workflow",
            instructions="You are a helpful assistant for testing purposes.",
            tools_config={
                "enabled_tools": ["test_tool"],
                "approved_tools": ["whitelisted_tool"]
            }
        )
        
        self.chat = UserChat.objects.create(
            user=self.user,
            workflow=self.workflow,
            title="Test Chat"
        )
    
    def test_workflow_context_creation(self):
        """Test WorkflowContext dataclass creation and default state."""
        context = WorkflowContext(
            user=self.user,
            chat=self.chat,
            workflow=self.workflow
        )
        
        self.assertEqual(context.user, self.user)
        self.assertEqual(context.chat, self.chat)
        self.assertEqual(context.workflow, self.workflow)
        self.assertEqual(context.state, {})
        self.assertIsNone(context.current_message)
    
    def test_get_tool_whitelist(self):
        """Test getting tool whitelist from workflow configuration."""
        # Mock engine without OpenAI requirement
        with patch('django_ergo.workflow_engine.openai.OpenAI'):
            engine = WorkflowEngine.__new__(WorkflowEngine)
            
        whitelist = engine.get_tool_whitelist(self.workflow)
        self.assertEqual(whitelist, ["whitelisted_tool"])
        
        # Test workflow with no tools config
        empty_workflow = Workflow.objects.create(
            name="Empty Workflow",
            instructions="No tools"
        )
        empty_whitelist = engine.get_tool_whitelist(empty_workflow)
        self.assertEqual(empty_whitelist, [])
    
    def test_is_tool_whitelisted(self):
        """Test checking if a tool is whitelisted."""
        with patch('django_ergo.workflow_engine.openai.OpenAI'):
            engine = WorkflowEngine.__new__(WorkflowEngine)
            
        self.assertTrue(engine.is_tool_whitelisted(self.workflow, "whitelisted_tool"))
        self.assertFalse(engine.is_tool_whitelisted(self.workflow, "not_whitelisted"))
    
    @patch('django_ergo.workflow_engine.ChatMessage')
    def test_serialize_workflow_context(self, mock_message):
        """Test workflow context serialization for pause/resume."""
        with patch('django_ergo.workflow_engine.openai.OpenAI'):
            engine = WorkflowEngine.__new__(WorkflowEngine)
            
        mock_msg = Mock()
        mock_msg.created_at = timezone.now()
        
        context = WorkflowContext(
            user=self.user,
            chat=self.chat,
            workflow=self.workflow,
            current_message=mock_msg,
            state={"step": 1, "data": "test"}
        )
        
        openai_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ]
        
        serialized = engine.serialize_workflow_context(context, openai_messages)
        
        self.assertEqual(serialized["workflow_id"], str(self.workflow.id))
        self.assertEqual(serialized["chat_id"], str(self.chat.id))
        self.assertEqual(serialized["user_id"], str(self.user.id))
        self.assertEqual(serialized["openai_messages"], openai_messages)
        self.assertEqual(serialized["workflow_state"], {"step": 1, "data": "test"})
        self.assertIn("model_config", serialized)
    
    def test_format_approval_request(self):
        """Test formatting approval request for user display."""
        with patch('django_ergo.workflow_engine.openai.OpenAI'):
            engine = WorkflowEngine.__new__(WorkflowEngine)
            
        pending_approvals = [
            {
                "tool_name": "test_tool",
                "description": "A test tool",
                "arguments": {"query": "test"}
            }
        ]
        
        formatted = engine._format_approval_request(pending_approvals)
        
        self.assertIn("Tool Approval Required", formatted)
        self.assertIn("test_tool", formatted)
        self.assertIn("A test tool", formatted)


class TestWorkflowEngineSignals(TestCase):
    """Test workflow engine signal firing (unit tests)."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.workflow = Workflow.objects.create(
            name="Test Workflow",
            instructions="Test workflow with signals",
            tools_config={"enabled_tools": ["signal_tool"]}
        )
        
        self.chat = UserChat.objects.create(
            user=self.user,
            workflow=self.workflow,
            title="Test Chat"
        )
    
    def test_tool_approval_requested_signal(self):
        """Test that tool_approval_requested signal is fired."""
        with patch('django_ergo.workflow_engine.openai.OpenAI'):
            engine = WorkflowEngine.__new__(WorkflowEngine)
            
        signal_fired = False
        
        def signal_handler(sender, **kwargs):
            nonlocal signal_fired
            signal_fired = True
        
        tool_approval_requested.connect(signal_handler)
        
        try:
            context = WorkflowContext(
                user=self.user,
                chat=self.chat,
                workflow=self.workflow
            )
            
            pending_approvals = [{"tool_name": "test_tool"}]
            
            with patch.object(engine, '_build_conversation_history', return_value=[]):
                with patch.object(engine, 'serialize_workflow_context', return_value={}):
                    engine._create_approval_request(context, Mock(), Mock(), pending_approvals)
            
            self.assertTrue(signal_fired)
            
        finally:
            tool_approval_requested.disconnect(signal_handler)


# ============================================================================
# DUAL-TIER OPENAI INTEGRATION TESTS
# ============================================================================

class TestWorkflowEngineOpenAIIntegration(TestCase):
    """Dual-tier tests for OpenAI integration in WorkflowEngine."""
    
    def setUp(self):
        """Set up test data."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        self.workflow = Workflow.objects.create(
            name="Test Workflow",
            instructions="You are a helpful assistant.",
            tools_config={"enabled_tools": []}
        )
        
        self.chat = UserChat.objects.create(
            user=self.user,
            workflow=self.workflow,
            title="Test Chat"
        )
    
    @pytest.mark.openai_real
    @openai_real("simple_message", "chat.completions")
    def test_process_message_real_api(self):
        """Test message processing with real OpenAI API (generates fixture)."""
        # This would be a real OpenAI API call if uncommented in workflow_engine.py
        # For now, it tests the current development mode
        engine = WorkflowEngine()
        message = "Hello, how are you?"
        
        response = engine.process_message(self.chat, message)
        
        # Currently returns development response
        self.assertEqual(response.message_type, MessageType.ASSISTANT_MESSAGE)
        self.assertIn("development mode", response.content.lower())
        
        # When real OpenAI is enabled, save fixture:
        input_data = {"message": message, "workflow": self.workflow.name}
        save_openai_fixture("simple_message", input_data, response, "chat.completions")
    
    @pytest.mark.openai_mocked  
    @openai_mocked("simple_message")
    def test_process_message_mocked(self, fixture):
        """Test message processing with saved fixture (fast, no cost)."""
        # Fixture is automatically loaded and OpenAI is mocked
        engine = WorkflowEngine()
        
        response = engine.process_message(self.chat, fixture.input_data["message"])
        
        self.assertEqual(response.message_type, MessageType.ASSISTANT_MESSAGE)
        self.assertEqual(response.role, MessageRole.ASSISTANT)
        # Can now assert against fixture data
        # self.assertEqual(response.content, fixture.response_data["content"])
    
    @pytest.mark.openai_real
    @openai_real("tool_approval", "chat.completions")
    def test_tool_approval_flow_real_api(self):
        """Test tool approval flow with real OpenAI API (generates fixture)."""
        # Set up workflow with approval-required tool
        workflow = Workflow.objects.create(
            name="Approval Workflow",
            instructions="You can create articles.",
            tools_config={"enabled_tools": ["create_article"]}
        )
        
        chat = UserChat.objects.create(
            user=self.user,
            workflow=workflow,
            title="Approval Test Chat"
        )
        
        engine = WorkflowEngine()
        message = "Create an article about Python programming"
        
        response = engine.process_message(chat, message)
        
        # Currently returns development response, but when OpenAI is enabled,
        # this should trigger tool approval
        self.assertEqual(response.message_type, MessageType.ASSISTANT_MESSAGE)
        
        # Save fixture for mocked tests
        input_data = {"message": message, "workflow": workflow.name}
        save_openai_fixture("tool_approval", input_data, response, "chat.completions")
        
        # When real OpenAI is enabled:
        # self.assertEqual(response.message_type, MessageType.TOOL_APPROVAL_REQUEST)
    
    @pytest.mark.openai_mocked
    @openai_mocked("tool_approval")
    def test_tool_approval_flow_mocked(self, fixture):
        """Test tool approval flow with saved fixture (fast, no cost)."""
        # Set up workflow to match fixture expectations
        workflow = Workflow.objects.create(
            name="Approval Workflow",
            instructions="You can create articles.",
            tools_config={"enabled_tools": ["create_article"]}
        )
        
        chat = UserChat.objects.create(
            user=self.user,
            workflow=workflow,
            title="Approval Test Chat"
        )
        
        engine = WorkflowEngine()
        
        # Use fixture data for consistent testing
        response = engine.process_message(chat, fixture.input_data["message"])
        
        # When real OpenAI integration is enabled, this would test actual approval flow
        # For now, verify the current development behavior
        self.assertEqual(response.message_type, MessageType.ASSISTANT_MESSAGE)
        
        # Future: self.assertEqual(response.message_type, MessageType.TOOL_APPROVAL_REQUEST)
    
    @pytest.mark.openai_real
    def test_conversation_history_real_api(self):
        """Test conversation history building with real OpenAI API."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        # Add some messages to test conversation building
        self.chat.add_message(
            message_type=MessageType.USER_INPUT,
            content="Hello",
            role=MessageRole.USER
        )
        
        self.chat.add_message(
            message_type=MessageType.ASSISTANT_MESSAGE,
            content="Hi there!",
            role=MessageRole.ASSISTANT
        )
        
        engine = WorkflowEngine()
        context = WorkflowContext(
            user=self.user,
            chat=self.chat,
            workflow=self.workflow
        )
        
        # Test conversation history building
        messages = engine._build_openai_conversation_history(context)
        
        # Should have system message + conversation messages
        self.assertGreaterEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], self.workflow.instructions)
        
        # Save fixture for mocked tests
        # input_data = {"chat_id": str(self.chat.id), "message_count": len(messages)}
        # save_openai_fixture("conversation_history", input_data, {"messages": messages}, "chat.completions")
    
    @pytest.mark.openai_mocked
    def test_conversation_history_mocked(self):
        """Test conversation history building with mocked data."""
        # Add some messages
        self.chat.add_message(
            message_type=MessageType.USER_INPUT,
            content="Hello",
            role=MessageRole.USER
        )
        
        self.chat.add_message(
            message_type=MessageType.ASSISTANT_MESSAGE,
            content="Hi there!",
            role=MessageRole.ASSISTANT
        )
        
        engine = WorkflowEngine()
        context = WorkflowContext(
            user=self.user,
            chat=self.chat,
            workflow=self.workflow
        )
        
        messages = engine._build_openai_conversation_history(context)
        
        # Should have system message + conversation messages  
        self.assertGreaterEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], self.workflow.instructions)
        
        # When fixtures are available, test against saved data
        # fixture = openai_test_manager.load_fixture("conversation_history")
        # if fixture:
        #     expected_messages = fixture.response_data["messages"]
        #     self.assertEqual(len(messages), len(expected_messages))