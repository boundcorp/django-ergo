from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django_ergo.conversation.models import ConversationSession, StructuredCall
from django_ergo.pricing import call_cost_parts

from ergonaut.api import voice


@pytest.fixture
def transcription(test_user, settings, monkeypatch):
    user, client = test_user
    session = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    settings.VOICE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 100
    settings.VOICE_TRANSCRIPTION_PER_DAY = 100
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(voice, "_duration", lambda *_: 60.0)
    provider = AsyncMock(return_value=SimpleNamespace(text="private words", usage=None))
    monkeypatch.setattr(voice, "openai_transcription", provider)

    def send():
        audio = SimpleUploadedFile("a.wav", b"RIFF0000WAVEdata", content_type="audio/wav")
        return client.post(f"/api/sessions/{session.id}/voice", {"audio": audio})

    return user, session, client, provider, send


@pytest.mark.parametrize("include_text_tokens", [True, False])
def test_token_usage_is_attributed_and_in_cost_report(transcription, include_text_tokens):
    user, session, client, provider, send = transcription
    details = {"audio_tokens": 1000}
    if include_text_tokens:
        details["text_tokens"] = 100
    provider.return_value.usage = SimpleNamespace(
        input_tokens=1100,
        output_tokens=200,
        input_token_details=SimpleNamespace(**details),
    )
    assert send().status_code == 200
    row = StructuredCall.objects.get(kind="voice_transcription")
    assert row.user == user and row.session == session
    assert row.input_tokens == 1100 and row.output_tokens == 200
    assert row.cost_usd == Decimal("0.004125")
    assert row.metadata["transcription"] == {
        "cost_source": "tokens",
        "estimated": False,
        "unpriced": False,
        "audio_tokens": 1000,
        "text_tokens": 100,
    }
    assert "private words" not in str(row.metadata) + row.request + str(row.response)
    result = client.get("/api/costs").json()
    assert result["total"]["cost"] == pytest.approx(0.004125)
    assert any(r["name"] == "voice_transcription" and r["calls"] == 1 for r in result["by_kind"])


def test_paid_transcription_in_cli_session(transcription):
    _, session, client, _, send = transcription
    session.transport_type = "cli"
    session.save(update_fields=["transport_type"])
    assert send().status_code == 200
    row = StructuredCall.objects.get(kind="voice_transcription")
    assert sum(call_cost_parts(row).values()) == pytest.approx(0.003)
    total = client.get("/api/costs").json()["total"]
    assert total["cost"] == pytest.approx(0.003)
    assert total["subscription_calls"] == 0


@pytest.mark.parametrize(
    "model,usage,expected,estimated",
    [
        ("gpt-transcribe", None, "0.0045", True),
        ("gpt-transcribe", SimpleNamespace(seconds=30.0), "0.00225", False),
        ("gpt-4o-mini-transcribe", None, "0.003", True),
        ("whisper-1", None, "0.006", True),
        ("whisper-1", SimpleNamespace(seconds=30.0), "0.003", False),
    ],
)
def test_duration_costs(transcription, settings, model, usage, expected, estimated):
    _, _, _, provider, send = transcription
    settings.VOICE_TRANSCRIPTION_MODEL = model
    provider.return_value.usage = usage
    assert send().status_code == 200
    row = StructuredCall.objects.get(kind="voice_transcription")
    assert row.cost_usd == Decimal(expected)
    assert row.metadata["transcription"] == {
        "cost_source": "duration",
        "estimated": estimated,
        "unpriced": False,
        "seconds": 60.0 if usage is None else 30.0,
    }


def test_unknown_model_and_failure(transcription, settings, caplog):
    _, session, client, provider, send = transcription
    settings.VOICE_TRANSCRIPTION_MODEL = "unknown-model"
    session.transport_type = "cli"
    session.save(update_fields=["transport_type"])
    assert send().status_code == 200
    row = StructuredCall.objects.get(kind="voice_transcription")
    assert row.cost_usd is None and row.metadata["transcription"]["unpriced"]
    assert call_cost_parts(row) is None
    assert "private words" not in caplog.text
    assert client.get("/api/costs").json()["total"]["unpriced_calls"] == 1
    provider.side_effect = RuntimeError("private words")
    assert send().status_code == 502
    assert StructuredCall.objects.filter(kind="voice_transcription").count() == 1
