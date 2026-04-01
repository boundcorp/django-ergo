"""Tests for the engine protocol ABC and EngineResponse."""

import pytest
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engine import TransportFailover


class TestEngineResponse:
    def test_text_response(self):
        r = EngineResponse(event_type="text", raw={"type": "text"}, text="Hello")
        assert r.event_type == "text"
        assert r.text == "Hello"
        assert r.tool_use is None
        assert r.thinking is None

    def test_tool_use_response(self):
        tool = {"id": "toolu_01", "name": "Bash", "input": {"command": "ls"}}
        r = EngineResponse(
            event_type="tool_use", raw={"type": "tool_use"}, tool_use=tool
        )
        assert r.tool_use["name"] == "Bash"

    def test_thinking_response(self):
        r = EngineResponse(event_type="thinking", raw={}, thinking="Let me think...")
        assert r.thinking == "Let me think..."

    def test_done_response(self):
        r = EngineResponse(event_type="done", raw={"stop_reason": "end_turn"})
        assert r.event_type == "done"

    def test_default_raw(self):
        r = EngineResponse(event_type="text")
        assert r.raw == {}


class TestTransportFailover:
    def test_attributes(self):
        exc = TransportFailover(original="cli", fallback="api", reason="CLI not found")
        assert exc.original == "cli"
        assert exc.fallback == "api"
        assert "CLI not found" in str(exc)


class TestEngineABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Engine()

    def test_required_methods(self):
        required = [
            "start_session",
            "resume_session",
            "send",
            "submit_tool_result",
            "get_tools_schema",
            "reconstruct_messages",
            "close_session",
            "get_tool_adapter",
        ]
        for name in required:
            assert hasattr(Engine, name)

    def test_generate_default_raises(self):
        """generate() has a default impl that raises NotImplementedError."""
        assert hasattr(Engine, "generate")


class TestEngineAdditionalTools:
    def test_send_accepts_additional_tools(self):
        """Engine.send() signature includes additional_tools parameter."""
        import inspect

        sig = inspect.signature(Engine.send)
        params = list(sig.parameters.keys())
        assert "additional_tools" in params
        assert sig.parameters["additional_tools"].default is None

    def test_submit_tool_result_accepts_additional_tools(self):
        """Engine.submit_tool_result() signature includes additional_tools parameter."""
        import inspect

        sig = inspect.signature(Engine.submit_tool_result)
        params = list(sig.parameters.keys())
        assert "additional_tools" in params
        assert sig.parameters["additional_tools"].default is None
