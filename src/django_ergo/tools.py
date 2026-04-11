"""
Tool system for Django Ergo workflows.

Provides a framework for registering and managing tools that can be used
by AI agents in workflows. Supports both read-only tools and tools that
can perform actions with side effects.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from django.contrib.auth import get_user_model

User = get_user_model()


@dataclass
class ToolConfig:
    """Configuration for a tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    requires_approval: bool = False
    readonly: bool = False


class ToolRegistry:
    """Registry for managing tools available to workflows."""

    def __init__(self):
        self._tools: dict[str, ToolConfig] = {}
        self._tool_functions: dict[str, Callable] = {}

    @staticmethod
    def _extract_parameters(func: Callable) -> dict[str, Any]:
        """Extract parameter info from a function signature, skipping 'user'."""
        _type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
        sig = inspect.signature(func)
        parameters = {}
        for param_name, param in sig.parameters.items():
            if param_name == "user":
                continue
            annotation = param.annotation
            param_type = (
                _type_map.get(annotation, "string")
                if annotation != inspect.Parameter.empty
                else "string"
            )
            param_info: dict[str, Any] = {
                "type": param_type,
                "description": f"Parameter {param_name}",
            }
            if param.default != inspect.Parameter.empty:
                param_info["default"] = param.default
                param_info["required"] = False
            else:
                param_info["required"] = True
            parameters[param_name] = param_info
        return parameters

    def register_tool(
        self,
        name: str,
        description: str,
        requires_approval: bool = False,
        readonly: bool = False,
    ):
        """
        Decorator to register a tool function.

        Args:
            name: Tool name
            description: Tool description
            requires_approval: Whether tool requires user approval before execution
            readonly: Whether tool is read-only (no side effects)
        """

        def decorator(func: Callable):
            config = ToolConfig(
                name=name,
                description=description,
                parameters=self._extract_parameters(func),
                requires_approval=requires_approval,
                readonly=readonly,
            )
            self._tools[name] = config
            self._tool_functions[name] = func

            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            return wrapper

        return decorator

    def get_tool(self, name: str) -> ToolConfig | None:
        """Get tool configuration by name."""
        return self._tools.get(name)

    def get_tool_function(self, name: str) -> Callable | None:
        """Get tool function by name."""
        return self._tool_functions.get(name)

    def list_tools(self, readonly_only: bool = False) -> list[ToolConfig]:
        """List all registered tools."""
        tools = list(self._tools.values())
        if readonly_only:
            tools = [tool for tool in tools if tool.readonly]
        return tools

    def execute_tool(
        self, name: str, user: User, arguments: dict[str, Any], approved: bool = False
    ) -> Any:
        """
        Execute a tool with the given arguments.

        Args:
            name: Tool name
            user: User executing the tool
            arguments: Tool arguments
            approved: Whether the tool execution has been approved

        Returns:
            Tool execution result

        Raises:
            ValueError: If tool not found or approval required but not given
        """
        tool_config = self.get_tool(name)
        if not tool_config:
            msg = f"Tool '{name}' not found"
            raise ValueError(msg)

        if tool_config.requires_approval and not approved:
            msg = f"Tool '{name}' requires approval before execution"
            raise ValueError(msg)

        tool_function = self.get_tool_function(name)
        if not tool_function:
            msg = f"Tool function for '{name}' not found"
            raise ValueError(msg)

        # Add user to arguments if the function expects it
        sig = inspect.signature(tool_function)
        if "user" in sig.parameters:
            arguments["user"] = user

        return tool_function(**arguments)


# Global tool registry
tool_registry = ToolRegistry()


def tool(
    name: str, description: str, requires_approval: bool = False, readonly: bool = False
):
    """
    Decorator to register a tool function with the global registry.

    Args:
        name: Tool name
        description: Tool description
        requires_approval: Whether tool requires user approval before execution
        readonly: Whether tool is read-only (no side effects)
    """
    return tool_registry.register_tool(
        name=name,
        description=description,
        requires_approval=requires_approval,
        readonly=readonly,
    )


class ToolRegistryBase:
    """
    Base class for creating tool registries.
    Provides a framework for organizing tools into logical groups.
    """

    def __init__(self):
        self.resources: list[Callable] = []  # Read-only tools
        self.tools: list[Callable] = []  # Action tools

    def add_resource(self, func: Callable):
        """Add a read-only tool (resource)."""
        self.resources.append(func)
        return func

    def add_tool(self, func: Callable):
        """Add an action tool."""
        self.tools.append(func)
        return func

    def to_model_toolset(self, user: User):
        """Convert to a toolset for a specific user."""
        # This would be implemented by subclasses to return
        # user-specific tool configurations
        return self


# Example tool registry for user-specific tools
class UserToolRegistry(ToolRegistryBase):
    """Registry for user-specific tools."""

    def __init__(self, user: User):
        super().__init__()
        self.user = user

    def to_model_toolset(self, user: User):
        """Return toolset configured for the specific user."""
        if user != self.user:
            # Return empty toolset for different user
            return UserToolRegistry(user)
        return self


# Global user tools registry
user_tools = UserToolRegistry(None)  # Will be configured per user
