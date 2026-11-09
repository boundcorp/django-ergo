import array
import io
import json
import math
import shutil
import subprocess
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django_ergo.conversation.models import ConversationAttachment, ConversationSession, StructuredCall

from ergonaut.api import voice


@pytest.fixture
def setup_voice(test_user, settings, monkeypatch):
    user, client = test_user
    session = ConversationSession.objects.create(user=user, bot_name="test", engine_type="openai")
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 5
    settings.VOICE_TRANSCRIPTION_PER_DAY = 100
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return user, client, f"/api/sessions/{session.id}/voice"


def audio_file(data=None, content_type="audio/wav"):
    if data is None:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 8000)
        data = buf.getvalue()
    return SimpleUploadedFile("voice.wav", data, content_type=content_type)


def test_config_auth_and_ownership(setup_voice, settings, monkeypatch, django_user_model):
    user, client, url = setup_voice
    assert client.get(url).json() == {
        "enabled": True,
        "max_duration": settings.VOICE_TRANSCRIPTION_MAX_DURATION,
    }
    settings.VOICE_TRANSCRIPTION_ENABLED = False
    assert not client.get(url).json()["enabled"]
    assert client.post(url, {"audio": audio_file()}).status_code == 404
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    monkeypatch.delenv("OPENAI_API_KEY")
    assert not client.get(url).json()["enabled"]
    settings.VOICE_TRANSCRIPTION_PROVIDER = "other"
    assert not client.get(url).json()["enabled"]
    client.force_login(django_user_model.objects.create_user("other", "other@example.com", "pw"))
    assert client.get(url).status_code == 404
    assert client.post(url, {"audio": audio_file()}).status_code == 404
    client.logout()
    assert client.get(url).status_code == 401
    assert client.get("/api/sessions/not-a-uuid/voice").status_code == 401


def test_transcription_is_ephemeral_and_configurable(setup_voice, settings, monkeypatch, caplog, tmp_path):
    _, client, url = setup_voice
    settings.FILE_UPLOAD_MAX_MEMORY_SIZE = 0
    settings.FILE_UPLOAD_TEMP_DIR = str(tmp_path)
    settings.VOICE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
    monkeypatch.setattr(voice, "_duration", lambda *_: 1.0)
    transcribe = AsyncMock(return_value=SimpleNamespace(text="hello world", usage=None))
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    response = client.post(url, {"audio": audio_file()})
    assert response.status_code == 200
    assert response.json() == {"text": "hello world"}
    assert transcribe.call_args.kwargs == {
        "model": "gpt-4o-mini-transcribe",
        "request_timeout": settings.VOICE_TRANSCRIPTION_TIMEOUT,
    }
    assert ConversationAttachment.objects.count() == 0
    assert not list(tmp_path.iterdir())
    assert "hello world" not in caplog.text


def test_validation_and_provider_errors(setup_voice, settings, monkeypatch):
    _, client, url = setup_voice
    transcribe = AsyncMock(return_value=SimpleNamespace(text="secret", usage=None))
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    assert client.post(url, {}).status_code == 400
    assert client.post(url, {"audio": audio_file(b"not audio")}).status_code == 400
    assert client.post(url, {"audio": audio_file(content_type="audio/webm")}).status_code == 400
    settings.VOICE_TRANSCRIPTION_MAX_BYTES = 20
    assert client.post(url, {"audio": audio_file()}).status_code == 413
    settings.VOICE_TRANSCRIPTION_MAX_BYTES = 1024 * 1024
    monkeypatch.setattr(voice, "_duration", lambda *_: settings.VOICE_TRANSCRIPTION_MAX_DURATION + 1)
    assert client.post(url, {"audio": audio_file()}).status_code == 400
    assert not transcribe.called
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 100
    monkeypatch.setattr(voice, "_duration", lambda *_: 1)
    transcribe.side_effect = RuntimeError("private provider error and secret")
    response = client.post(url, {"audio": audio_file()})
    assert response.status_code == 502
    assert response.json() == {"detail": "Voice transcription failed"}


def test_rate_limits_are_per_user_and_window(setup_voice, settings, monkeypatch, django_user_model):
    user, client, url = setup_voice
    settings.VOICE_TRANSCRIPTION_PER_MINUTE = 1
    settings.VOICE_TRANSCRIPTION_PER_DAY = 2
    clock = [86400.0]
    monkeypatch.setattr(voice, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(voice, "_duration", lambda *_: 1)
    transcribe = AsyncMock(return_value=SimpleNamespace(text="ok", usage=None))
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    assert client.post(url, {"audio": audio_file()}).status_code == 200
    assert client.post(url, {"audio": audio_file()}).status_code == 429
    clock[0] += 60
    assert client.post(url, {"audio": audio_file()}).status_code == 200
    clock[0] += 60
    assert client.post(url, {"audio": audio_file()}).status_code == 429
    other = django_user_model.objects.create_user("other", "other@example.com", "pw")
    other_session = ConversationSession.objects.create(user=other, bot_name="test", engine_type="openai")
    client.force_login(other)
    assert client.post(f"/api/sessions/{other_session.id}/voice", {"audio": audio_file()}).status_code == 200
    client.force_login(user)
    clock[0] += 86400
    assert client.post(url, {"audio": audio_file()}).status_code == 200


def test_session_csrf_required(setup_voice):
    user, _, url = setup_voice
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(url, {"audio": audio_file()}).status_code == 403


def test_provider_rate_limit_and_timeout_are_clean(setup_voice, monkeypatch):
    import httpx
    import openai

    _, client, url = setup_voice
    monkeypatch.setattr(voice, "_duration", lambda *_: 1)
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.com"))
    transcribe = AsyncMock(side_effect=openai.RateLimitError("secret", response=response, body=None))
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    result = client.post(url, {"audio": audio_file()})
    assert result.status_code == 429
    assert "secret" not in result.content.decode()
    transcribe.side_effect = TimeoutError("secret")
    result = client.post(url, {"audio": audio_file()})
    assert result.status_code == 502
    assert "secret" not in result.content.decode()


def test_duration_probes_packets_without_access_to_other_protocols(monkeypatch):
    def run(args, **kwargs):
        assert args[args.index("-protocol_whitelist") + 1] == "pipe"
        return type(
            "Result",
            (),
            {
                "stdout": json.dumps(
                    {
                        "packets": [
                            {"pts_time": "0", "duration_time": "0.5"},
                            {"pts_time": "0.5", "duration_time": "0.5"},
                        ]
                    }
                ).encode()
            },
        )()

    monkeypatch.setattr(voice.subprocess, "run", run)
    assert voice._duration(b"audio", "wav") == 1


def test_missing_or_broken_probe_returns_clean_json(setup_voice, monkeypatch):
    _, client, url = setup_voice

    def missing(*args, **kwargs):
        raise FileNotFoundError("private path")

    monkeypatch.setattr(voice.subprocess, "run", missing)
    response = client.post(url, {"audio": audio_file()})
    assert response.status_code == 503
    assert response.json() == {"detail": "Audio validation is unavailable"}

    def malformed(*args, **kwargs):
        return SimpleNamespace(stdout=b"not json")

    monkeypatch.setattr(voice.subprocess, "run", malformed)
    response = client.post(url, {"audio": audio_file()})
    assert response.status_code == 400
    assert response.json() == {"detail": "Could not read audio duration"}


@pytest.mark.parametrize(
    ("mime", "magic", "expected"),
    [
        (
            "audio/webm;codecs=opus",
            b"\x1a\x45\xdf\xa3more",
            ("matroska", "audio/webm", "audio.webm"),
        ),
        ("audio/mp4", b"\x00\x00\x00\x18ftypmore", ("mov", "audio/mp4", "audio.m4a")),
        ("audio/mpeg", b"ID3more", ("mp3", "audio/mpeg", "audio.mp3")),
        ("audio/wav", b"RIFF\0\0\0\0WAVE", ("wav", "audio/wav", "audio.wav")),
        ("audio/x-m4a", b"\x00\x00\x00\x18ftypmore", ("mov", "audio/mp4", "audio.m4a")),
    ],
)
def test_browser_formats_get_safe_provider_filenames(mime, magic, expected):
    assert voice._format(magic, mime) == expected


@pytest.mark.parametrize("mime", ["audio/ogg", "application/ogg", "audio/ogg;codecs=opus"])
def test_ogg_is_rejected_before_probing_or_provider(setup_voice, monkeypatch, mime):
    _, client, url = setup_voice
    transcribe = AsyncMock()
    probe = AsyncMock()
    monkeypatch.setattr(voice, "openai_transcription", transcribe)
    monkeypatch.setattr(voice, "_duration", probe)
    response = client.post(url, {"audio": audio_file(b"OggSmore", content_type=mime)})
    assert response.status_code == 400
    assert response.json() == {"detail": "Unsupported or invalid audio format"}
    assert not probe.called
    assert not transcribe.called


def test_chunk_failure_returns_no_partial_text_but_accounts_success(setup_voice, monkeypatch):
    _, client, url = setup_voice
    monkeypatch.setattr(voice, "_duration", lambda *_: 601)
    monkeypatch.setattr(
        voice,
        "_chunks",
        lambda *_: [(b"one", "audio/mpeg", "one.mp3", 300), (b"two", "audio/mpeg", "two.mp3", 300)],
    )
    provider = AsyncMock(side_effect=[SimpleNamespace(text="private words", usage=None), RuntimeError("secret")])
    monkeypatch.setattr(voice, "openai_transcription", provider)
    response = client.post(url, {"audio": audio_file()})
    assert response.status_code == 502
    assert response.json() == {"detail": "Voice transcription failed"}
    assert StructuredCall.objects.filter(kind="voice_transcription").count() == 1


def test_join_transcripts_preserves_uncertain_boundaries():
    assert voice._join_transcripts(["one two three", "Two three four", "five"]) == "one two three four five"
    assert voice._join_transcripts(["a word", "word again"]) == "a word word again"


def test_chunks_cover_boundaries_and_provider_limits(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((float(args[args.index("-ss") + 1]), float(args[args.index("-t") + 1])))
        return SimpleNamespace(stdout=b"encoded mp3")

    monkeypatch.setattr(voice.subprocess, "run", run)
    chunks = voice._chunks(b"audio", "matroska", 1801)
    assert calls == [(0, 300), (297, 300), (594, 300), (891, 300), (1188, 300), (1485, 300), (1782, 19)]
    assert [chunk[3] for chunk in chunks] == [length for _, length in calls]


def test_long_real_recording_through_endpoint(setup_voice, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    _, client, url = setup_voice
    audio = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=sin(2*PI*if(lt(t\\,200)\\,400\\,if(lt(t\\,500)\\,600\\,800))*t):s=8000:d=610",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    seen = []

    async def provider(data, mime, filename, **kwargs):
        assert (mime, filename) == ("audio/mpeg", "audio.mp3")
        duration = voice._duration(data, "mp3")
        pcm = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
                "-t",
                "1",
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "8000",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout
        samples = array.array("h", pcm)

        def power(frequency):
            real = sum(sample * math.cos(2 * math.pi * frequency * i / 8000) for i, sample in enumerate(samples))
            imag = sum(sample * math.sin(2 * math.pi * frequency * i / 8000) for i, sample in enumerate(samples))
            return real * real + imag * imag

        frequency = max((400, 600, 800), key=power)
        seen.append((duration, frequency))
        return SimpleNamespace(text=f"frequency {frequency}", usage=None)

    monkeypatch.setattr(voice, "openai_transcription", provider)
    response = client.post(url, {"audio": audio_file(audio, content_type="audio/webm")})
    assert response.status_code == 200
    assert len(seen) == 3
    assert [frequency for _, frequency in seen] == [400, 600, 800]
    assert [round(duration) for duration, _ in seen] == [300, 300, 16]
    assert response.json()["text"] == " ".join(f"frequency {frequency}" for _, frequency in seen)
    assert StructuredCall.objects.filter(kind="voice_transcription").count() == 3
