"""Tests for Engine.generate() — one-shot typed output."""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from pydantic import BaseModel

EXPECTED_RATING = 9.5


class MovieReview(BaseModel):
    title: str
    rating: float
    summary: str


class TestClaudeAPIGenerate:
    def test_generate_text(self):
        engine = ClaudeAPIEngine(config={"api_key": "test-key"})
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello!")]
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=mock_response
            )
            result = async_to_sync(engine.generate)("Say hello")

        assert result.event_type == "done"
        assert result.text == "Hello!"

    def test_generate_with_response_model(self):
        engine = ClaudeAPIEngine(config={"api_key": "test-key"})
        tool_input = {
            "title": "Inception",
            "rating": EXPECTED_RATING,
            "summary": "Mind-bending",
        }
        mock_tool_block = MagicMock(type="tool_use", id="toolu_01", input=tool_input)
        mock_tool_block.name = "structured_output"
        mock_response = MagicMock()
        mock_response.content = [mock_tool_block]
        mock_response.stop_reason = "tool_use"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=50)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=mock_response
            )
            result = async_to_sync(engine.generate)(
                "Review Inception", response_model=MovieReview
            )

        parsed = result.raw["parsed"]
        assert isinstance(parsed, MovieReview)
        assert parsed.title == "Inception"
        assert parsed.rating == EXPECTED_RATING

    def test_generate_with_system(self):
        engine = ClaudeAPIEngine(config={"api_key": "test-key"})
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Arrr!")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(
                return_value=mock_response
            )
            async_to_sync(engine.generate)("Who are you?", system="You are a pirate.")
            call_kwargs = mock_client.return_value.messages.create.call_args.kwargs
            assert call_kwargs["system"] == "You are a pirate."


class TestOpenAIAPIGenerate:
    def test_generate_text(self):
        engine = OpenAIAPIEngine(config={"api_key": "test-key"})
        mock_choice = MagicMock()
        mock_choice.message.content = "Hello!"
        mock_choice.message.tool_calls = None
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(
                return_value=mock_response
            )
            result = async_to_sync(engine.generate)("Say hello")

        assert result.text == "Hello!"

    def test_generate_with_response_model(self):
        engine = OpenAIAPIEngine(config={"api_key": "test-key"})
        tool_call = MagicMock()
        tool_call.id = "call_abc"
        tool_call.function.name = "structured_output"
        tool_call.function.arguments = json.dumps(
            {"title": "Inception", "rating": EXPECTED_RATING, "summary": "Mind-bending"}
        )
        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [tool_call]
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=50)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(
                return_value=mock_response
            )
            result = async_to_sync(engine.generate)(
                "Review Inception", response_model=MovieReview
            )

        parsed = result.raw["parsed"]
        assert isinstance(parsed, MovieReview)
        assert parsed.title == "Inception"
