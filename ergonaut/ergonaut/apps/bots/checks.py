import logging
import shutil

from django.conf import settings
from django.core.checks import Warning, register

logger = logging.getLogger(__name__)


@register()
def voice_ffprobe_check(app_configs, **kwargs):
    if settings.VOICE_TRANSCRIPTION_ENABLED and shutil.which("ffprobe") is None:
        return [
            Warning(
                "Voice transcription is enabled, but ffprobe was not found on PATH.",
                hint="Install ffmpeg (which includes ffprobe) and make ffprobe available on PATH.",
                id="ergonaut_bots.W001",
            )
        ]
    return []


def warn_if_voice_ffprobe_missing():
    for warning in voice_ffprobe_check(None):
        logger.warning("%s %s", warning.msg, warning.hint)
