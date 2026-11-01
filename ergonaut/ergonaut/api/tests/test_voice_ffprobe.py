import logging
import shutil
import subprocess

import pytest
from django.core.checks import run_checks

from ergonaut.api.voice import _duration, _format


@pytest.mark.parametrize(
    ("container", "content_type", "demuxer"),
    [("webm", "audio/webm", "matroska"), ("ogg", "audio/ogg", "ogg")],
)
def test_generated_browser_audio_duration(container, content_type, demuxer):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for real audio probe tests")
    # Streaming output has no seekable header to backfill duration, like a
    # MediaRecorder upload. Probe the real Opus packets rather than mocking it.
    audio = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "libopus",
            "-f",
            container,
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    detected, _, _ = _format(audio, content_type)
    assert detected == demuxer
    assert 0.9 <= _duration(audio, detected) <= 1.1


@pytest.mark.parametrize("enabled,found,warning", [(False, False, False), (True, True, False), (True, False, True)])
def test_ffprobe_system_check(settings, monkeypatch, enabled, found, warning):
    settings.VOICE_TRANSCRIPTION_ENABLED = enabled
    monkeypatch.setattr("ergonaut.apps.bots.checks.shutil.which", lambda _: "/usr/bin/ffprobe" if found else None)
    messages = run_checks()
    matches = [message for message in messages if message.id == "ergonaut_bots.W001"]
    assert bool(matches) is warning
    if warning:
        assert "ffprobe" in matches[0].msg


def test_ffprobe_startup_warning(settings, monkeypatch, caplog):
    from ergonaut.apps.bots.checks import warn_if_voice_ffprobe_missing

    settings.VOICE_TRANSCRIPTION_ENABLED = True
    monkeypatch.setattr("ergonaut.apps.bots.checks.shutil.which", lambda _: None)
    previous_disable = logging.root.manager.disable
    try:
        logging.disable(logging.NOTSET)
        warn_if_voice_ffprobe_missing()
        assert "ffprobe was not found" in caplog.text
    finally:
        logging.disable(previous_disable)
