"""Image, audio and document attachments on user messages.

Callers pass ``Attachment`` objects alongside a message::

    await engine.send(session, "What's in this photo?",
                      attachments=[Attachment.from_path("fridge.jpg")])

They are stored as ``ConversationAttachment`` rows keyed by the message's
sequence and turned into engine-native content parts when context is rebuilt:

| kind     | Claude                         | OpenAI (Chat Completions)            |
| -------- | ------------------------------ | ------------------------------------ |
| image    | ``image`` block                | ``image_url`` part                   |
| document | ``document`` block (PDF/text)  | ``file`` part                        |
| audio    | transcript text                | ``input_audio`` part when the engine |
|          |                                | config sets ``audio_input``, else    |
|          |                                | transcript text                      |

Audio is transcribed on save when ``DJANGO_ERGO["AUDIO_TRANSCRIBER"]`` is set
and no transcript was given.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile

from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationAttachment
    from django_ergo.conversation.models import ConversationSession

log = logging.getLogger(__name__)

# input_audio accepts these formats.
_OPENAI_AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
}


def kind_for_media_type(media_type: str) -> str:
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    return "document"


@dataclass
class Attachment:
    """An attachment to send with a message: bytes or a URL, plus its media type."""

    media_type: str
    data: bytes | None = None
    url: str = ""
    filename: str = ""
    transcript: str = ""
    kind: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.data and not self.url:
            msg = "Attachment needs data or a url"
            raise ValueError(msg)
        self.kind = self.kind or kind_for_media_type(self.media_type)

    @classmethod
    def from_path(cls, path: str | Path, media_type: str | None = None, **kwargs):
        path = Path(path)
        guessed = media_type or mimetypes.guess_type(path.name)[0]
        return cls(
            media_type=guessed or "application/octet-stream",
            data=path.read_bytes(),
            filename=path.name,
            **kwargs,
        )


async def openai_transcriber(data: bytes, media_type: str, filename: str) -> str:
    """Transcribe audio with OpenAI's transcription API (whisper-1)."""
    import openai

    client = openai.AsyncOpenAI()
    response = await client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename or "audio", data, media_type),
    )
    return response.text


async def save_attachments(
    session: ConversationSession,
    message_sequence: int,
    attachments: list[Attachment],
) -> list[ConversationAttachment]:
    """Store attachments for the user message at ``message_sequence``."""
    from django_ergo.conversation.models import ConversationAttachment

    transcriber = api_settings.AUDIO_TRANSCRIBER
    saved = []
    for position, attachment in enumerate(attachments):
        transcript = attachment.transcript
        if (
            attachment.kind == "audio"
            and not transcript
            and attachment.data
            and transcriber is not None
        ):
            try:
                transcript = await transcriber(
                    attachment.data, attachment.media_type, attachment.filename
                )
            except Exception:
                log.exception("audio transcription failed for %s", attachment.filename)
        row = ConversationAttachment(
            session=session,
            message_sequence=message_sequence,
            position=position,
            kind=attachment.kind,
            media_type=attachment.media_type,
            url=attachment.url,
            filename=attachment.filename,
            transcript=transcript or "",
            metadata=attachment.metadata,
        )
        if attachment.data:
            row.size = len(attachment.data)
            row.sha256 = hashlib.sha256(attachment.data).hexdigest()
            name = attachment.filename or f"{row.id}"
            row.file.save(name, ContentFile(attachment.data), save=False)
        await row.asave()
        saved.append(row)
    return saved


def attachments_by_sequence(session) -> dict[int, list[ConversationAttachment]]:
    """Map message sequence -> attachments, for sessions that have any."""
    from django_ergo.conversation.models import ConversationSession

    if not isinstance(session, ConversationSession):
        return {}
    grouped: dict[int, list] = {}
    for row in session.attachments.all():
        grouped.setdefault(row.message_sequence, []).append(row)
    return grouped


def _read_b64(row: ConversationAttachment) -> str:
    with row.file.open("rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def audio_placeholder(row: ConversationAttachment) -> str:
    label = row.filename or row.media_type
    if row.transcript:
        return f"[Audio attachment {label}, transcript]: {row.transcript}"
    return f"[Audio attachment {label}: no transcript available]"


def claude_block(row: ConversationAttachment) -> dict:
    """Engine-native Claude content block for an attachment."""
    if row.kind == "audio":
        return {"type": "text", "text": audio_placeholder(row)}
    block_type = "image" if row.kind == "image" else "document"
    if row.file:
        source = {
            "type": "base64",
            "media_type": row.media_type,
            "data": _read_b64(row),
        }
    else:
        source = {"type": "url", "url": row.url}
    return {"type": block_type, "source": source}


def openai_part(row: ConversationAttachment, *, audio_input: bool = False) -> dict:
    """Chat Completions content part for an attachment."""
    if row.kind == "image":
        url = f"data:{row.media_type};base64,{_read_b64(row)}" if row.file else row.url
        return {"type": "image_url", "image_url": {"url": url}}
    if row.kind == "audio":
        audio_format = _OPENAI_AUDIO_FORMATS.get(row.media_type)
        if audio_input and row.file and audio_format:
            return {
                "type": "input_audio",
                "input_audio": {"data": _read_b64(row), "format": audio_format},
            }
        return {"type": "text", "text": audio_placeholder(row)}
    if row.file:
        return {
            "type": "file",
            "file": {
                "filename": row.filename or "document",
                "file_data": f"data:{row.media_type};base64,{_read_b64(row)}",
            },
        }
    return {"type": "text", "text": f"[Document attachment: {row.url}]"}


def describe(row: ConversationAttachment) -> str:
    """Short text stand-in used by renderers and history tools."""
    label = row.filename or row.url or row.media_type
    if row.kind == "audio":
        return audio_placeholder(row)
    return f"[{row.kind} attachment: {label} ({row.media_type})]"
