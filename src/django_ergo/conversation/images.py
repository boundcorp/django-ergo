"""Images in tool results, and how many images each model call carries.

A tool can return images along with its text::

    from django_ergo.conversation.images import ToolImage, ToolResult

    @bot_tool
    def sales_chart(days: int = 7) -> ToolResult:
        png = render_chart(days)
        return ToolResult(f"Sales, last {days} days", [ToolImage(png, name="sales.png")])

Both engines send the images to the model with the tool result. Claude gets
``image`` blocks inside the ``tool_result``. OpenAI Chat Completions tool
messages can't hold images, so the tool message gets the text and a user
message with the image parts follows the tool messages.

History keeps references, not bytes: in a session, image bytes are saved as a
session file (source ``bot``) and the stored tool result holds an
``image_ref`` item naming that file. ``ToolImage.from_attachment(row)``
references a file the session already has. Images sent with user messages
are referenced the same way.

Each model call carries only the latest ``DJANGO_ERGO["IMAGES_IN_CONTEXT"]``
images (default 2). Older ones become ``[image omitted: name (id=...)]``, so
the model can look again if it needs to. Images are downscaled to
``DJANGO_ERGO["IMAGE_MAX_SIDE"]`` pixels (default 1024) on the long side when
Pillow is installed; without Pillow they are sent as they are, up to 5 MB.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

IMAGE_REF = "image_ref"
# Media types both APIs accept as images.
SENDABLE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Without Pillow, larger images are left out rather than sent whole.
MAX_RAW_BYTES = 5_000_000
JPEG_QUALITY = 85
_CACHE_SIZE = 32
_prepared_cache: OrderedDict[tuple, tuple[bytes, str] | None] = OrderedDict()


@dataclass
class ToolImage:
    """An image for a tool result: bytes, a stored attachment, or a URL."""

    data: bytes | None = None
    media_type: str = "image/png"
    name: str = ""
    attachment_id: str = ""
    url: str = ""

    def __post_init__(self):
        if not (self.data or self.attachment_id or self.url):
            msg = "ToolImage needs data, an attachment_id or a url"
            raise ValueError(msg)

    @classmethod
    def from_attachment(cls, row) -> ToolImage:
        """Reference a ConversationAttachment (no bytes are copied)."""
        return cls(
            media_type=row.media_type,
            name=row.filename,
            attachment_id=str(row.id),
            url=row.url or "",
        )

    @classmethod
    def from_path(cls, path: str | Path, media_type: str | None = None) -> ToolImage:
        path = Path(path)
        return cls(
            data=path.read_bytes(),
            media_type=media_type or mimetypes.guess_type(path.name)[0] or "image/png",
            name=path.name,
        )


@dataclass
class ToolResult:
    """A tool result with images. ``str()`` gives the text alone."""

    text: str = ""
    images: list[ToolImage] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text


def as_tool_result(value: Any) -> Any:
    """A bare ToolImage (or list of them) becomes a ToolResult; anything else is unchanged."""
    if isinstance(value, ToolImage):
        return ToolResult(images=[value])
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, ToolImage) for item in value)
    ):
        return ToolResult(images=list(value))
    return value


def has_images(result: Any) -> bool:
    return isinstance(result, ToolResult) and bool(result.images)


# -- references ---------------------------------------------------------------


def image_ref(
    *,
    name: str = "",
    media_type: str = "",
    attachment_id: str = "",
    url: str = "",
    data: bytes | None = None,
) -> dict:
    """An ``image_ref`` item: how history points at an image.

    ``text`` is what readers that don't know images show. ``data`` (base64)
    is only ever held in memory; ``storable`` drops it.
    """
    label = name or url or attachment_id or "image"
    ref: dict = {
        "type": IMAGE_REF,
        "name": name,
        "media_type": media_type,
        "text": f"[image: {label}]",
    }
    if attachment_id:
        ref["attachment_id"] = attachment_id
    if url:
        ref["url"] = url
    if data:
        ref["data"] = base64.b64encode(data).decode("ascii")
    return ref


def is_ref(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == IMAGE_REF


def attachment_ref(row) -> dict:
    """A reference to an image sent with a user message (a row or an in-memory Attachment)."""
    row_id = getattr(row, "pk", None)
    has_file = bool(getattr(row, "file", None))
    return image_ref(
        name=row.filename,
        media_type=row.media_type,
        attachment_id=str(row_id) if row_id and has_file else "",
        url=row.url or "",
        data=getattr(row, "data", None),
    )


def _inline_ref(image: ToolImage) -> dict:
    return image_ref(
        name=image.name,
        media_type=image.media_type,
        attachment_id=image.attachment_id,
        url=image.url,
        data=None if image.attachment_id else image.data,
    )


def result_content(result: Any, refs: list[dict]) -> list[dict]:
    """Tool result content: the text (when there is any), then the image references."""
    text = str(result)
    return [*([{"type": "text", "text": text}] if text else []), *refs]


def memory_result(result: Any) -> Any:
    """A tool result for an in-memory transcript: a string, or text plus inline refs."""
    if not has_images(result):
        return str(result)
    return result_content(result, [_inline_ref(image) for image in result.images])


def store_images(session, images: list[ToolImage]) -> list[dict]:
    """References for tool images; bytes are saved as files in ``session``.

    Runs the ORM, so call it from sync code (or through sync_to_async).
    """
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationSession

    refs = []
    for image in images:
        if image.attachment_id or not isinstance(session, ConversationSession):
            refs.append(_inline_ref(image))
            continue
        extension = mimetypes.guess_extension(image.media_type) or ""
        row = save_session_file(
            session,
            image.name or f"tool-image-{uuid.uuid4().hex[:8]}{extension}",
            image.data or b"",
            media_type=image.media_type,
            source="bot",
            metadata={"tool_image": True},
        )
        refs.append(
            image_ref(
                name=row.filename,
                media_type=row.media_type,
                attachment_id=str(row.id),
            )
        )
    return refs


async def stored_result(session, result: Any) -> tuple[str, list[dict]]:
    """(text, image refs) for persisting a tool result in a session."""
    if not has_images(result):
        return str(result), []
    from asgiref.sync import sync_to_async

    refs = await sync_to_async(store_images, thread_sensitive=True)(
        session, result.images
    )
    return str(result), refs


def storable_ref(ref: dict) -> dict:
    """A reference fit for a JSON field: bytes are replaced by a note."""
    if not ref.get("data"):
        return ref
    if ref.get("attachment_id") or ref.get("url"):
        return {k: v for k, v in ref.items() if k != "data"}
    name = ref.get("name")
    label = f"image attachment {name}" if name else "image attachment"
    return {"type": "text", "text": f"[{label}, not stored]"}


# -- what a model call carries ---------------------------------------------------


def _iter_refs(messages: list[dict]) -> Iterator[dict]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if is_ref(block):
                yield block
            elif isinstance(block, dict) and isinstance(block.get("content"), list):
                yield from (b for b in block["content"] if is_ref(b))


def prepare_messages(
    messages: list[dict], engine_type: str, *, keep: int | None = None
) -> list[dict]:
    """Turn image references into engine image parts for one model call.

    Only the latest ``keep`` images are sent; older ones become a short text
    placeholder. Loads stored files, so call it from sync code. The input
    list is not changed.
    """
    refs = list(_iter_refs(messages))
    if not refs and engine_type != "openai":
        return messages
    keep = api_settings.IMAGES_IN_CONTEXT if keep is None else keep
    shown = {id(ref) for ref in refs[-keep:]} if keep > 0 else set()
    if engine_type == "openai":
        return _openai_messages(messages, shown)
    return _claude_messages(messages, shown)


def _label(ref: dict) -> str:
    name = ref.get("name") or ref.get("url") or "image"
    if ref.get("attachment_id"):
        return f"{name} (id={ref['attachment_id']})"
    return name


def omitted(ref: dict) -> str:
    return f"[image omitted: {_label(ref)}]"


def _claude_part(ref: dict, shown: set[int]) -> dict:
    if id(ref) not in shown:
        return {"type": "text", "text": omitted(ref)}
    loaded = load_image(ref)
    if loaded is None:
        return {"type": "text", "text": f"[image unavailable: {_label(ref)}]"}
    media_type, data, url = loaded
    if url:
        return {"type": "image", "source": {"type": "url", "url": url}}
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _claude_messages(messages: list[dict], shown: set[int]) -> list[dict]:
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        blocks = []
        for block in content:
            if is_ref(block):
                blocks.append(_claude_part(block, shown))
            elif isinstance(block, dict) and isinstance(block.get("content"), list):
                blocks.append(
                    {
                        **block,
                        "content": [
                            _claude_part(b, shown) if is_ref(b) else b
                            for b in block["content"]
                        ],
                    }
                )
            else:
                blocks.append(block)
        out.append({**message, "content": blocks})
    return out


def _openai_part(ref: dict, shown: set[int]) -> dict:
    if id(ref) not in shown:
        return {"type": "text", "text": omitted(ref)}
    loaded = load_image(ref)
    if loaded is None:
        return {"type": "text", "text": f"[image unavailable: {_label(ref)}]"}
    media_type, data, url = loaded
    return {
        "type": "image_url",
        "image_url": {"url": url or f"data:{media_type};base64,{data}"},
    }


def _openai_messages(messages: list[dict], shown: set[int]) -> list[dict]:
    out: list[dict] = []
    pending: list[dict] = []  # image parts from the current run of tool messages

    def flush():
        if pending:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Images from the tool results above:"},
                        *pending,
                    ],
                }
            )
            pending.clear()

    for message in messages:
        if message.get("role") != "tool":
            flush()
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        if message.get("role") == "tool":
            # Tool messages hold text only; their images follow in a user message.
            texts = []
            for block in content:
                if not is_ref(block):
                    texts.append(str(block.get("text", "")))
                    continue
                part = _openai_part(block, shown)
                if part["type"] == "image_url":
                    texts.append(f"[image: {_label(block)}, shown below]")
                    pending.extend(
                        [{"type": "text", "text": f"[image: {_label(block)}]"}, part]
                    )
                else:
                    texts.append(part["text"])
            out.append({**message, "content": "\n".join(t for t in texts if t)})
        else:
            out.append(
                {
                    **message,
                    "content": [
                        _openai_part(b, shown) if is_ref(b) else b for b in content
                    ],
                }
            )
    flush()
    return out


# -- loading and downscaling -------------------------------------------------------


def load_image(ref: dict) -> tuple[str, str, str] | None:
    """(media_type, base64 data, url) for a reference; None when it can't be sent.

    A reference with only a URL comes back as a URL.
    """
    try:
        if ref.get("data"):
            prepared = prepare_image(
                base64.b64decode(ref["data"]), ref.get("media_type") or ""
            )
        elif ref.get("attachment_id"):
            prepared = _prepared_attachment(ref["attachment_id"])
            if prepared is None and ref.get("url"):
                return "", "", ref["url"]
        elif ref.get("url"):
            return "", "", ref["url"]
        else:
            return None
    except Exception:  # a missing image never breaks the call
        log.warning("could not load image %s", _label(ref), exc_info=True)
        return None
    if prepared is None:
        return None
    data, media_type = prepared
    return media_type, base64.b64encode(data).decode("ascii"), ""


def _prepared_attachment(attachment_id: str) -> tuple[bytes, str] | None:
    from django_ergo.conversation.models import ConversationAttachment

    row = ConversationAttachment.objects.filter(id=attachment_id).first()
    if row is None or not row.file:
        return None
    key = (attachment_id, row.sha256, row.file.name, api_settings.IMAGE_MAX_SIDE)
    if key in _prepared_cache:
        _prepared_cache.move_to_end(key)
        return _prepared_cache[key]
    with row.file.open("rb") as handle:
        prepared = prepare_image(handle.read(), row.media_type)
    _prepared_cache[key] = prepared
    while len(_prepared_cache) > _CACHE_SIZE:
        _prepared_cache.popitem(last=False)
    return prepared


def prepare_image(
    data: bytes, media_type: str, max_side: int | None = None
) -> tuple[bytes, str] | None:
    """Downscale to ``max_side`` on the long side (Pillow) and return (bytes, media_type).

    Images Pillow can't read, or any image without Pillow, go as they are when
    their type is one the APIs accept and they're under 5 MB; otherwise None.
    """
    max_side = max_side or api_settings.IMAGE_MAX_SIDE
    try:
        from PIL import Image
    except ImportError:
        Image = None  # noqa: N806
    if Image is not None:
        try:
            return _resize(Image, data, media_type, max_side)
        except Exception:  # not an image Pillow can read
            log.debug("Pillow could not read a %s image", media_type, exc_info=True)
    if media_type not in SENDABLE_TYPES or len(data) > MAX_RAW_BYTES:
        return None
    return data, media_type


def _resize(Image, data: bytes, media_type: str, max_side: int):  # noqa: N803
    with Image.open(BytesIO(data)) as image:
        if max(image.size) <= max_side and media_type in SENDABLE_TYPES:
            return data, media_type
        image.seek(0)
        frame = image.copy()
    frame.thumbnail((max_side, max_side))
    out = BytesIO()
    if frame.mode in {"RGBA", "LA", "P", "PA"}:
        frame.convert("RGBA").save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    frame.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue(), "image/jpeg"
