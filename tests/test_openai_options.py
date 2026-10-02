"""Tests for model-dependent OpenAI request options and the default model."""

from __future__ import annotations

import pytest

from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.openai_options import DEFAULT_OPENAI_MODEL
from django_ergo.openai_options import chat_options
from django_ergo.settings import api_settings
from tests.test_conversation_structured import FakeOpenAIClient
from tests.test_conversation_structured import openai_tool


def test_default_model_is_gpt_6_luna():
    assert DEFAULT_OPENAI_MODEL == "gpt-6-luna"
    assert api_settings.OPENAI_MODEL == "gpt-6-luna"
    assert OpenAIAPIEngine(config={}).model == "gpt-6-luna"


def test_gpt_6_uses_no_reasoning_for_tool_calls():
    assert chat_options(
        "gpt-6-luna",
        temperature=0.7,
        max_tokens=100,
        reasoning_effort="high",
        tools=True,
    ) == {"max_completion_tokens": 100, "reasoning_effort": "none"}
    assert chat_options("gpt-6-luna", temperature=0.7, reasoning_effort="high") == {
        "reasoning_effort": "high"
    }
    assert chat_options("gpt-6-luna", temperature=0.7) == {}


def test_older_models_keep_temperature():
    assert chat_options("gpt-4o", temperature=0.7, max_tokens=50, tools=True) == {
        "temperature": 0.7,
        "max_completion_tokens": 50,
    }
    # Other reasoning families get no automatic effort and no temperature.
    assert chat_options("o4-mini", temperature=0.7, tools=True) == {}


@pytest.mark.asyncio
async def test_engine_sends_gpt_6_tool_options():
    engine = OpenAIAPIEngine(config={"max_tokens": 256})
    engine._client = FakeOpenAIClient(openai_tool("lookup", {"q": "x"}))
    await engine.complete(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
    )
    sent = engine._client.calls[0]
    assert sent["model"] == "gpt-6-luna"
    assert sent["reasoning_effort"] == "none"
    assert sent["max_completion_tokens"] == 256
    assert "temperature" not in sent
    assert "max_tokens" not in sent
