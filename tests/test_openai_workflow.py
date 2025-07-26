"""
Tests for OpenAI integrations in the workflow engine.

These tests prepare for when the OpenAI integration in workflow_engine.py
is uncommented and activated. Currently provides structure and mocked tests.
"""

import pytest
from unittest.mock import patch, Mock, MagicMock
from django.test import TestCase
from django.contrib.auth import get_user_model

from django_ergo.models import UserChat, ChatMessage, Workflow, MessageType, MessageRole
from django_ergo.workflow_engine import WorkflowEngine, WorkflowContext
from .openai_test_utils import openai_test_manager, save_openai_fixture

User = get_user_model()


class TestWorkflowEngineOpenAI(TestCase):
    """Test OpenAI integration in WorkflowEngine."""
    
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
            tools_config={"enabled_tools": []}
        )
        
        self.chat = UserChat.objects.create(
            user=self.user,
            workflow=self.workflow,
            title="Test Chat"
        )
        
        self.engine = WorkflowEngine()
    
    def test_current_development_mode(self):
        """Test that workflow engine currently returns development responses."""
        message = "Hello, this is a test message"
        
        response_message = self.engine.process_message(self.chat, message)
        
        # Should get development response
        assert response_message.message_type == MessageType.ASSISTANT_MESSAGE
        assert "development response" in response_message.content.lower()
        assert response_message.metadata.get("development") is True
    
    @pytest.mark.openai_real
    def test_workflow_engine_with_real_openai_when_activated(self):
        """Test workflow engine with real OpenAI API (when uncommented)."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        # This test is prepared for when OpenAI integration is uncommented
        # Currently will use development mode
        
        message_content = "What is Django and how does it work?"
        
        # Mock the OpenAI client for preparation
        with patch('django_ergo.workflow_engine.openai') as mock_openai:
            mock_client = Mock()
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message = Mock()
            mock_response.choices[0].message.content = "Django is a Python web framework..."
            mock_response.choices[0].message.tool_calls = None
            mock_response.usage = Mock()
            mock_response.usage.model_dump.return_value = {"prompt_tokens": 20, "completion_tokens": 50}
            
            mock_client.chat.completions.create.return_value = mock_response
            mock_openai.OpenAI.return_value = mock_client
            
            # For now, just test development mode
            response_message = self.engine.process_message(self.chat, message_content)
            
            # Development mode assertions
            assert response_message.message_type == MessageType.ASSISTANT_MESSAGE
            assert "development response" in response_message.content.lower()
            
            # Prepare fixture for when OpenAI is activated
            input_data = {
                "message": message_content,
                "workflow_instructions": self.workflow.instructions,
                "model": "gpt-4o-mini"
            }
            
            save_openai_fixture(
                "workflow_engine_basic", 
                input_data, 
                mock_response, 
                "chat.completions"
            )
    
    @pytest.mark.openai_mocked
    def test_workflow_engine_with_mocked_openai(self):
        """Test workflow engine with mocked OpenAI API using fixtures."""
        fixture = openai_test_manager.load_fixture("workflow_engine_basic")
        if not fixture:
            pytest.skip("No fixture found - run with TEST_OPENAI=true first")
        
        # When OpenAI integration is uncommented, this test will be fully functional
        # For now, test that the mocking infrastructure works
        
        mock_response = openai_test_manager.create_mock_response(fixture)
        
        with patch('django_ergo.workflow_engine.openai') as mock_openai:
            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_openai.OpenAI.return_value = mock_client
            
            # Test current development mode
            response_message = self.engine.process_message(
                self.chat, 
                fixture.input_data["message"]
            )
            
            # Currently returns development response
            assert response_message.message_type == MessageType.ASSISTANT_MESSAGE
    
    def test_build_conversation_history(self):
        """Test conversation history building for OpenAI API."""
        # Add some messages to the chat
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
        
        self.chat.add_message(
            message_type=MessageType.USER_INPUT,
            content="How are you?",
            role=MessageRole.USER
        )
        
        # Create context
        context = WorkflowContext(
            user=self.user,
            chat=self.chat,
            workflow=self.workflow
        )
        
        # Test conversation history building
        messages = self.engine._build_conversation_history(context)
        
        # Should have system message + conversation
        assert len(messages) >= 4  # system + 3 messages
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == self.workflow.instructions
        
        # Check message order and content
        user_messages = [msg for msg in messages if msg["role"] == "user"]
        assistant_messages = [msg for msg in messages if msg["role"] == "assistant"]
        
        assert len(user_messages) == 2
        assert len(assistant_messages) == 1
    
    def test_get_workflow_tools(self):
        """Test workflow tools configuration for OpenAI API."""
        # Test with empty tools config
        tools = self.engine._get_workflow_tools(self.workflow)
        assert tools is None
        
        # Test with tools configured
        self.workflow.tools_config = {
            "enabled_tools": ["search_knowledge", "send_email"]
        }
        self.workflow.save()
        
        # This would work when tool registry is fully implemented
        tools = self.engine._get_workflow_tools(self.workflow)
        # Currently returns None due to empty tool registry


class TestWorkflowEngineToolCalls:
    """Test tool call handling in workflow engine."""
    
    def setUp(self):
        """Set up test data.""" 
        self.user = User.objects.create_user(
            username="tooluser",
            email="tool@example.com", 
            password="testpass123"
        )
        
        self.workflow = Workflow.objects.create(
            name="Tool Workflow",
            instructions="You can use tools to help users.",
            tools_config={"enabled_tools": ["search", "calculate"]}
        )
        
        self.chat = UserChat.objects.create(
            user=self.user,
            workflow=self.workflow,
            title="Tool Chat"
        )
        
        self.engine = WorkflowEngine()
    
    @pytest.mark.openai_real
    def test_tool_calls_real_api(self):
        """Test tool calls with real OpenAI API."""
        if not openai_test_manager.should_use_real_api():
            pytest.skip("TEST_OPENAI not set - skipping costly API test")
        
        # This test prepares for tool call functionality
        # Currently will use development mode
        
        message_content = "Search for information about Python web frameworks"
        
        with patch('django_ergo.tool_registry') as mock_registry:
            # Mock tool registry
            mock_tool = Mock()
            mock_tool.name = "search"
            mock_tool.requires_approval = False
            mock_registry.get_tool.return_value = mock_tool
            mock_registry.execute_tool.return_value = {"results": ["Django", "Flask", "FastAPI"]}
            
            response_message = self.engine.process_message(self.chat, message_content)
            
            # Currently returns development response
            assert response_message.message_type == MessageType.ASSISTANT_MESSAGE
            
            # Prepare fixture for tool calls
            input_data = {
                "message": message_content,
                "tools_enabled": True,
                "model": "gpt-4o-mini"
            }
            
            # Mock tool call response
            mock_response = Mock()
            mock_response.choices = [Mock()]
            mock_response.choices[0].message = Mock()
            mock_response.choices[0].message.content = "I'll search for Python web frameworks."
            mock_response.choices[0].message.tool_calls = [Mock()]
            mock_response.choices[0].message.tool_calls[0].id = "call_123"
            mock_response.choices[0].message.tool_calls[0].function = Mock()
            mock_response.choices[0].message.tool_calls[0].function.name = "search"
            mock_response.choices[0].message.tool_calls[0].function.arguments = '{"query": "Python web frameworks"}'
            mock_response.usage = Mock()
            mock_response.usage.model_dump.return_value = {"prompt_tokens": 30, "completion_tokens": 40}
            
            save_openai_fixture(
                "workflow_tool_calls",
                input_data,
                mock_response,
                "chat.completions"
            )
    
    @pytest.mark.openai_mocked  
    def test_tool_calls_mocked(self):
        """Test tool calls with mocked OpenAI API."""
        fixture = openai_test_manager.load_fixture("workflow_tool_calls")
        if not fixture:
            pytest.skip("No fixture found - run with TEST_OPENAI=true first")
        
        # Test mocked tool call handling
        # This will be fully functional when OpenAI integration is uncommented
        
        with patch('django_ergo.tool_registry') as mock_registry:
            mock_tool = Mock()
            mock_tool.name = "search"
            mock_tool.requires_approval = False
            mock_registry.get_tool.return_value = mock_tool
            mock_registry.execute_tool.return_value = {"results": ["Django", "Flask"]}
            
            response_message = self.engine.process_message(
                self.chat,
                fixture.input_data["message"]
            )
            
            # Currently returns development response
            assert response_message.message_type == MessageType.ASSISTANT_MESSAGE


class TestWorkflowEngineContextManagement:
    """Test workflow context and state management."""
    
    def test_workflow_context_creation(self):
        """Test WorkflowContext creation and state management."""
        user = User.objects.create_user(username="contextuser", password="pass123")
        workflow = Workflow.objects.create(name="Context Workflow")
        chat = UserChat.objects.create(user=user, workflow=workflow)
        
        context = WorkflowContext(
            user=user,
            chat=chat,
            workflow=workflow,
            state={"step": 1, "data": "test"}
        )
        
        assert context.user == user
        assert context.chat == chat
        assert context.workflow == workflow
        assert context.state["step"] == 1
        assert context.state["data"] == "test"
    
    def test_workflow_context_default_state(self):
        """Test WorkflowContext with default empty state."""
        user = User.objects.create_user(username="defaultuser", password="pass123")
        workflow = Workflow.objects.create(name="Default Workflow")
        chat = UserChat.objects.create(user=user, workflow=workflow)
        
        context = WorkflowContext(
            user=user,
            chat=chat, 
            workflow=workflow
        )
        
        assert context.state == {}


# Prepare for future OpenAI Agents integration tests
class TestFutureOpenAIAgents:
    """Tests prepared for OpenAI Agents integration from old-code-inspiration."""
    
    @pytest.mark.skip(reason="OpenAI Agents integration not yet implemented")
    def test_openai_agents_integration(self):
        """Test OpenAI Agents when integrated."""
        # This will test the old-code-inspiration/workflows/openai_agent.py
        # functionality when it's brought into the main codebase
        pass
    
    @pytest.mark.skip(reason="OpenAI Agents integration not yet implemented") 
    def test_openai_agents_tool_integration(self):
        """Test OpenAI Agents with Ergo tools."""
        # Test ErgoOpenAITool wrapper functionality
        pass