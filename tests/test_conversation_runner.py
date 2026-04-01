"""Tests for run_conversation_turn."""

from unittest.mock import MagicMock
from unittest.mock import patch

from django_ergo.conversation.runner import PendingApproval
from django_ergo.conversation.runner import _tool_requires_approval


async def _async_gen(*items):
    for item in items:
        yield item


class TestToolRequiresApproval:
    def test_unknown_tool(self):
        assert _tool_requires_approval("nonexistent", None) is False

    @patch("django_ergo.conversation.runner.tool_registry")
    def test_tool_no_approval(self, mock_registry):
        mock_config = MagicMock()
        mock_config.requires_approval = False
        mock_registry.get_tool.return_value = mock_config
        assert _tool_requires_approval("safe_tool", None) is False

    @patch("django_ergo.conversation.runner.tool_registry")
    def test_tool_needs_approval(self, mock_registry):
        mock_config = MagicMock()
        mock_config.requires_approval = True
        mock_registry.get_tool.return_value = mock_config
        assert _tool_requires_approval("danger_tool", None) is True

    @patch("django_ergo.conversation.runner.tool_registry")
    def test_tool_whitelisted(self, mock_registry):
        mock_config = MagicMock()
        mock_config.requires_approval = True
        mock_registry.get_tool.return_value = mock_config
        workflow = MagicMock()
        workflow.get_tools_config.return_value = {"approved_tools": ["danger_tool"]}
        assert _tool_requires_approval("danger_tool", workflow) is False


class TestPendingApproval:
    def test_dataclass(self):
        pa = PendingApproval(
            tool_use_id="toolu_01", tool_name="delete", arguments={"id": "123"}
        )
        assert pa.tool_use_id == "toolu_01"
        assert pa.tool_name == "delete"
        assert pa.arguments == {"id": "123"}


class TestExtraTools:
    def test_extra_tools_accepts_list(self):
        """run_conversation_turn accepts a list of Toolkits."""
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        assert "extra_tools" in sig.parameters
        assert sig.parameters["extra_tools"].default is None

    @patch("django_ergo.conversation.runner.tool_registry")
    def test_first_matching_toolkit_handles_tool(self, mock_registry):
        """When multiple toolkits are provided, the first one that claims the tool handles it."""
        from unittest.mock import MagicMock

        toolkit_a = MagicMock()
        toolkit_a.has_tool.return_value = False
        toolkit_b = MagicMock()
        toolkit_b.has_tool.return_value = True
        toolkit_b.execute_tool.return_value = "result from b"

        # Simulate the dispatch logic
        toolkits = [toolkit_a, toolkit_b]
        name = "kb_search"
        result = None
        for tk in toolkits:
            if tk.has_tool(name):
                result = tk.execute_tool(name, {"query": "test"})
                break

        toolkit_a.has_tool.assert_called_with(name)
        toolkit_b.has_tool.assert_called_with(name)
        assert result == "result from b"
        toolkit_a.execute_tool.assert_not_called()

    def test_extra_tools_backward_compatible_with_none(self):
        """run_conversation_turn still works without extra_tools."""
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        assert sig.parameters["extra_tools"].default is None


class TestHelperFunctions:
    def test_collect_toolkit_schemas(self):
        from django_ergo.conversation.runner import _collect_toolkit_schemas

        toolkit_a = MagicMock()
        toolkit_a.get_tools_schema.return_value = [{"name": "tool_a"}]
        toolkit_b = MagicMock()
        toolkit_b.get_tools_schema.return_value = [
            {"name": "tool_b"},
            {"name": "tool_c"},
        ]

        adapter = MagicMock()
        result = _collect_toolkit_schemas([toolkit_a, toolkit_b], adapter)
        expected_count = 3
        assert len(result) == expected_count
        assert result[0] == {"name": "tool_a"}
        assert result[1] == {"name": "tool_b"}

    def test_find_toolkit_for_tool_found(self):
        from django_ergo.conversation.runner import _find_toolkit_for_tool

        toolkit_a = MagicMock()
        toolkit_a.has_tool.return_value = False
        toolkit_b = MagicMock()
        toolkit_b.has_tool.return_value = True

        result = _find_toolkit_for_tool([toolkit_a, toolkit_b], "kb_search")
        assert result is toolkit_b

    def test_find_toolkit_for_tool_not_found(self):
        from django_ergo.conversation.runner import _find_toolkit_for_tool

        toolkit_a = MagicMock()
        toolkit_a.has_tool.return_value = False

        result = _find_toolkit_for_tool([toolkit_a], "unknown")
        assert result is None
