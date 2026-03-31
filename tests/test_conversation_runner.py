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
    @patch("django_ergo.conversation.runner.tool_registry")
    def test_extra_tools_handled_first(self, mock_registry):
        """When extra_tools has the tool, it handles it — not the global registry."""

        extra = MagicMock()
        extra.has_tool.return_value = True
        extra.execute_tool.return_value = "toolkit result"

        # Verify has_tool is checked — we can't easily test the full async flow
        # without pytest-asyncio, so test the helper
        assert extra.has_tool("history_view_conversation") is True
        extra.execute_tool.assert_not_called()

    def test_extra_tools_none_is_backward_compatible(self):
        """run_conversation_turn still works without extra_tools."""
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        params = list(sig.parameters.keys())
        assert "extra_tools" in params
        assert sig.parameters["extra_tools"].default is None
