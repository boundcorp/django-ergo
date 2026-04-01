"""Tests for Toolkit ABC protocol."""

import pytest
from django_ergo.conversation.toolkit import Toolkit


class TestToolkitABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Toolkit()

    def test_required_methods(self):
        required = {"has_tool", "execute_tool", "get_tools_schema", "render_overview"}
        abstract = set(Toolkit.__abstractmethods__)
        assert abstract == required

    def test_concrete_subclass_works(self):
        class FakeToolkit(Toolkit):
            def has_tool(self, tool_name):
                return tool_name == "test"

            def execute_tool(self, tool_name, arguments):
                return "result"

            def get_tools_schema(self, adapter):
                return []

            def render_overview(self):
                return "overview"

        tk = FakeToolkit()
        assert tk.has_tool("test") is True
        assert tk.has_tool("other") is False
        assert tk.execute_tool("test", {}) == "result"
        assert tk.get_tools_schema(None) == []
        assert tk.render_overview() == "overview"
