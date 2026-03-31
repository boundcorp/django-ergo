"""Tests for tool adapters."""

import pytest
from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.tools import ToolConfig


@pytest.fixture()
def sample_tool():
    return ToolConfig(
        name="search_kb",
        description="Search the knowledge base",
        parameters={
            "query": {
                "type": "string",
                "required": True,
                "description": "Search query",
            },
            "top_k": {"type": "integer", "required": False, "default": 5},
        },
    )


class TestClaudeToolAdapter:
    def setup_method(self):
        self.adapter = ClaudeToolAdapter()

    def test_to_engine_schema(self, sample_tool):
        schema = self.adapter.to_engine_schema(sample_tool)
        assert schema["name"] == "search_kb"
        assert schema["description"] == "Search the knowledge base"
        assert schema["input_schema"]["type"] == "object"
        assert "query" in schema["input_schema"]["properties"]
        assert "query" in schema["input_schema"]["required"]
        assert "top_k" not in schema["input_schema"]["required"]

    def test_parse_tool_call(self):
        raw = {"id": "toolu_01", "name": "search_kb", "input": {"query": "django"}}
        name, args = self.adapter.parse_tool_call(raw)
        assert name == "search_kb"
        assert args == {"query": "django"}

    def test_format_tool_result(self):
        result = self.adapter.format_tool_result(
            "toolu_01", "Found 3 results", is_error=False
        )
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "toolu_01"
        assert result["content"] == "Found 3 results"
        assert result["is_error"] is False

    def test_format_tool_result_error(self):
        result = self.adapter.format_tool_result("toolu_01", "Not found", is_error=True)
        assert result["is_error"] is True


class TestOpenAIToolAdapter:
    def setup_method(self):
        self.adapter = OpenAIToolAdapter()

    def test_to_engine_schema(self, sample_tool):
        schema = self.adapter.to_engine_schema(sample_tool)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search_kb"
        assert schema["function"]["parameters"]["type"] == "object"
        assert "query" in schema["function"]["parameters"]["properties"]
        assert "query" in schema["function"]["parameters"]["required"]

    def test_parse_tool_call(self):
        raw = {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "search_kb", "arguments": '{"query": "django"}'},
        }
        name, args = self.adapter.parse_tool_call(raw)
        assert name == "search_kb"
        assert args == {"query": "django"}

    def test_format_tool_result(self):
        result = self.adapter.format_tool_result(
            "call_abc", "Found 3 results", is_error=False
        )
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "call_abc"
        assert result["content"] == "Found 3 results"
