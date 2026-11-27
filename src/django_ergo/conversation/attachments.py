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

A document the model can't read (not a PDF or text, e.g. a video) goes as a
text stand-in with its filename and file id, so the model knows it arrived
and can hand it to a tool.

Audio is transcribed on save when ``DJANGO_ERGO["AUDIO_TRANSCRIBER"]`` is set
and no transcript was given.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import NoReturn

from django.core.files.base import ContentFile
from django.core.files.base import File

from django_ergo.conversation.inference_policy import assert_public_endpoint
from django_ergo.conversation.inference_policy import effective_openai_base_url
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


async def openai_transcriber(
    data: bytes,
    media_type: str,
    filename: str,
    *,
    model: str = "gpt-transcribe",
    request_timeout: float | None = None,
) -> str:
    """Transcribe audio, returning text for attachment callers."""
    response = await openai_transcription(
        data, media_type, filename, model=model, request_timeout=request_timeout
    )
    return response.text


async def openai_transcription(
    data: bytes,
    media_type: str,
    filename: str,
    *,
    model: str = "gpt-transcribe",
    request_timeout: float | None = None,
):
    """Return the OpenAI JSON response, including any provider usage."""
    import openai

    from django_ergo.conversation.spend_guard import check_unguarded_dispatch

    await check_unguarded_dispatch()
    # AsyncOpenAI() falls back to OPENAI_BASE_URL: refuse a private endpoint.
    what = "The audio transcriber"
    assert_public_endpoint(effective_openai_base_url(), what=what)
    options = (
        {"timeout": request_timeout, "max_retries": 0}
        if request_timeout is not None
        else {}
    )
    client = openai.AsyncOpenAI(**options)
    assert_public_endpoint(getattr(client, "base_url", None), what=what)
    return await client.audio.transcriptions.create(
        model=model,
        file=(filename or "audio", data, media_type),
        response_format="json",
    )


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


def _has_bytes(row: ConversationAttachment | Attachment) -> bool:
    return bool(getattr(row, "data", None) or getattr(row, "file", None))


def _read_b64(row: ConversationAttachment | Attachment) -> str:
    """Base64 of a stored attachment row or an in-memory Attachment."""
    if data := getattr(row, "data", None):
        return base64.b64encode(data).decode("ascii")
    with row.file.open("rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def audio_placeholder(row: ConversationAttachment | Attachment) -> str:
    label = row.filename or row.media_type
    if row.transcript:
        return f"[Audio attachment {label}, transcript]: {row.transcript}"
    return f"[Audio attachment {label}: no transcript available]"


def model_readable(row: ConversationAttachment | Attachment) -> bool:
    """Whether the engines can send this file to the model: an image, a PDF or
    text. Anything else (a video, a zip) goes as a text stand-in."""
    return row.kind != "document" or (
        row.media_type == "application/pdf" or is_text(row.media_type)
    )


def unreadable_placeholder(row: ConversationAttachment | Attachment) -> str:
    """Names a file the model can't read, with its id so tools can still use it."""
    label = row.filename or row.url or row.media_type
    file_id = getattr(row, "id", None)
    ref = f", file id {file_id}" if file_id else ""
    return (
        f"[{row.kind.capitalize()} attachment {label} ({row.media_type}){ref}. "
        "You can't open this file type yourself, but tools that take a file "
        "can use it by id or filename.]"
    )


def claude_block(row: ConversationAttachment | Attachment) -> dict:
    """Engine-native Claude content block for an attachment."""
    if row.kind == "audio":
        return {"type": "text", "text": audio_placeholder(row)}
    if not model_readable(row):
        return {"type": "text", "text": unreadable_placeholder(row)}
    # Images never come here: engines send them as references (see ``images``),
    # downscaled and windowed.
    if _has_bytes(row):
        source = {
            "type": "base64",
            "media_type": row.media_type,
            "data": _read_b64(row),
        }
    else:
        source = {"type": "url", "url": row.url}
    return {"type": "document", "source": source}


def openai_part(
    row: ConversationAttachment | Attachment, *, audio_input: bool = False
) -> dict:
    """Chat Completions content part for an attachment (images go as references)."""
    if row.kind == "audio":
        audio_format = _OPENAI_AUDIO_FORMATS.get(row.media_type)
        if audio_input and _has_bytes(row) and audio_format:
            return {
                "type": "input_audio",
                "input_audio": {"data": _read_b64(row), "format": audio_format},
            }
        return {"type": "text", "text": audio_placeholder(row)}
    if not model_readable(row):
        return {"type": "text", "text": unreadable_placeholder(row)}
    if _has_bytes(row):
        return {
            "type": "file",
            "file": {
                "filename": row.filename or "document",
                "file_data": f"data:{row.media_type};base64,{_read_b64(row)}",
            },
        }
    return {"type": "text", "text": f"[Document attachment: {row.url}]"}


def describe(row: ConversationAttachment | Attachment) -> str:
    """Short text stand-in used by renderers and history tools."""
    label = row.filename or row.url or row.media_type
    if row.kind == "audio":
        return audio_placeholder(row)
    return f"[{row.kind} attachment: {label} ({row.media_type})]"


# -- session files ------------------------------------------------------------

TEXT_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-sh",
    "application/sql",
    "application/toml",
}


def guess_media_type(filename: str, default: str = "application/octet-stream") -> str:
    if Path(filename).suffix.lower() == ".jhtml":
        return "text/x-jhtml"  # a live page (django_ergo.bots.pages)
    guessed = mimetypes.guess_type(filename)[0]
    if guessed:
        return guessed
    if Path(filename).suffix.lower() in {
        ".md",
        ".markdown",
        ".txt",
        ".log",
        ".yaml",
        ".yml",
        ".toml",
        ".csv",
    }:
        return (
            "text/markdown"
            if filename.lower().endswith((".md", ".markdown"))
            else "text/plain"
        )
    return default


def check_snapshot_access(row, session, *, transfer=False) -> None:
    """Snapshots are readable only here and may not leave through file tools."""
    if row.private_export_id and (transfer or row.session_id != session.pk):
        msg = "Private export snapshots are destination-only and cannot be forwarded."
        raise ValueError(msg)


def is_text(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in TEXT_MEDIA_TYPES


def save_session_file(  # noqa: PLR0913
    session: ConversationSession,
    filename: str,
    data: bytes | File,
    *,
    media_type: str = "",
    source: str = "upload",
    metadata: dict | None = None,
) -> ConversationAttachment:
    """Store a file in a session, not tied to any message. ``data`` may be a
    Django ``File`` (an upload), which is streamed rather than read whole."""
    from django_ergo.conversation.models import ConversationAttachment

    media_type = media_type or guess_media_type(filename)
    row = ConversationAttachment(
        session=session,
        message_sequence=None,
        kind=kind_for_media_type(media_type),
        media_type=media_type,
        filename=Path(filename).name or "file",
        source=source,
        metadata=metadata or {},
    )
    _store(row, data)
    row.save()
    return row


def replace_session_file(
    row: ConversationAttachment, data: bytes
) -> ConversationAttachment:
    """Overwrite a file's bytes, keeping its id; counts versions in metadata."""
    if row.private_export_id:
        msg = "Private export snapshots are immutable; create a new file instead."
        raise ValueError(msg)
    old = row.file.name if row.file else ""
    _store(row, data)
    row.metadata = {
        **(row.metadata or {}),
        "version": int((row.metadata or {}).get("version", 1)) + 1,
    }
    row.save()
    if old and old != row.file.name:
        row.file.storage.delete(old)
    return row


# -- guarded text edits ---------------------------------------------------------

APPEND_KEYS_KEPT = 100
PREVIEW_LINES = 20
PREVIEW_LINE_CHARS = 200


def _refuse(message: str, category: str, **fields) -> NoReturn:
    """Raise a categorized ``ToolError``; nothing was changed."""
    from django_ergo.conversation.tool_dispatch import ToolError

    raise ToolError(message, category=category, **fields)


def _locked_text(session_id, attachment_id, *, max_bytes):
    """The row (locked ``FOR UPDATE``) and its decoded text. Must run inside
    ``transaction.atomic()``. Refuses anything it could not rewrite whole."""
    from django_ergo.conversation.models import ConversationAttachment

    row = (
        ConversationAttachment.objects.select_for_update()
        .filter(pk=attachment_id, session_id=session_id)
        .first()
    )
    if row is None:
        _refuse(f"No file {attachment_id}", "not_found")
    if row.private_export_id:
        _refuse(
            "Private export snapshots are immutable; create a new file instead.",
            "permission_denied",
        )
    if not is_text(row.media_type) or not row.file:
        _refuse(f"{row.filename} is not a text file", "invalid_argument")
    if (row.size or 0) > max_bytes:
        _refuse(
            f"{row.filename} is {row.size} bytes, over the {max_bytes}-byte "
            "limit for edits; nothing was changed. Use ergo_attachments_create "
            "for a smaller file.",
            "limit_exceeded",
        )
    with row.file.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        _refuse(
            f"{row.filename} is over the {max_bytes}-byte limit for edits; "
            "nothing was changed.",
            "limit_exceeded",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(
            f"{row.filename} is not valid UTF-8; nothing was changed.",
            "invalid_argument",
        )
    return row, data, text


def _check_precondition(row, data, expected_sha256, expected_version):
    """Compare against the bytes just read under the lock, not the stored hash."""
    actual = hashlib.sha256(data).hexdigest()
    version = int((row.metadata or {}).get("version", 1))
    expected = (expected_sha256 or "").strip().lower()
    if expected and len(expected) < 12:  # noqa: PLR2004
        _refuse(
            "expected_sha256 must be at least 12 hex characters", "invalid_argument"
        )
    stale = (expected and not actual.startswith(expected)) or (
        expected_version is not None and int(expected_version) != version
    )
    if stale:
        _refuse(
            f"{row.filename} changed since you read it; nothing was changed.",
            "stale_version",
            next_action="Re-read the file (or the range) and retry with its current sha256.",
            current_sha256=actual,
            current_version=version,
        )
    return actual, version


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _write_locked(row, data: bytes, version: int):
    """Store ``data`` on the locked row. The old file goes only after commit."""
    from django.db import transaction

    old = row.file.name if row.file else ""
    row.file.save(row.filename or f"{row.id}", ContentFile(data), save=False)
    new = row.file.name
    storage = row.file.storage
    row.size = len(data)
    row.sha256 = hashlib.sha256(data).hexdigest()
    row.metadata = {**(row.metadata or {}), "version": version}
    try:
        row.save(update_fields=["file", "size", "sha256", "metadata", "updated_at"])
    except BaseException:
        storage.delete(new)
        raise
    if old and old != new:
        transaction.on_commit(lambda: storage.delete(old))


def _receipt(row, previous_sha, new_text, first_line, last_line, **extra):
    lines = new_text.splitlines()
    shown = [
        f"{n}: {lines[n - 1][:PREVIEW_LINE_CHARS]}"
        for n in range(first_line, min(last_line, first_line + PREVIEW_LINES - 1) + 1)
        if n <= len(lines)
    ]
    return {
        "id": str(row.id),
        "filename": row.filename,
        "version": (row.metadata or {}).get("version", 1),
        "sha256": row.sha256,
        "previous_sha256": previous_sha,
        "changed_lines": f"{first_line}-{last_line}",
        "total_lines": len(lines),
        "preview": shown,
        **extra,
    }


def edit_session_text(  # noqa: PLR0913
    session_id,
    attachment_id,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool = False,
    expected_sha256: str = "",
    expected_version: int | None = None,
    max_bytes: int,
) -> dict:
    """Exact-text replace under a row lock. The precondition is checked against
    the bytes read inside the lock, so two writers cannot both pass it. Changes
    nothing (and says why) on a stale version, a missing or ambiguous match, or a
    result over ``max_bytes``."""
    from django.db import transaction

    if not old_text:
        _refuse("old_text must not be empty", "invalid_argument")
    if not expected_sha256 and expected_version is None:
        _refuse(
            "Give expected_sha256 (or expected_version) from your last read.",
            "invalid_argument",
        )
    with transaction.atomic():
        row, data, text = _locked_text(session_id, attachment_id, max_bytes=max_bytes)
        previous, version = _check_precondition(
            row, data, expected_sha256, expected_version
        )
        count = text.count(old_text)
        if count == 0:
            _refuse(
                "old_text was not found; nothing was changed.",
                "not_found",
                next_action="Re-read the range and copy the text exactly.",
            )
        if count > 1 and not replace_all:
            first = []
            at = -1
            for _ in range(min(count, 3)):
                at = text.index(old_text, at + 1)
                first.append(_line_of(text, at))
            _refuse(
                f"old_text matches {count} places (lines {first}); nothing was changed.",
                "ambiguous_match",
                matches=count,
                first_lines=first,
                next_action="Add surrounding text to make old_text unique, or set replace_all.",
            )
        # Locate every replaced span in the new text, to report what changed.
        pieces = text.split(old_text)
        spans, out_len = [], 0
        out = []
        for position, piece in enumerate(pieces):
            out.append(piece)
            out_len += len(piece)
            if position < len(pieces) - 1:
                spans.append((out_len, out_len + len(new_text)))
                out.append(new_text)
                out_len += len(new_text)
        result = "".join(out)
        encoded = result.encode()
        if len(encoded) > max_bytes:
            _refuse(
                f"Result would be {len(encoded)} bytes; the limit is {max_bytes}. "
                "Nothing was changed.",
                "limit_exceeded",
            )
        first_line = _line_of(result, spans[0][0])
        last_line = _line_of(result, max(spans[-1][1] - 1, spans[-1][0]))
        _write_locked(row, encoded, version + 1)
        return _receipt(
            row, previous, result, first_line, last_line, replacements=len(spans)
        )


def append_session_text(  # noqa: PLR0913
    session_id,
    attachment_id,
    content: str,
    *,
    separator: str = "\n",
    idempotency_key: str = "",
    expected_sha256: str = "",
    expected_version: int | None = None,
    max_bytes: int,
) -> dict:
    """Append under a row lock. ``separator`` goes between the old text and
    ``content`` unless the file is empty or already ends with it. A retry with
    the same ``idempotency_key`` and payload returns the first receipt and writes
    nothing; the same key with a different payload is a conflict. Keys are scoped
    to the file and the last ``APPEND_KEYS_KEPT`` are remembered."""
    from django.db import transaction

    if not content:
        _refuse("content must not be empty", "invalid_argument")
    key = (idempotency_key or "").strip()
    payload = hashlib.sha256(
        json.dumps([content, separator], ensure_ascii=False).encode()
    ).hexdigest()
    with transaction.atomic():
        row, data, text = _locked_text(session_id, attachment_id, max_bytes=max_bytes)
        keys = list((row.metadata or {}).get("append_keys") or [])
        seen = next((k for k in keys if key and k.get("key") == key), None)
        if seen is not None:
            if seen.get("payload_sha256") != payload:
                _refuse(
                    f"idempotency_key {key!r} was already used with different content; "
                    "nothing was changed.",
                    "conflict",
                    next_action="Use a new key for new content.",
                )
            return {**seen["receipt"], "replayed": True}
        previous, version = _check_precondition(
            row, data, expected_sha256, expected_version
        )
        glue = separator if text and not text.endswith(separator) else ""
        result = text + glue + content
        encoded = result.encode()
        if len(encoded) > max_bytes:
            _refuse(
                f"Result would be {len(encoded)} bytes; the limit is {max_bytes}. "
                "Nothing was changed.",
                "limit_exceeded",
            )
        first_line = _line_of(result, len(text + glue))
        _write_locked(row, encoded, version + 1)
        receipt = _receipt(
            row, previous, result, first_line, len(result.splitlines()), appended=True
        )
        if key:
            row.metadata = {
                **row.metadata,
                "append_keys": [
                    *keys,
                    {"key": key, "payload_sha256": payload, "receipt": receipt},
                ][-APPEND_KEYS_KEPT:],
            }
            row.save(update_fields=["metadata"])
        return receipt


def _store(row: ConversationAttachment, data: bytes | File) -> None:
    """Save ``data`` as the row's file. A ``File`` (an upload) is hashed and
    copied in chunks, so a large video never sits in memory whole."""
    if isinstance(data, File):
        digest = hashlib.sha256()
        for chunk in data.chunks():
            digest.update(chunk)
        data.seek(0)
        row.size = data.size
        row.sha256 = digest.hexdigest()
        row.file.save(row.filename or f"{row.id}", data, save=False)
        return
    row.size = len(data)
    row.sha256 = hashlib.sha256(data).hexdigest()
    row.file.save(row.filename or f"{row.id}", ContentFile(data), save=False)


def read_text(row: ConversationAttachment, limit: int = 50_000) -> str:
    """A text file's contents, or a short description for other files."""
    if not is_text(row.media_type) or not row.file:
        text = describe(row)
        if row.transcript:
            text += f"\nTranscript: {row.transcript}"
        return text
    with row.file.open("rb") as handle:
        data = handle.read(limit + 1)
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        text += f"\n[truncated at {limit} characters]"
    return text


def find_session_file(session, ref: str) -> ConversationAttachment:
    """A file in ``session`` by id or by filename (the newest with that name).
    Raises ValueError naming what's there when nothing matches."""
    import uuid

    from django_ergo.conversation.models import ConversationAttachment

    ref = str(ref).strip()
    rows = ConversationAttachment.objects.filter(session=session)
    try:
        found = rows.filter(id=uuid.UUID(ref)).first()
    except ValueError:
        found = rows.filter(filename=ref).order_by("archived_at", "-updated_at").first()
    if found is None:
        names = list(
            rows.filter(archived_at__isnull=True)
            .order_by("-updated_at")
            .values_list("filename", flat=True)[:20]
        )
        listing = ", ".join(names) or "no files"
        msg = f"No file {ref!r} in that chat (it has: {listing})"
        raise ValueError(msg)
    return found
