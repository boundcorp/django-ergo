"""Public inference clients refuse the private (Fireworks) endpoint.

Every SDK client here is a mock: no test makes a network call.
"""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import anthropic
import openai
import pytest

from django_ergo.conversation.attachments import openai_transcriber
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.embedding_providers import OpenAIEmbeddingProvider

FIREWORKS_URL = "https://api.fireworks.ai/inference/v1"
PRIVATE_URLS = [FIREWORKS_URL, "https://foo.fireworks.ai", "fireworks.ai"]


@pytest.fixture(autouse=True)
def _public_env(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test-sentinel-123")


def claude_response():
    response = MagicMock()
    response.content = [MagicMock(type="text", text="ok")]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=1, output_tokens=1)
    return response


def mock_claude_client(base_url="https://api.anthropic.com"):
    client = MagicMock()
    client.base_url = base_url
    client.messages.create = AsyncMock(return_value=claude_response())
    return client


# -- Claude API engine --------------------------------------------------------


@pytest.mark.parametrize("base_url", PRIVATE_URLS)
async def test_claude_refuses_private_base_url_before_building_client(base_url):
    engine = ClaudeAPIEngine(config={"api_key": "test-key", "base_url": base_url})
    with (
        patch.object(anthropic, "AsyncAnthropic") as sdk,
        pytest.raises(PolicyError, match="fireworks.ai"),
    ):
        await engine.generate("hi")
    sdk.assert_not_called()
    assert engine._client is None


async def test_claude_refuses_private_anthropic_base_url_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", FIREWORKS_URL)
    engine = ClaudeAPIEngine(config={"api_key": "test-key"})
    with (
        patch.object(anthropic, "AsyncAnthropic") as sdk,
        pytest.raises(PolicyError),
    ):
        await engine.complete([{"role": "user", "content": "hi"}])
    sdk.assert_not_called()


async def test_claude_public_base_url_builds_client_and_sends():
    engine = ClaudeAPIEngine(
        config={"api_key": "test-key", "base_url": "https://proxy.example.com"}
    )
    client = mock_claude_client("https://proxy.example.com")
    with patch.object(anthropic, "AsyncAnthropic", return_value=client) as sdk:
        result = await engine.generate("hi")
    sdk.assert_called_once_with(
        api_key="test-key", base_url="https://proxy.example.com"
    )
    client.messages.create.assert_awaited_once()
    assert result.text == "ok"


async def test_claude_generate_refuses_client_mutated_to_private():
    engine = ClaudeAPIEngine(config={"api_key": "test-key"})
    engine._client = mock_claude_client()
    engine._client.base_url = FIREWORKS_URL
    with pytest.raises(PolicyError):
        await engine.generate("hi")
    engine._client.messages.create.assert_not_awaited()


async def test_claude_complete_refuses_client_mutated_to_private():
    engine = ClaudeAPIEngine(config={"api_key": "test-key"})
    engine._client = mock_claude_client("https://x.fireworks.ai/v1")
    with pytest.raises(PolicyError):
        await engine.complete([{"role": "user", "content": "hi"}])
    engine._client.messages.create.assert_not_awaited()


async def test_claude_session_call_refuses_client_mutated_to_private():
    engine = ClaudeAPIEngine(config={"api_key": "test-key"})
    engine._client = mock_claude_client(FIREWORKS_URL)
    session = MagicMock(id=1, workflow=None)
    with (
        patch.object(engine, "reconstruct_messages", return_value=[]),
        pytest.raises(PolicyError),
    ):
        async for _event in engine._call(session):
            pass
    engine._client.messages.create.assert_not_awaited()


# -- audio transcription --------------------------------------------------------


def mock_transcription_client(base_url="https://api.openai.com/v1"):
    client = MagicMock()
    client.base_url = base_url
    client.audio.transcriptions.create = AsyncMock(return_value=MagicMock(text="hi"))
    return client


async def test_transcriber_refuses_private_openai_base_url_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", FIREWORKS_URL)
    with (
        patch.object(openai, "AsyncOpenAI") as sdk,
        pytest.raises(PolicyError),
    ):
        await openai_transcriber(b"RIFF", "audio/wav", "a.wav")
    sdk.assert_not_called()


async def test_transcriber_refuses_private_client():
    client = mock_transcription_client(FIREWORKS_URL)
    with (
        patch.object(openai, "AsyncOpenAI", return_value=client),
        pytest.raises(PolicyError),
    ):
        await openai_transcriber(b"RIFF", "audio/wav", "a.wav")
    client.audio.transcriptions.create.assert_not_awaited()


async def test_transcriber_public_endpoint_sends():
    client = mock_transcription_client()
    with patch.object(openai, "AsyncOpenAI", return_value=client):
        text = await openai_transcriber(b"RIFF", "audio/wav", "a.wav")
    assert text == "hi"
    client.audio.transcriptions.create.assert_awaited_once()


# -- embeddings -----------------------------------------------------------------


def embedding_provider(*, base_url=None, module_client=None):
    sdk = MagicMock()
    sdk.base_url = base_url
    sdk._client = module_client
    sdk.embeddings.create.return_value.data = [MagicMock(embedding=[0.1] * 1536)]
    with patch.dict("sys.modules", {"openai": sdk}):
        provider = OpenAIEmbeddingProvider({"api_key": "test-key"})
    return provider, sdk


def test_embeddings_refuse_private_openai_base_url_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", FIREWORKS_URL)
    provider, sdk = embedding_provider()
    with pytest.raises(PolicyError):
        provider.generate_embedding("text")
    sdk.embeddings.create.assert_not_called()


def test_embeddings_refuse_private_module_base_url():
    provider, sdk = embedding_provider(base_url="https://foo.fireworks.ai/v1")
    with pytest.raises(PolicyError):
        provider.generate_embedding("text")
    sdk.embeddings.create.assert_not_called()


def test_embeddings_refuse_cached_private_module_client():
    provider, sdk = embedding_provider(module_client=MagicMock(base_url=FIREWORKS_URL))
    with pytest.raises(PolicyError):
        provider.generate_embedding("text")
    sdk.embeddings.create.assert_not_called()


def test_embeddings_public_endpoint_sends():
    provider, sdk = embedding_provider(
        module_client=MagicMock(base_url="https://api.openai.com/v1")
    )
    assert provider.generate_embedding("text") == [0.1] * 1536
    sdk.embeddings.create.assert_called_once()
