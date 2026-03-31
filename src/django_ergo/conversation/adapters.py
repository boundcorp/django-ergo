"""Tool adapters for converting ergo tools to engine-native formats."""

from __future__ import annotations

import json
from abc import ABC
from abc import abstractmethod
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from django_ergo.tools import ToolConfig


class ToolAdapter(ABC):
    @abstractmethod
    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        """Convert an ergo tool definition to engine-native format."""

    @abstractmethod
    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        """Extract (tool_name, arguments) from engine-native tool call."""

    @abstractmethod
    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        """Format tool result for engine-native submission."""


class ClaudeToolAdapter(ToolAdapter):
    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        properties = {}
        required = []
        for param_name, param_info in tool_config.parameters.items():
            properties[param_name] = {"type": param_info.get("type", "string")}
            if "description" in param_info:
                properties[param_name]["description"] = param_info["description"]
            if param_info.get("required"):
                required.append(param_name)
        return {
            "name": tool_config.name,
            "description": tool_config.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        return raw["name"], raw["input"]

    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": str(result),
            "is_error": is_error,
        }


class OpenAIToolAdapter(ToolAdapter):
    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        properties = {}
        required = []
        for param_name, param_info in tool_config.parameters.items():
            properties[param_name] = {"type": param_info.get("type", "string")}
            if "description" in param_info:
                properties[param_name]["description"] = param_info["description"]
            if param_info.get("required"):
                required.append(param_name)
        return {
            "type": "function",
            "function": {
                "name": tool_config.name,
                "description": tool_config.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        return raw["function"]["name"], json.loads(raw["function"]["arguments"])

    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        return {"role": "tool", "tool_call_id": tool_use_id, "content": str(result)}
