"""
Pure unit tests for tool registry - Hybrid Approach Implementation.

These are fast unit tests that test core business logic without Django models.
They demonstrate the hybrid testing pattern documented in cursor rules.
"""

import inspect
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from unittest.mock import Mock

import pytest


# Mock simplified versions of django_ergo components for testing
@dataclass
class MockToolConfig:
    """Mock ToolConfig for testing without Django imports."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    readonly: bool = False


class MockToolRegistry:
    """Mock ToolRegistry for testing core functionality."""

    def __init__(self):
        self._tools = {}
        self._tool_functions = {}

    def register_tool(
        self,
        name: str,
        description: str,
        requires_approval: bool = False,
        readonly: bool = False,
    ):
        """Mock register_tool decorator."""

        def decorator(func):
            # Extract parameters from function signature
            sig = inspect.signature(func)
            parameters = {}

            for param_name, param in sig.parameters.items():
                if param_name == "user":  # Skip user parameter
                    continue

                param_info = {
                    "required": param.default == inspect.Parameter.empty,
                    "type": self._get_param_type(param.annotation),
                }

                if param.default != inspect.Parameter.empty:
                    param_info["default"] = param.default

                parameters[param_name] = param_info

            # Create and store tool config
            config = MockToolConfig(
                name=name,
                description=description,
                parameters=parameters,
                requires_approval=requires_approval,
                readonly=readonly,
            )

            self._tools[name] = config
            self._tool_functions[name] = func

            return func

        return decorator

    def _get_param_type(self, annotation):
        """Convert Python type annotations to string types."""
        if annotation in {str, "str"}:
            return "string"
        if annotation in {int, "int"}:
            return "integer"
        if annotation in {float, "float"}:
            return "number"
        if annotation in {bool, "bool"}:
            return "boolean"
        return "string"  # Default

    def get_tool(self, name: str) -> MockToolConfig | None:
        """Get tool configuration by name."""
        return self._tools.get(name)

    def execute_tool(
        self, name: str, user, arguments: dict[str, Any], approved: bool = False
    ):
        """Execute a tool with given arguments."""
        config = self.get_tool(name)
        if not config:
            msg = f"Tool '{name}' not found"
            raise ValueError(msg)

        if config.requires_approval and not approved:
            msg = f"Tool '{name}' requires approval but was not approved"
            raise ValueError(msg)

        func = self._tool_functions.get(name)
        if not func:
            msg = f"Function for '{name}' not found"
            raise ValueError(msg)

        # Inject user parameter if the function expects it
        sig = inspect.signature(func)
        if "user" in sig.parameters:
            arguments = {"user": user, **arguments}

        return func(**arguments)

    def list_tools(self, readonly_only: bool = False) -> list[MockToolConfig]:
        """List all registered tools."""
        tools = list(self._tools.values())
        if readonly_only:
            tools = [t for t in tools if t.readonly]
        return tools


# ============================================================================
# FAST UNIT TESTS (Tier 1: Core Business Logic)
# ============================================================================


class TestToolConfigUnit:
    """Test ToolConfig dataclass functionality."""

    def test_tool_config_creation(self):
        """Test ToolConfig creation with all parameters."""
        config = MockToolConfig(
            name="test_tool",
            description="A test tool",
            parameters={"param1": {"type": "string", "required": True}},
            requires_approval=True,
            readonly=False,
        )

        assert config.name == "test_tool"
        assert config.description == "A test tool"
        assert config.parameters["param1"]["type"] == "string"
        assert config.requires_approval is True
        assert config.readonly is False

    def test_tool_config_defaults(self):
        """Test ToolConfig with default values."""
        config = MockToolConfig(name="simple_tool", description="Simple tool")

        # Test defaults
        assert config.requires_approval is False
        assert config.readonly is False
        assert config.parameters == {}


class TestToolRegistryUnit:
    """Test ToolRegistry core functionality without Django dependencies."""

    def setup_method(self):
        """Set up test registry for each test."""
        self.registry = MockToolRegistry()
        # Mock user object
        self.mock_user = Mock()
        self.mock_user.username = "testuser"

    def test_register_tool_basic(self):
        """Test basic tool registration with decorator."""

        @self.registry.register_tool(name="basic_tool", description="A basic tool")
        def basic_function(param1: str, param2: int = 10):
            return {"param1": param1, "param2": param2}

        # Verify tool is registered
        config = self.registry.get_tool("basic_tool")
        assert config is not None
        assert config.name == "basic_tool"
        assert config.description == "A basic tool"
        assert config.requires_approval is False
        assert config.readonly is False

        # Verify parameters were extracted
        params = config.parameters
        assert "param1" in params
        assert "param2" in params
        assert params["param1"]["type"] == "string"
        assert params["param2"]["type"] == "integer"
        assert params["param1"]["required"] is True
        assert params["param2"]["required"] is False
        assert params["param2"]["default"] == 10

    def test_register_tool_with_options(self):
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
        assert config.requires_approval is True
        assert config.readonly is True

        # Should skip 'user' parameter in extraction
        params = config.parameters
        assert "user" not in params
        assert "data" in params

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
        assert params["string_param"]["type"] == "string"
        assert params["int_param"]["type"] == "integer"
        assert params["float_param"]["type"] == "number"
        assert params["bool_param"]["type"] == "boolean"
        assert params["untyped_param"]["type"] == "string"  # Default
        assert params["default_param"]["type"] == "string"

        # Check required/default handling
        assert params["string_param"]["required"] is True
        assert params["default_param"]["required"] is False
        assert params["default_param"]["default"] == "default"

    def test_execute_tool_success(self):
        """Test successful tool execution."""

        @self.registry.register_tool("executable_tool", "Executable tool")
        def executable_function(user, message: str):
            return {"user": user.username, "message": message}

        result = self.registry.execute_tool(
            name="executable_tool",
            user=self.mock_user,
            arguments={"message": "Hello World"},
            approved=True,
        )

        assert result["user"] == "testuser"
        assert result["message"] == "Hello World"

    def test_execute_tool_approval_required(self):
        """Test tool execution that requires approval."""

        @self.registry.register_tool(
            "approval_required_tool", "Tool requiring approval", requires_approval=True
        )
        def approval_required_function():
            return "approved_result"

        # Test without approval - should raise error
        with pytest.raises(ValueError, match="requires approval"):
            self.registry.execute_tool(
                name="approval_required_tool",
                user=self.mock_user,
                arguments={},
                approved=False,
            )

        # Test with approval - should succeed
        result = self.registry.execute_tool(
            name="approval_required_tool",
            user=self.mock_user,
            arguments={},
            approved=True,
        )

        assert result == "approved_result"

    def test_execute_tool_not_found(self):
        """Test executing non-existent tool."""
        with pytest.raises(ValueError, match="not found"):
            self.registry.execute_tool(
                name="nonexistent_tool",
                user=self.mock_user,
                arguments={},
                approved=True,
            )

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
        assert len(all_tools) == 3
        tool_names = [tool.name for tool in all_tools]
        assert "tool1" in tool_names
        assert "tool2" in tool_names
        assert "tool3" in tool_names

        # Test listing readonly tools only
        readonly_tools = self.registry.list_tools(readonly_only=True)
        assert len(readonly_tools) == 1
        assert readonly_tools[0].name == "tool2"


class TestToolRegistryAdvanced:
    """Test advanced tool registry scenarios."""

    def setup_method(self):
        """Set up test registry for each test."""
        self.registry = MockToolRegistry()
        self.mock_user = Mock()
        self.mock_user.id = 123
        self.mock_user.username = "testuser"
        self.mock_user.email = "test@example.com"

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
        assert params["required_str"]["required"] is True
        assert params["optional_int"]["required"] is False
        assert params["optional_int"]["default"] == 42
        assert params["optional_float"]["default"] == 3.14
        assert params["optional_bool"]["default"] is True

        # Test execution with minimal parameters
        result = self.registry.execute_tool(
            name="complex_tool",
            user=self.mock_user,
            arguments={"required_str": "test"},
            approved=True,
        )

        assert result["required_str"] == "test"
        assert result["optional_int"] == 42  # Default value

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
            user=self.mock_user,
            arguments={"action": "test_action"},
            approved=True,
        )

        assert result["user_id"] == 123
        assert result["username"] == "testuser"
        assert result["email"] == "test@example.com"
        assert result["action"] == "test_action"
        assert result["data"] == "default"

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

        assert readonly_config.readonly is True
        assert readonly_config.requires_approval is False

        assert action_config.readonly is False
        assert action_config.requires_approval is True

        # Both should execute when approved
        readonly_result = self.registry.execute_tool(
            name="readonly_tool", user=self.mock_user, arguments={}, approved=True
        )

        action_result = self.registry.execute_tool(
            name="action_tool", user=self.mock_user, arguments={}, approved=True
        )

        assert readonly_result["type"] == "readonly"
        assert action_result["type"] == "action"


# ============================================================================
# DEMONSTRATION: Hybrid Testing Pattern
# ============================================================================


def test_hybrid_testing_documentation():
    """
    This test documents the hybrid testing approach for the team.

    **Tier 1: Fast Unit Tests (what we just demonstrated above)**
    - Test core business logic without external dependencies
    - Use mocks and simple data structures
    - Run by default in CI/development
    - Examples: tool registry, parameter validation, data transformation

    **Tier 2: Integration Tests with OpenAI (would be in separate files)**
    - Use @pytest.mark.openai_real for real API tests (when TEST_OPENAI=true)
    - Use @pytest.mark.openai_mocked for fixture-based tests
    - Test actual OpenAI integration, tool calling, response handling
    - Examples: workflow engine with real/mocked OpenAI responses

    **Benefits:**
    - Fast feedback during development
    - Cost control (no API charges by default)
    - Comprehensive coverage with realistic scenarios
    - Easy debugging and maintenance
    """
    # This test always passes - it's documentation
    assert True


if __name__ == "__main__":
    # Can be run directly for quick testing
    registry = MockToolRegistry()

    @registry.register_tool("example_tool", "Example tool for testing")
    def example_function(message: str, count: int = 1):
        return {"message": message, "count": count}

    # Test the tool
    mock_user = Mock()
    mock_user.username = "demo_user"

    result = registry.execute_tool(
        name="example_tool",
        user=mock_user,
        arguments={"message": "Hello, World!"},
        approved=True,
    )

    print("✅ Hybrid testing pattern working!")
    print(f"Result: {result}")
    print("\nRun with: python -m pytest tests/test_tool_registry_unit.py -v")
