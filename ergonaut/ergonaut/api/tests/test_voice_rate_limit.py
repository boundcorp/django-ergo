from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, close_old_connections, connection
from django_ergo.conversation.models import ConversationSession

from ergonaut.api import voice
from ergonaut.apps.bots.models import VoiceRateLimit


def test_fixed_windows_and_bounded_rows(db, django_user_model, settings, monkeypatch):
    user = django_user_model.objects.create_user("voice-limit")
    other = django_user_model.objects.create_user("voice-other")
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 1
    settings.VOICE_TRANSCRIPTION_PER_DAY = 2
    clock = [86399]
    monkeypatch.setattr(voice, "time", SimpleNamespace(time=lambda: clock[0]))

    assert voice._charge(user)
    assert not voice._charge(user)
    assert voice._charge(other)
    clock[0] = 86400  # UTC midnight also starts a new minute.
    assert voice._charge(user)
    clock[0] += 60
    assert voice._charge(user)
    clock[0] += 60
    assert not voice._charge(user)  # The new minute cannot override the daily limit.
    row = VoiceRateLimit.objects.get(user=user)
    assert (row.minute, row.minute_count, row.day, row.day_count) == (1442, 0, 1, 2)
    clock[0] = 172800
    assert voice._charge(user)
    assert VoiceRateLimit.objects.count() == 2
    user.delete()
    assert VoiceRateLimit.objects.count() == 1


@pytest.mark.parametrize("minute,day", [(0, 100), (5, 0), (-1, 100), (5, -1)])
def test_nonpositive_limit_rejects_without_increment(db, django_user_model, settings, minute, day):
    user = django_user_model.objects.create_user("voice-limit")
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = minute
    settings.VOICE_TRANSCRIPTION_PER_DAY = day
    assert not voice._charge(user)
    row = VoiceRateLimit.objects.get(user=user)
    assert (row.minute_count, row.day_count) == (0, 0)


def test_unavailable_and_inaccessible_requests_do_not_consume_attempts(
    test_user, django_user_model, settings, monkeypatch
):
    user, client = test_user
    owner = django_user_model.objects.create_user("voice-owner")
    own_session = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    other_session = ConversationSession.objects.create(user=owner, bot_name="test", engine_type="openai")
    settings.VOICE_TRANSCRIPTION_PROVIDER = "openai"
    settings.VOICE_TRANSCRIPTION_ENABLED = False
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert client.post(f"/api/sessions/{own_session.id}/voice", {}).status_code == 404
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    assert client.post(f"/api/sessions/{other_session.id}/voice", {}).status_code == 404
    assert not VoiceRateLimit.objects.exists()


def test_limiter_database_failure_fails_closed(test_user, settings, monkeypatch):
    user, client = test_user
    session = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    settings.VOICE_TRANSCRIPTION_PROVIDER = "openai"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fail_charge(_):
        raise DatabaseError("sensitive database details")

    monkeypatch.setattr(voice, "_charge", fail_charge)
    transcribe = AsyncMock()
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    response = client.post(f"/api/sessions/{session.id}/voice", {})
    assert response.status_code == 503
    assert response.json() == {"detail": "Voice transcription rate limiting is unavailable"}
    assert "sensitive" not in response.content.decode()
    assert not transcribe.called


def test_invalid_uploads_consume_attempts(test_user, settings, monkeypatch):
    user, client = test_user
    session = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    settings.VOICE_TRANSCRIPTION_PROVIDER = "openai"
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 1
    settings.VOICE_TRANSCRIPTION_PER_DAY = 100
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(voice, "time", SimpleNamespace(time=lambda: 86400))
    transcribe = AsyncMock()
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    url = f"/api/sessions/{session.id}/voice"
    assert client.post(url, {}).status_code == 400
    assert client.post(url, {}).status_code == 429
    assert not transcribe.called
    row = VoiceRateLimit.objects.get(user=user)
    assert (row.minute_count, row.day_count) == (1, 1)


def test_provider_failure_consumes_attempt(test_user, settings, monkeypatch):
    user, client = test_user
    session = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    settings.VOICE_TRANSCRIPTION_PROVIDER = "openai"
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 1
    settings.VOICE_TRANSCRIPTION_PER_DAY = 100
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(voice, "time", SimpleNamespace(time=lambda: 86400))
    monkeypatch.setattr(voice, "_duration", lambda *_: 1)
    transcribe = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    url = f"/api/sessions/{session.id}/voice"

    def audio():
        return SimpleUploadedFile("voice.wav", b"RIFFxxxxWAVEdata", content_type="audio/wav")

    assert client.post(url, {"audio": audio()}).status_code == 502
    assert client.post(url, {"audio": audio()}).status_code == 429
    assert transcribe.call_count == 1
    row = VoiceRateLimit.objects.get(user=user)
    assert (row.minute_count, row.day_count) == (1, 1)


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_requests_are_serialized(django_user_model, settings, monkeypatch):
    if not connection.features.has_select_for_update:
        pytest.skip("requires a database with row-level select_for_update")
    user = django_user_model.objects.create_user("voice-concurrent")
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 3
    settings.VOICE_TRANSCRIPTION_PER_DAY = 3
    monkeypatch.setattr(voice, "time", SimpleNamespace(time=lambda: 86400))
    barrier = Barrier(8)

    def attempt(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return voice._charge(user)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    assert sum(results) == 3
    row = VoiceRateLimit.objects.get(user=user)
    assert (row.minute_count, row.day_count) == (3, 3)
    assert VoiceRateLimit.objects.count() == 1
