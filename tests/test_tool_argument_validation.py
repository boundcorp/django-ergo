"""Tool arguments are checked against the declared schema before dispatch."""

import json

import pytest

from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import ToolContext
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.tool_dispatch import ToolError
from django_ergo.conversation.tool_dispatch import dispatch_tool

CALLS: list = []


@bot_tool
def apply(file_id: str, ops: list[str], page: str = "") -> str:
    CALLS.append((file_id, ops, page))
    return "ok"


@bot_tool(takes_context=True)
def with_ctx(ctx, name: str, flag: bool = False) -> str:
    return f"{name}:{flag}"


@bot_tool
def loose(name: str, **extra) -> str:
    return f"{name}:{sorted(extra)}"


def toolkit():
    CALLS.clear()
    return FunctionToolkit.from_functions([apply, with_ctx, loose], ToolContext())


def test_missing_required_never_calls_function():
    with pytest.raises(ToolError) as info:
        toolkit().execute_tool("apply", {"ops": []})
    assert "missing required argument(s): file_id" in str(info.value)
    assert info.value.fields["category"] == "invalid_arguments"
    assert info.value.fields["retryable"] is True
    assert info.value.fields["state_changed"] is False
    assert info.value.fields["missing"] == ["file_id"]
    assert CALLS == []


def test_dispatch_serializes_structured_error():
    result, is_error = dispatch_tool("apply", {"ops": []}, [toolkit()])
    assert is_error
    body = json.loads(result)
    assert body["category"] == "invalid_arguments"
    assert "file_id" in body["error"]


def test_unknown_argument_suggests_close_match():
    with pytest.raises(ToolError) as info:
        toolkit().execute_tool("apply", {"file_id": "f", "ops": [], "pagee": "x"})
    assert "pagee (did you mean page?)" in str(info.value)
    assert info.value.fields["unknown"] == ["pagee"]
    assert CALLS == []


def test_valid_call_unchanged():
    tk = toolkit()
    assert tk.execute_tool("apply", {"file_id": "f", "ops": ["a"]}) == "ok"
    assert CALLS == [("f", ["a"], "")]
    assert tk.execute_tool("with_ctx", {"name": "n"}) == "n:False"
    assert tk.execute_tool("with_ctx", {"name": "n", "flag": True}) == "n:True"


def test_kwargs_tool_accepts_extras():
    assert toolkit().execute_tool("loose", {"name": "a", "x": 1}) == "a:['x']"


def test_kwargs_tool_still_needs_required():
    with pytest.raises(ToolError):
        toolkit().execute_tool("loose", {"x": 1})
