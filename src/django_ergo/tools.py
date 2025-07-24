"""
Tool system for Django Ergo workflows.

Provides a framework for registering and managing tools that can be used
by AI agents in workflows. Supports both read-only tools and tools that
can perform actions with side effects.
"""

import inspect
from typing import Dict, List, Any, Callable, Optional, Type
from dataclasses import dataclass
from functools import wraps
from django.contrib.auth import get_user_model

User = get_user_model()


@dataclass
class ToolConfig:
    """Configuration for a tool."""
    name: str
    description: str
    parameters: Dict[str, Any]
    requires_approval: bool = False
    readonly: bool = False


class ToolRegistry:
    """Registry for managing tools available to workflows."""
    
    def __init__(self):
        self._tools: Dict[str, ToolConfig] = {}
        self._tool_functions: Dict[str, Callable] = {}
    
    def register_tool(
        self,
        name: str,
        description: str,
        requires_approval: bool = False,
        readonly: bool = False
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
            # Extract parameter information from function signature
            sig = inspect.signature(func)
            parameters = {}
            
            for param_name, param in sig.parameters.items():
                if param_name == 'user':  # Skip user parameter
                    continue
                    
                param_info = {
                    "type": "string",  # Default type
                    "description": f"Parameter {param_name}",
                }
                
                # Try to get type information
                if param.annotation != inspect.Parameter.empty:
                    if param.annotation == str:
                        param_info["type"] = "string"
                    elif param.annotation == int:
                        param_info["type"] = "integer"
                    elif param.annotation == float:
                        param_info["type"] = "number"
                    elif param.annotation == bool:
                        param_info["type"] = "boolean"
                
                # Check if parameter has default value
                if param.default != inspect.Parameter.empty:
                    param_info["default"] = param.default
                else:
                    param_info["required"] = True
                
                parameters[param_name] = param_info
            
            # Register the tool
            config = ToolConfig(
                name=name,
                description=description,
                parameters=parameters,
                requires_approval=requires_approval,
                readonly=readonly
            )
            
            self._tools[name] = config
            self._tool_functions[name] = func
            
            @wraps(func)
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            
            return wrapper
        
        return decorator
    
    def get_tool(self, name: str) -> Optional[ToolConfig]:
        """Get tool configuration by name."""
        return self._tools.get(name)
    
    def get_tool_function(self, name: str) -> Optional[Callable]:
        """Get tool function by name."""
        return self._tool_functions.get(name)
    
    def list_tools(self, readonly_only: bool = False) -> List[ToolConfig]:
        """List all registered tools."""
        tools = list(self._tools.values())
        if readonly_only:
            tools = [tool for tool in tools if tool.readonly]
        return tools
    
    def execute_tool(
        self, 
        name: str, 
        user: User, 
        arguments: Dict[str, Any],
        approved: bool = False
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
            raise ValueError(f"Tool '{name}' not found")
        
        if tool_config.requires_approval and not approved:
            raise ValueError(f"Tool '{name}' requires approval before execution")
        
        tool_function = self.get_tool_function(name)
        if not tool_function:
            raise ValueError(f"Tool function for '{name}' not found")
        
        # Add user to arguments if the function expects it
        sig = inspect.signature(tool_function)
        if 'user' in sig.parameters:
            arguments['user'] = user
        
        return tool_function(**arguments)


# Global tool registry
tool_registry = ToolRegistry()


def tool(
    name: str,
    description: str,
    requires_approval: bool = False,
    readonly: bool = False
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
        readonly=readonly
    )


class ToolRegistryBase:
    """
    Base class for creating tool registries.
    Provides a framework for organizing tools into logical groups.
    """
    
    def __init__(self):
        self.resources: List[Callable] = []  # Read-only tools
        self.tools: List[Callable] = []      # Action tools
    
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