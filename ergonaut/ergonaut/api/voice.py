"""Ephemeral composer dictation. Audio and transcripts are never saved by this API."""

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import time
from decimal import Decimal

import openai
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, transaction
from django_ergo.conversation.attachments import openai_transcription
from django_ergo.conversation.models import StructuredCall, StructuredCallStatus
from ninja import Router
from ninja.errors import HttpError

from ergonaut.api.auth import user_auth
from ergonaut.api.bots import uuid_or_404, visible_sessions
from ergonaut.apps.bots.models import VoiceRateLimit

router = Router(tags=["voice"], auth=user_auth)

logger = logging.getLogger(__name__)

# USD per minute; token rates are USD per million. Source:
# https://developers.openai.com/api/docs/models/gpt-transcribe
# https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe
# https://developers.openai.com/api/docs/models/whisper-1
TRANSCRIPTION_PRICES = {
    "gpt-transcribe": {"minute": Decimal("0.0045")},
    "gpt-4o-mini-transcribe": {
        "minute": Decimal("0.003"),
        "audio_input": Decimal("3"),
        "text_input": Decimal("1.25"),
        "text_output": Decimal("5"),
    },
    "whisper-1": {"minute": Decimal("0.006")},
}

# Keep each provider request comfortably below its duration and 25 MB limits.
CHUNK_SECONDS = 300
CHUNK_OVERLAP = 3
PROVIDER_MAX_BYTES = 24 * 1000 * 1000
CHUNK_CONCURRENCY = 4


def _enabled():
    return bool(
        settings.VOICE_TRANSCRIPTION_ENABLED
        and settings.VOICE_TRANSCRIPTION_PROVIDER == "openai"
        and os.environ.get("OPENAI_API_KEY")
    )


def _authorize(request, session_id):
    session = visible_sessions(request.auth).filter(id=uuid_or_404(session_id)).first()
    if session is None:
        raise HttpError(404, "No such session")
    return session


def _record_transcription(user, session, model, response, duration):
    """Record usage only: never persist audio, transcript, or provider errors."""
    price = TRANSCRIPTION_PRICES.get(model)
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    details = getattr(usage, "input_token_details", None)
    audio_tokens = getattr(details, "audio_tokens", None)
    text_tokens = getattr(details, "text_tokens", None)
    if (
        text_tokens is None
        and isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and isinstance(audio_tokens, int)
        and not isinstance(audio_tokens, bool)
    ):
        text_tokens = input_tokens - audio_tokens
    valid_tokens = (
        all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0
            for n in (input_tokens, output_tokens, audio_tokens, text_tokens)
        )
        and audio_tokens + text_tokens == input_tokens
    )
    parts = None
    estimated = False
    source = "unknown"
    if price and valid_tokens and "audio_input" in price:
        input_cost = (audio_tokens * price["audio_input"] + text_tokens * price["text_input"]) / Decimal(1000000)
        output_cost = output_tokens * price["text_output"] / Decimal(1000000)
        parts = {"input": float(input_cost), "cache_write": 0.0, "cache_read": 0.0, "output": float(output_cost)}
        source = "tokens"
    elif price:
        seconds = getattr(usage, "seconds", None)
        if (
            not isinstance(seconds, int | float)
            or isinstance(seconds, bool)
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            seconds = duration
            estimated = True
        amount = Decimal(str(seconds)) * price["minute"] / Decimal(60)
        parts = {"input": float(amount), "cache_write": 0.0, "cache_read": 0.0, "output": 0.0}
        source = "duration"
    else:
        logger.warning("No transcription price configured for model %s", model)
    metadata = {"transcription": {"cost_source": source, "estimated": estimated, "unpriced": parts is None}}
    if valid_tokens:
        metadata["transcription"].update(audio_tokens=audio_tokens, text_tokens=text_tokens)
    if source == "duration":
        metadata["transcription"]["seconds"] = seconds
    if parts is not None:
        metadata["cost_parts"] = parts
    StructuredCall.objects.create(
        kind="voice_transcription",
        user=user,
        session=session,
        status=StructuredCallStatus.COMPLETED,
        engine_type="openai",
        model_name=model,
        input_tokens=input_tokens if valid_tokens else 0,
        output_tokens=output_tokens if valid_tokens else 0,
        cost_usd=Decimal(str(sum(parts.values()))) if parts is not None else None,
        metadata=metadata,
    )


def _charge(user):
    # Lock the user row even for the first request, so creation and both counters
    # are serialized across all application processes and sessions.
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        row, _ = VoiceRateLimit.objects.get_or_create(user=user)
        now = int(time.time())
        minute, day = now // 60, now // 86400
        reset = False
        if row.minute != minute:
            row.minute, row.minute_count = minute, 0
            reset = True
        if row.day != day:
            row.day, row.day_count = day, 0
            reset = True
        if (
            row.minute_count >= settings.VOICE_TRANSCRIPTION_PER_MINUTE
            or row.day_count >= settings.VOICE_TRANSCRIPTION_PER_DAY
        ):
            if reset:
                row.save(update_fields=["minute", "minute_count", "day", "day_count"])
            return False
        row.minute_count += 1
        row.day_count += 1
        row.save(update_fields=["minute", "minute_count", "day", "day_count"])
        return True


def _format(data, content_type):
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in ("audio/webm", "video/webm") and data.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska", "audio/webm", "audio.webm"
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
            timeout=60,
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


def _chunks(data, demuxer, duration):
    """Transcode independently decodable, overlapping chunks without writing audio to disk."""
    chunks = []
    start = 0
    while start < duration:
        length = min(CHUNK_SECONDS, duration - start)
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-protocol_whitelist",
                    "pipe",
                    "-f",
                    demuxer,
                    "-i",
                    "pipe:0",
                    "-ss",
                    str(start),
                    "-t",
                    str(length),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "32k",
                    "-f",
                    "mp3",
                    "pipe:1",
                ],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=True,
            )
        except FileNotFoundError:
            raise HttpError(503, "Audio processing is unavailable") from None
        except subprocess.SubprocessError:
            raise HttpError(400, "Could not process audio") from None
        if not result.stdout or len(result.stdout) > PROVIDER_MAX_BYTES:
            raise HttpError(400, "Could not process audio")
        chunks.append((result.stdout, "audio/mpeg", "audio.mp3", length))
        if start + length >= duration:
            break
        start += CHUNK_SECONDS - CHUNK_OVERLAP
    return chunks


def _join_transcripts(texts):
    """Remove only matching multi-word overlaps; retain uncertain boundary words."""
    result = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        previous = list(re.finditer(r"\S+", result))
        current = list(re.finditer(r"\S+", text))
        for count in range(min(len(previous), len(current), 30), 1, -1):
            left = [m.group().strip(".,!?;:\"'()[]{}").casefold() for m in previous[-count:]]
            right = [m.group().strip(".,!?;:\"'()[]{}").casefold() for m in current[:count]]
            if left == right and all(left):
                text = text[current[count - 1].end() :].lstrip()
                break
        else:
            # A provider can inflect one overlapping word differently. Require
            # four exact words and a shared stem before preferring the newer
            # rendition; shorter or less certain matches retain both sides.
            right = [m.group().strip(".,!?;:\"'()[]{}").casefold() for m in current]
            offset = 1 if right and right[0] == "and" else 0
            for count in range(min(len(previous), len(current) - offset, 30), 4, -1):
                left = [m.group().strip(".,!?;:\"'()[]{}").casefold() for m in previous[-count:]]
                pairs = list(zip(left, right[offset : offset + count]))
                changed = [(a, b) for a, b in pairs if a != b]
                if len(changed) != 1 or not all(a and b for a, b in pairs):
                    continue
                a, b = changed[0]
                stem = 0
                while stem < min(len(a), len(b)) and a[stem] == b[stem]:
                    stem += 1
                endings = {"", "s", "d", "ed", "es", "ing"}
                if stem >= 5 and a[stem:] in endings and b[stem:] in endings:
                    result = result[: previous[-count].start()].rstrip()
                    break
        if text:
            result = f"{result} {text}".strip()
    return result


@router.get("/sessions/{session_id}/voice")
def voice_config(request, session_id: str):
    _authorize(request, session_id)
    return {
        "enabled": _enabled(),
        "max_duration": settings.VOICE_TRANSCRIPTION_MAX_DURATION,
    }


@router.post("/sessions/{session_id}/voice")
def voice_transcribe(request, session_id: str):
    session = _authorize(request, session_id)
    if not _enabled():
        raise HttpError(404, "Voice transcription is unavailable")
    try:
        allowed = _charge(request.auth)
    except DatabaseError:
        raise HttpError(503, "Voice transcription rate limiting is unavailable") from None
    if not allowed:
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

        if duration <= CHUNK_SECONDS and len(data) <= PROVIDER_MAX_BYTES:
            chunks = [(data, media_type, filename, duration)]
        else:
            chunks = _chunks(data, demuxer, duration)

        async def transcribe():
            semaphore = asyncio.Semaphore(CHUNK_CONCURRENCY)

            async def one(chunk):
                async with semaphore:
                    return await asyncio.wait_for(
                        openai_transcription(
                            *chunk[:3],
                            model=settings.VOICE_TRANSCRIPTION_MODEL,
                            request_timeout=settings.VOICE_TRANSCRIPTION_TIMEOUT,
                        ),
                        timeout=settings.VOICE_TRANSCRIPTION_TIMEOUT,
                    )

            return await asyncio.gather(*(one(chunk) for chunk in chunks), return_exceptions=True)

        responses = async_to_sync(transcribe)()
        # Account for every successful provider call, even if another chunk failed.
        for chunk, response in zip(chunks, responses):
            if not isinstance(response, BaseException):
                _record_transcription(request.auth, session, settings.VOICE_TRANSCRIPTION_MODEL, response, chunk[3])
        if any(isinstance(response, openai.RateLimitError) for response in responses):
            raise HttpError(429, "Transcription provider rate limit exceeded")
        if any(
            isinstance(response, BaseException) or not isinstance(getattr(response, "text", None), str)
            for response in responses
        ):
            # Never log provider exceptions: they may contain request details.
            raise HttpError(502, "Voice transcription failed")
        return {"text": _join_transcripts([response.text for response in responses])}
    finally:
        audio.close()
