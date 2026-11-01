"""Ephemeral composer dictation. Audio and transcripts are never saved by this API."""

import asyncio
import json
import math
import os
import subprocess
import time

import openai
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django_ergo.conversation.attachments import openai_transcriber
from ninja import Router
from ninja.errors import HttpError

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import uuid_or_404, visible_sessions
from ergonaut.apps.bots.models import VoiceRateLimit

router = Router(tags=["voice"], auth=user_auth)


def _enabled():
    return bool(
        settings.VOICE_TRANSCRIPTION_ENABLED
        and settings.VOICE_TRANSCRIPTION_PROVIDER == "openai"
        and os.environ.get("OPENAI_API_KEY")
    )


def _authorize(request, session_id):
    if not visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).exists():
        raise HttpError(404, "No such session")


def _charge(user):
    # Lock the user row even for the first request, so creation and both counters
    # are serialized across all application processes and sessions.
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        row, _ = VoiceRateLimit.objects.get_or_create(user=user)
        now = int(time.time())
        minute, day = now // 60, now // 86400
        if row.minute != minute:
            row.minute, row.minute_count = minute, 0
        if row.day != day:
            row.day, row.day_count = day, 0
        if (
            row.minute_count >= settings.VOICE_TRANSCRIPTION_PER_MINUTE
            or row.day_count >= settings.VOICE_TRANSCRIPTION_PER_DAY
        ):
            return False
        row.minute_count += 1
        row.day_count += 1
        row.save(update_fields=["minute", "minute_count", "day", "day_count"])
        return True


def _format(data, content_type):
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in ("audio/webm", "video/webm") and data.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska", "audio/webm", "audio.webm"
    if mime in ("audio/ogg", "application/ogg") and data.startswith(b"OggS"):
        return "ogg", "audio/ogg", "audio.ogg"
    if mime in ("audio/mpeg", "audio/mp3") and (
        data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    ):
        return "mp3", "audio/mpeg", "audio.mp3"
    if mime in ("audio/wav", "audio/wave", "audio/x-wav") and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav", "audio/wav", "audio.wav"
    if mime in ("audio/mp4", "video/mp4", "audio/m4a", "audio/x-m4a") and data[4:8] == b"ftyp":
        return "mov", "audio/mp4", "audio.m4a"
    raise HttpError(400, "Unsupported or invalid audio format")


def _duration(data, demuxer):
    # Probe packet timestamps rather than relying on container duration metadata:
    # browser MediaRecorder WebM streams often omit the latter. Restrict ffprobe
    # to stdin, preventing a crafted container from opening network/local URLs.
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "pipe",
                "-f",
                demuxer,
                "-select_streams",
                "a:0",
                "-show_entries",
                "packet=pts_time,duration_time",
                "-of",
                "json",
                "-i",
                "pipe:0",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        packets = json.loads(result.stdout)["packets"]
        if not packets:
            raise ValueError("no audio packets")
        times = [(float(p["pts_time"]), float(p.get("duration_time", 0))) for p in packets]
        if any(not math.isfinite(t) or not math.isfinite(d) or d < 0 for t, d in times):
            raise ValueError("invalid timestamp")
        return max(t + d for t, d in times) - min(t for t, _ in times)
    except FileNotFoundError:
        raise HttpError(503, "Audio validation is unavailable") from None
    except (
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        raise HttpError(400, "Could not read audio duration") from None


@router.get("/sessions/{session_id}/voice")
def voice_config(request, session_id: str):
    _authorize(request, session_id)
    return {
        "enabled": _enabled(),
        "max_duration": settings.VOICE_TRANSCRIPTION_MAX_DURATION,
    }


@router.post("/sessions/{session_id}/voice")
def voice_transcribe(request, session_id: str):
    _authorize(request, session_id)
    if not _enabled():
        raise HttpError(404, "Voice transcription is unavailable")
    if not _charge(request.auth):
        raise HttpError(429, "Voice transcription rate limit exceeded")
    # Reject obviously oversized requests before Django parses/spools the body.
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HttpError(400, "Invalid request size") from None
    if length > settings.VOICE_TRANSCRIPTION_MAX_BYTES + 65536:
        raise HttpError(413, "Audio is too large")
    audio = request.FILES.get("audio")
    if audio is None:
        raise HttpError(400, "Audio file is required")
    try:
        if audio.size > settings.VOICE_TRANSCRIPTION_MAX_BYTES:
            raise HttpError(413, "Audio is too large")
        data = audio.read(settings.VOICE_TRANSCRIPTION_MAX_BYTES + 1)
        if not data or len(data) > settings.VOICE_TRANSCRIPTION_MAX_BYTES:
            raise HttpError(400 if not data else 413, "Audio is empty or too large")
        demuxer, media_type, filename = _format(data, audio.content_type)
        duration = _duration(data, demuxer)
        if duration <= 0:
            raise HttpError(400, "Audio duration is invalid")
        if duration > settings.VOICE_TRANSCRIPTION_MAX_DURATION:
            raise HttpError(400, "Audio exceeds the maximum duration")

        async def transcribe():
            return await asyncio.wait_for(
                openai_transcriber(
                    data,
                    media_type,
                    filename,
                    model=settings.VOICE_TRANSCRIPTION_MODEL,
                    request_timeout=settings.VOICE_TRANSCRIPTION_TIMEOUT,
                ),
                timeout=settings.VOICE_TRANSCRIPTION_TIMEOUT,
            )

        try:
            text = async_to_sync(transcribe)()
        except openai.RateLimitError:
            raise HttpError(429, "Transcription provider rate limit exceeded") from None
        except Exception:
            # Never log provider exceptions: they may contain request details.
            raise HttpError(502, "Voice transcription failed") from None
        return {"text": text}
    finally:
        audio.close()
