"""Offline regression checks for consumer-specific credential names."""

import pytest
from django.test import override_settings

from django_ergo.openai_credentials import selected_openai_key
from django_ergo.openai_credentials import voice_openai_key


def test_named_key_is_exclusive(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.setenv("TEST_ORG_KEY", "org-fake")
    assert selected_openai_key("TEST_ORG_KEY") == "org-fake"
    monkeypatch.delenv("TEST_ORG_KEY")
    with pytest.raises(ValueError, match="TEST_ORG_KEY"):
        selected_openai_key("TEST_ORG_KEY")
    assert selected_openai_key() == "ambient-fake"


@override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "TEST_VOICE_KEY"})
def test_voice_selection_is_independent(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.setenv("TEST_VOICE_KEY", "voice-fake")
    assert voice_openai_key() == "voice-fake"
    monkeypatch.delenv("TEST_VOICE_KEY")
    with pytest.raises(ValueError, match="TEST_VOICE_KEY"):
        voice_openai_key()


def test_chat_named_key_never_falls_back(monkeypatch):
    from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.delenv("TEST_CHAT_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_CHAT_KEY"):
        OpenAIAPIEngine({"api_key_env": "TEST_CHAT_KEY"})
    monkeypatch.setenv("TEST_CHAT_KEY", "chat-fake")
    assert OpenAIAPIEngine({"api_key_env": "TEST_CHAT_KEY"}).api_key == "chat-fake"


def test_embedding_named_key_never_falls_back(monkeypatch):
    from django_ergo.embedding_providers import OpenAIEmbeddingProvider

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.delenv("TEST_EMBED_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_EMBED_KEY"):
        OpenAIEmbeddingProvider({"api_key_env": "TEST_EMBED_KEY"})
    monkeypatch.setenv("TEST_EMBED_KEY", "embedding-fake")
    assert (
        OpenAIEmbeddingProvider({"api_key_env": "TEST_EMBED_KEY"}).api_key
        == "embedding-fake"
    )


async def test_concurrent_instance_clients_use_only_selected_keys(monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    import openai

    from django_ergo.conversation.attachments import openai_transcription
    from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
    from django_ergo.embedding_providers import OpenAIEmbeddingProvider

    seen = []

    def respond(request):
        seen.append((request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1536}]})
        if request.url.path.endswith("transcriptions"):
            return httpx.Response(200, json={"text": "offline"})
        return httpx.Response(
            200,
            json={
                "choices": [],
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": "fake",
            },
        )

    sync_factory, async_factory = openai.OpenAI, openai.AsyncOpenAI
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kw: sync_factory(
            **kw, http_client=httpx.Client(transport=httpx.MockTransport(respond))
        ),
    )
    monkeypatch.setattr(
        openai,
        "AsyncOpenAI",
        lambda **kw: async_factory(
            **kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.setenv("ORG_CHAT", "chat-fake")
    monkeypatch.setenv("ORG_EMBED", "embedding-fake")
    monkeypatch.setenv("ORG_VOICE", "voice-fake")
    monkeypatch.setattr(openai, "api_key", "global-must-remain")
    embeddings = [
        OpenAIEmbeddingProvider({"api_key_env": name})
        for name in ("ORG_EMBED", "ORG_CHAT")
    ]
    chats = [
        OpenAIAPIEngine({"api_key_env": name}) for name in ("ORG_CHAT", "ORG_EMBED")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(
            len(vector) == 1536
            for vector in pool.map(
                lambda p: p.generate_embedding("offline"), embeddings
            )
        )
    with override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "ORG_VOICE"}):
        await asyncio.gather(
            *(
                engine.client.chat.completions.create(model="fake", messages=[])
                for engine in chats
            ),
            openai_transcription(b"offline", "audio/wav", filename="fake.wav"),
        )
    assert openai.api_key == "global-must-remain"
    assert sorted(seen) == sorted(
        [
            ("/v1/embeddings", "Bearer embedding-fake"),
            ("/v1/embeddings", "Bearer chat-fake"),
            ("/v1/chat/completions", "Bearer chat-fake"),
            ("/v1/chat/completions", "Bearer embedding-fake"),
            ("/v1/audio/transcriptions", "Bearer voice-fake"),
        ]
    )
    for provider in embeddings:
        provider.client.close()
    for engine in chats:
        await engine.client.close()


async def test_transcription_missing_custom_key_never_uses_ambient(monkeypatch):
    from django_ergo.conversation.attachments import openai_transcription

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    with (
        override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "MISSING_VOICE"}),
        pytest.raises(ValueError, match="MISSING_VOICE"),
    ):
        await openai_transcription(b"offline", "audio/wav", filename="fake.wav")
