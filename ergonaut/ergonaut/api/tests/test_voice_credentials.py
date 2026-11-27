"""Actual installed voice SDK constructors, without sessions or provider requests."""

import asyncio
from types import SimpleNamespace

import pytest
from django.test import override_settings

from ergonaut.api import voice


def test_composer_missing_explicit_key_is_disabled(monkeypatch, settings):
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    with override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "VOICE_ONLY"}):
        assert not voice._enabled()
        monkeypatch.setenv("VOICE_ONLY", "voice-fake")
        assert voice._enabled()


@pytest.mark.asyncio
async def test_realtime_instances_keep_the_selected_key(monkeypatch):
    pytest.importorskip("livekit.plugins.openai.realtime.gpt_live_model")
    from ergonaut import realtime_voice_bridge as bridge

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.setenv("VOICE_ONE", "one-fake")
    monkeypatch.setenv("VOICE_TWO", "two-fake")
    with override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "VOICE_ONE"}):
        first = bridge.Assistant("offline", {"state": {}, "events": [], "delegations": []}, None)
    with override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "VOICE_TWO"}):
        second = bridge.Assistant("offline", {"state": {}, "events": [], "delegations": []}, None)
    assert first.llm.duplex_model._opts.api_key == "one-fake"
    assert second.llm.duplex_model._opts.api_key == "two-fake"
    with (
        override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "MISSING_VOICE"}),
        pytest.raises(ValueError, match="MISSING_VOICE"),
    ):
        bridge.Assistant("offline", {"state": {}, "events": [], "delegations": []}, None)
    await asyncio.gather(first.llm.aclose(), second.llm.aclose())


@pytest.mark.asyncio
async def test_text_bridge_tts_uses_explicit_instance_keys(monkeypatch):
    pytest.importorskip("livekit.plugins.openai")
    from ergonaut import realtime_voice_text_bridge as bridge

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-fake")
    monkeypatch.setenv("VOICE_ONE", "one-fake")
    monkeypatch.setenv("VOICE_TWO", "two-fake")
    monkeypatch.setattr(bridge, "media_scope", lambda *_: (None, {"tts_model": "tts-1", "tts_voice": "alloy"}))
    monkeypatch.setattr(bridge.silero.VAD, "load", lambda **_: None)
    clients = []
    constructor = bridge.openai.TTS

    def capture(**kwargs):
        result = constructor(**kwargs)
        clients.append(result)
        return result

    def stop_before_session(**_):
        raise RuntimeError("offline constructor check")

    monkeypatch.setattr(bridge.openai, "TTS", capture)
    monkeypatch.setattr(bridge, "AgentSession", stop_before_session)
    for name in ("VOICE_ONE", "VOICE_TWO"):
        with (
            override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": name}),
            pytest.raises(RuntimeError, match="offline constructor check"),
        ):
            await bridge.text_voice_session(SimpleNamespace(), "offline", "offline", {})
    assert [client._client.api_key for client in clients] == ["one-fake", "two-fake"]
    with (
        override_settings(DJANGO_ERGO={"VOICE_OPENAI_API_KEY_ENV": "MISSING_VOICE"}),
        pytest.raises(ValueError, match="MISSING_VOICE"),
    ):
        await bridge.text_voice_session(SimpleNamespace(), "offline", "offline", {})
    assert len(clients) == 2
