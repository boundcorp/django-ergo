"""
Tests for the tool registry system.

Tests tool registration, discovery, parameter extraction, validation,
and execution through the ToolRegistry class.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django_ergo.tools import ToolConfig
from django_ergo.tools import ToolRegistry
from django_ergo.tools import ToolRegistryBase
from django_ergo.tools import UserToolRegistry
from django_ergo.tools import tool
from django_ergo.tools import tool_registry

User = get_user_model()


class TestToolConfig(TestCase):
    """Test ToolConfig dataclass."""

    def test_tool_config_creation(self):
        """Test ToolConfig creation with all parameters."""
        config = ToolConfig(
            name="test_tool",
            description="A test tool",
            parameters={"param1": {"type": "string", "required": True}},
            requires_approval=True,
            readonly=False,
        )

        self.assertEqual(config.name, "test_tool")
        self.assertEqual(config.description, "A test tool")
        self.assertEqual(config.parameters["param1"]["type"], "string")
        self.assertTrue(config.requires_approval)
        self.assertFalse(config.readonly)

    def test_tool_config_defaults(self):
        """Test ToolConfig with default values."""
        config = ToolConfig(
            name="simple_tool", description="Simple tool", parameters={}
        )

        # Test defaults
        self.assertFalse(config.requires_approval)
        self.assertFalse(config.readonly)


class TestToolRegistry(TestCase):
    """Test ToolRegistry class functionality."""

    def setUp(self):
        """Set up test registry."""
        self.registry = ToolRegistry()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )

    def test_register_tool_decorator_basic(self):
        """Test basic tool registration with decorator."""

        @self.registry.register_tool(name="basic_tool", description="A basic tool")
        def basic_function(param1: str, param2: int = 10):
            return {"param1": param1, "param2": param2}

        # Verify tool is registered
        config = self.registry.get_tool("basic_tool")
        self.assertIsNotNone(config)
        self.assertEqual(config.name, "basic_tool")
        self.assertEqual(config.description, "A basic tool")
        self.assertFalse(config.requires_approval)
        self.assertFalse(config.readonly)

        # Verify parameters were extracted
        params = config.parameters
        self.assertIn("param1", params)
        self.assertIn("param2", params)
        self.assertEqual(params["param1"]["type"], "string")
        self.assertEqual(params["param2"]["type"], "integer")
        self.assertTrue(params["param1"]["required"])
        self.assertFalse(params["param2"]["required"])
        self.assertEqual(params["param2"]["default"], 10)

    def test_register_tool_decorator_with_options(self):
        """Test tool registration with requires_approval and readonly options."""

        @self.registry.register_tool(
            name="approval_tool",
            description="Tool requiring approval",
            requires_approval=True,
            readonly=True,
        )
        def approval_function(user, data: str):
            return {"user": user.username, "data": data}

        config = self.registry.get_tool("approval_tool")
        self.assertTrue(config.requires_approval)
        self.assertTrue(config.readonly)

        # Should skip 'user' parameter in extraction
        params = config.parameters
        self.assertNotIn("user", params)
        self.assertIn("data", params)

    def test_parameter_type_extraction(self):
        """Test parameter type extraction from function signatures."""

        @self.registry.register_tool(
            name="typed_tool", description="Tool with typed parameters"
        )
        def typed_function(  # noqa: PLR0913
            string_param: str,
            int_param: int,
            float_param: float,
            bool_param: bool,
            untyped_param,
            default_param: str = "default",
        ):
            return {}

        config = self.registry.get_tool("typed_tool")
        params = config.parameters

        # Check type extraction
        self.assertEqual(params["string_param"]["type"], "string")
        self.assertEqual(params["int_param"]["type"], "integer")
        self.assertEqual(params["float_param"]["type"], "number")
        self.assertEqual(params["bool_param"]["type"], "boolean")
        self.assertEqual(params["untyped_param"]["type"], "string")  # Default
        self.assertEqual(params["default_param"]["type"], "string")

        # Check required/default handling
        self.assertTrue(params["string_param"]["required"])
        self.assertFalse(params["default_param"]["required"])
        self.assertEqual(params["default_param"]["default"], "default")

    def test_get_tool_function(self):
        """Test retrieving tool functions."""

        def test_function():
            return "test_result"

        self.registry._tool_functions["test_tool"] = test_function

        retrieved_function = self.registry.get_tool_function("test_tool")
        self.assertEqual(retrieved_function, test_function)
        self.assertEqual(retrieved_function(), "test_result")

        # Test non-existent tool
        self.assertIsNone(self.registry.get_tool_function("nonexistent"))

    def test_list_tools(self):
        """Test listing all registered tools."""

        # Register some tools
        @self.registry.register_tool("tool1", "Description 1")
        def tool1():
            pass

        @self.registry.register_tool("tool2", "Description 2", readonly=True)
        def tool2():
            pass

        @self.registry.register_tool("tool3", "Description 3")
        def tool3():
            pass

        # Test listing all tools
        all_tools = self.registry.list_tools()
        self.assertEqual(len(all_tools), 3)
        tool_names = [tool.name for tool in all_tools]
        self.assertIn("tool1", tool_names)
        self.assertIn("tool2", tool_names)
        self.assertIn("tool3", tool_names)

        # Test listing readonly tools only
        readonly_tools = self.registry.list_tools(readonly_only=True)
        self.assertEqual(len(readonly_tools), 1)
        self.assertEqual(readonly_tools[0].name, "tool2")

    def test_execute_tool_success(self):
        """Test successful tool execution."""

        @self.registry.register_tool("executable_tool", "Executable tool")
        def executable_function(user, message: str):
            return {"user": user.username, "message": message}

        result = self.registry.execute_tool(
            name="executable_tool",
            user=self.user,
            arguments={"message": "Hello World"},
            approved=True,
        )

        self.assertEqual(result["user"], "testuser")
        self.assertEqual(result["message"], "Hello World")

    def test_execute_tool_errors(self):
        """Test tool execution error scenarios."""
        # Test non-existent tool
        with pytest.raises(ValueError, match="not found"):
            self.registry.execute_tool(
                name="nonexistent_tool", user=self.user, arguments={}, approved=True
            )

        # Test tool requiring approval without approval
        @self.registry.register_tool(
            "approval_required_tool", "Tool requiring approval", requires_approval=True
        )
        def approval_required_function():
            return "approved_result"

        with pytest.raises(ValueError, match="requires approval"):
            self.registry.execute_tool(
                name="approval_required_tool",
                user=self.user,
                arguments={},
                approved=False,
            )

        # Test with approval
        result = self.registry.execute_tool(
            name="approval_required_tool", user=self.user, arguments={}, approved=True
        )

        self.assertEqual(result, "approved_result")

    def test_execute_tool_user_parameter_injection(self):
        """Test that user parameter is injected correctly."""

        @self.registry.register_tool("user_tool", "Tool with user parameter")
        def user_function(user, data: str):
            return {"username": user.username, "data": data}

        # User should be injected automatically
        result = self.registry.execute_tool(
            name="user_tool",
            user=self.user,
            arguments={"data": "test_data"},
            approved=True,
        )

        self.assertEqual(result["username"], "testuser")
        self.assertEqual(result["data"], "test_data")

    def test_execute_tool_without_user_parameter(self):
        """Test executing tool that doesn't expect user parameter."""

        @self.registry.register_tool("no_user_tool", "Tool without user parameter")
        def no_user_function(data: str):
            return {"data": data}

        result = self.registry.execute_tool(
            name="no_user_tool",
            user=self.user,
            arguments={"data": "test_data"},
            approved=True,
        )

        self.assertEqual(result["data"], "test_data")


class TestGlobalToolDecorator(TestCase):
    """Test the global @tool decorator."""

    def setUp(self):
        """Set up test user."""
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )

    def test_global_tool_decorator(self):
        """Test the global @tool decorator."""

        @tool(
            name="global_tool",
            description="A global tool",
            requires_approval=True,
            readonly=True,
        )
        def global_function(param: str):
            return {"param": param}

        # Verify tool is registered in global registry
        config = tool_registry.get_tool("global_tool")
        self.assertIsNotNone(config)
        self.assertEqual(config.name, "global_tool")
        self.assertEqual(config.description, "A global tool")
        self.assertTrue(config.requires_approval)
        self.assertTrue(config.readonly)

        # Test execution through global registry
        result = tool_registry.execute_tool(
            name="global_tool",
            user=self.user,
            arguments={"param": "test_value"},
            approved=True,
        )

        self.assertEqual(result["param"], "test_value")


class TestToolRegistryBase(TestCase):
    """Test ToolRegistryBase class."""

    def test_tool_registry_base_creation(self):
        """Test ToolRegistryBase initialization."""
        registry = ToolRegistryBase()

        self.assertEqual(registry.resources, [])
        self.assertEqual(registry.tools, [])

    def test_add_resource_and_tool(self):
        """Test adding resources and tools."""
        registry = ToolRegistryBase()

        def resource_function():
            return "resource_result"

        def tool_function():
            return "tool_result"

        # Test add_resource
        result = registry.add_resource(resource_function)
        self.assertEqual(result, resource_function)
        self.assertIn(resource_function, registry.resources)
        self.assertEqual(len(registry.resources), 1)
        self.assertEqual(len(registry.tools), 0)

        # Test add_tool
        result = registry.add_tool(tool_function)
        self.assertEqual(result, tool_function)
        self.assertIn(tool_function, registry.tools)
        self.assertEqual(len(registry.tools), 1)
        self.assertEqual(len(registry.resources), 1)

    def test_to_model_toolset(self):
        """Test to_model_toolset method."""
        registry = ToolRegistryBase()
        user = User.objects.create_user(username="testuser", password="testpass123")

        # Base implementation just returns self
        result = registry.to_model_toolset(user)
        self.assertEqual(result, registry)


class TestUserToolRegistry(TestCase):
    """Test UserToolRegistry class."""

    def setUp(self):
        """Set up test users."""
        self.user1 = User.objects.create_user(
            username="user1", email="user1@example.com", password="testpass123"
        )

        self.user2 = User.objects.create_user(
            username="user2", email="user2@example.com", password="testpass123"
        )

    def test_user_tool_registry_creation(self):
        """Test UserToolRegistry initialization."""
        registry = UserToolRegistry(self.user1)

        self.assertEqual(registry.user, self.user1)
        self.assertEqual(registry.resources, [])
        self.assertEqual(registry.tools, [])

    def test_to_model_toolset_different_users(self):
        """Test to_model_toolset with same and different users."""
        registry = UserToolRegistry(self.user1)

        # Add some tools and resources
        registry.add_resource(lambda: "resource")
        registry.add_tool(lambda: "tool")

        # Should return same registry for same user
        result = registry.to_model_toolset(self.user1)
        self.assertEqual(result, registry)
        self.assertEqual(len(result.resources), 1)
        self.assertEqual(len(result.tools), 1)

        # Should return empty registry for different user
        result = registry.to_model_toolset(self.user2)
        self.assertNotEqual(result, registry)
        self.assertEqual(result.user, self.user2)
        self.assertEqual(len(result.resources), 0)
        self.assertEqual(len(result.tools), 0)


class TestToolRegistryErrorHandling(TestCase):
    """Test error handling in tool registry."""

    def setUp(self):
        """Set up test registry and user."""
        self.registry = ToolRegistry()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )

    def test_execute_tool_function_not_found(self):
        """Test executing tool when function is missing."""
        # Register tool config but not function
        config = ToolConfig(
            name="broken_tool", description="Broken tool", parameters={}
        )
        self.registry._tools["broken_tool"] = config

        with pytest.raises(ValueError, match="function for 'broken_tool' not found"):
            self.registry.execute_tool(
                name="broken_tool", user=self.user, arguments={}, approved=True
            )

    def test_execute_tool_function_exception(self):
        """Test handling exceptions during tool execution."""

        error_message = "Tool execution failed"

        @self.registry.register_tool("error_tool", "Tool that raises exception")
        def error_function():
            raise ValueError(error_message)

        # Exception should propagate
        with pytest.raises(ValueError, match="Tool execution failed"):
            self.registry.execute_tool(
                name="error_tool", user=self.user, arguments={}, approved=True
            )


class TestComplexToolScenarios(TestCase):
    """Test complex tool registration and execution scenarios."""

    def setUp(self):
        """Set up test data."""
        self.registry = ToolRegistry()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )

    def test_tool_with_complex_parameters(self):
        """Test tool with complex parameter types and defaults."""

        @self.registry.register_tool(
            name="complex_tool", description="Tool with complex parameters"
        )
        def complex_function(
            required_str: str,
            optional_int: int = 42,
            optional_float: float = 3.14,
            optional_bool: bool = True,
            untyped_with_default=None,
        ):
            return {
                "required_str": required_str,
                "optional_int": optional_int,
                "optional_float": optional_float,
                "optional_bool": optional_bool,
                "untyped_with_default": untyped_with_default,
            }

        config = self.registry.get_tool("complex_tool")
        params = config.parameters

        # Verify parameter extraction
        self.assertTrue(params["required_str"]["required"])
        self.assertFalse(params["optional_int"]["required"])
        self.assertEqual(params["optional_int"]["default"], 42)
        self.assertEqual(params["optional_float"]["default"], 3.14)
        self.assertEqual(params["optional_bool"]["default"], True)

        # Test execution with minimal parameters
        result = self.registry.execute_tool(
            name="complex_tool",
            user=self.user,
            arguments={"required_str": "test"},
            approved=True,
        )

        self.assertEqual(result["required_str"], "test")
        self.assertEqual(result["optional_int"], 42)  # Default value

    def test_tool_execution_with_user_context(self):
        """Test tool execution that uses user context."""

        @self.registry.register_tool(
            name="user_context_tool", description="Tool that uses user context"
        )
        def user_context_function(user, action: str, data: str = "default"):
            return {
                "user_id": user.id,
                "username": user.username,
                "email": user.email,
                "action": action,
                "data": data,
            }

        result = self.registry.execute_tool(
            name="user_context_tool",
            user=self.user,
            arguments={"action": "test_action"},
            approved=True,
        )

        self.assertEqual(result["user_id"], self.user.id)
        self.assertEqual(result["username"], "testuser")
        self.assertEqual(result["email"], "test@example.com")
        self.assertEqual(result["action"], "test_action")
        self.assertEqual(result["data"], "default")

    def test_readonly_vs_action_tools(self):
        """Test distinction between readonly and action tools."""

        @self.registry.register_tool(
            name="readonly_tool", description="Read-only tool", readonly=True
        )
        def readonly_function():
            return {"type": "readonly"}

        @self.registry.register_tool(
            name="action_tool", description="Action tool", requires_approval=True
        )
        def action_function():
            return {"type": "action"}

        readonly_config = self.registry.get_tool("readonly_tool")
        action_config = self.registry.get_tool("action_tool")

        self.assertTrue(readonly_config.readonly)
        self.assertFalse(readonly_config.requires_approval)

        self.assertFalse(action_config.readonly)
        self.assertTrue(action_config.requires_approval)

        # Both should execute when approved
        readonly_result = self.registry.execute_tool(
            name="readonly_tool", user=self.user, arguments={}, approved=True
        )

        action_result = self.registry.execute_tool(
            name="action_tool", user=self.user, arguments={}, approved=True
        )

        self.assertEqual(readonly_result["type"], "readonly")
        self.assertEqual(action_result["type"], "action")
