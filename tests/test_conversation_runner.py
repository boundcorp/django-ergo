"""Tests for run_conversation_turn and the shared tool dispatch."""

from unittest.mock import MagicMock

from django_ergo.conversation.runner import PendingApproval
from django_ergo.conversation.tool_dispatch import collect_toolkit_schemas
from django_ergo.conversation.tool_dispatch import dispatch_tool
from django_ergo.conversation.tool_dispatch import find_toolkit
from django_ergo.conversation.tool_dispatch import requires_approval


def toolkit(name, *, approval=False, result="ok", error=None):
    tk = MagicMock()
    tk.has_tool.side_effect = lambda n: n == name
    tk.requires_approval.side_effect = lambda n: approval
    if error is not None:
        tk.execute_tool.side_effect = error
    else:
        tk.execute_tool.return_value = result
    return tk


class TestRequiresApproval:
    def test_unknown_tool(self):
        assert requires_approval("nonexistent", None) is False

    def test_the_toolkit_decides(self):
        assert requires_approval("safe", [toolkit("safe")]) is False
        assert requires_approval("danger", [toolkit("danger", approval=True)]) is True


class TestDispatchTool:
    def test_first_matching_toolkit_handles_tool(self):
        a, b = toolkit("other"), toolkit("kb_search", result="result from b")
        assert dispatch_tool("kb_search", {"query": "x"}, [a, b]) == (
            "result from b",
            False,
        )
        a.execute_tool.assert_not_called()

    def test_any_tool_failure_goes_back_as_an_error(self):
        broken = toolkit("boom", error=LookupError("no such row"))
        assert dispatch_tool("boom", {}, [broken]) == ("no such row", True)

    def test_unknown_tool(self):
        assert dispatch_tool("nope", {}, []) == ("Unknown tool: nope", True)


class TestPendingApproval:
    def test_dataclass(self):
        pa = PendingApproval(
            tool_use_id="toolu_01", tool_name="delete", arguments={"id": "123"}
        )
        assert pa.tool_use_id == "toolu_01"
        assert pa.tool_name == "delete"
        assert pa.arguments == {"id": "123"}


class TestExtraTools:
    def test_extra_tools_defaults_to_none(self):
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        assert sig.parameters["extra_tools"].default is None


class TestHelperFunctions:
    def test_collect_toolkit_schemas(self):
        toolkit_a = MagicMock()
        toolkit_a.get_tools_schema.return_value = [{"name": "tool_a"}]
        toolkit_b = MagicMock()
        toolkit_b.get_tools_schema.return_value = [
            {"name": "tool_b"},
            {"name": "tool_c"},
        ]
        result = collect_toolkit_schemas([toolkit_a, toolkit_b], MagicMock())
        assert result == [{"name": "tool_a"}, {"name": "tool_b"}, {"name": "tool_c"}]

    def test_find_toolkit(self):
        a, b = toolkit("other"), toolkit("kb_search")
        assert find_toolkit([a, b], "kb_search") is b
        assert find_toolkit([a], "unknown") is None
