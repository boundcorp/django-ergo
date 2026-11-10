"""Watching videos: still frames with ffmpeg, and questions answered by Gemini.

The engines (Claude, OpenAI) don't take video, so a bot sees one in two ways:

- **Frames.** ``extract_frames`` pulls JPEG stills at chosen times (or evenly
  spaced ones) for the bot to look at itself. Each frame is its own small
  ffmpeg run that seeks to the timestamp, decodes one frame on one thread and
  scales it down, so memory stays in the tens of MB whatever the video's
  length. ffmpeg runs with the highest OOM score, so if memory runs out
  anyway the kernel kills it, not the worker that started it.
- **Gemini.** ``ask_gemini`` uploads the file to the Gemini Files API
  (streamed from disk, never read whole), waits for it to be processed, asks
  the question with the video (frames and soundtrack) and deletes the upload.
  ``start``/``end`` limit it to part of the video, so a long one needn't be
  cut first. A 30-second ad is roughly 8k tokens at the default sampling of
  one frame a second.

Standard library only (``urllib``); ffmpeg and ffprobe must be on PATH for
frames and probing.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django_ergo.conversation.models import ConversationAttachment

GEMINI_BASE = "https://generativelanguage.googleapis.com"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
FFMPEG_TIMEOUT = 60
PROBE_TIMEOUT = 30
# Raise ffmpeg's OOM score before exec: it, not the worker, goes first.
_OOM_FIRST = 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$@"'
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg"}


class VideoError(Exception):
    """A video couldn't be probed, sampled or sent to Gemini."""


def is_video(media_type: str, filename: str = "") -> bool:
    return (media_type or "").startswith("video/") or (
        Path(filename or "").suffix.lower() in VIDEO_EXTENSIONS
    )


def video_media_type(media_type: str, filename: str = "") -> str:
    """A ``video/*`` type for Gemini, from the stored type or the filename."""
    if (media_type or "").startswith("video/"):
        return media_type
    guessed = mimetypes.guess_type(filename or "")[0] or ""
    return guessed if guessed.startswith("video/") else "video/mp4"


@contextlib.contextmanager
def local_path(row: ConversationAttachment) -> Iterator[Path]:
    """A path on disk for a stored file: the file itself on local storage,
    else a temporary copy written in chunks (never held in memory whole)."""
    if not row.file:
        msg = f"{row.filename} has no stored copy"
        raise VideoError(msg)
    try:
        path = Path(row.file.path)
    except (NotImplementedError, AttributeError, ValueError):
        path = None
    if path is not None and path.exists():
        yield path
        return
    suffix = Path(row.filename or "").suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="ergo-video-") as folder:
        copy = Path(folder) / f"video{suffix}"
        with row.file.open("rb") as source, copy.open("wb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        yield copy


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    if not shutil.which(args[0]):
        msg = f"{args[0]} is not installed on this host"
        raise VideoError(msg)
    try:
        return subprocess.run(  # noqa: S603
            ["sh", "-c", _OOM_FIRST, "sh", *args],  # noqa: S607
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"{args[0]} took longer than {timeout}s"
        raise VideoError(msg) from exc


@dataclass
class VideoInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False

    def describe(self) -> str:
        size = f"{self.width}x{self.height}, " if self.width else ""
        audio = "with sound" if self.has_audio else "no sound"
        return f"{self.duration:.1f}s, {size}{audio}"


def probe(path: Path) -> VideoInfo:
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        PROBE_TIMEOUT,
    )
    if proc.returncode != 0:
        msg = f"Not a readable video: {proc.stderr.decode(errors='replace')[-300:]}"
        raise VideoError(msg)
    data = json.loads(proc.stdout or b"{}")
    info = VideoInfo()
    with contextlib.suppress(TypeError, ValueError):
        info.duration = float(data.get("format", {}).get("duration") or 0)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info.width:
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
        elif stream.get("codec_type") == "audio":
            info.has_audio = True
    if not info.width:
        msg = "The file has no video stream"
        raise VideoError(msg)
    return info


def even_times(duration: float, count: int) -> list[float]:
    """``count`` timestamps, each in the middle of an equal slice."""
    if duration <= 0 or count <= 0:
        return [0.0]
    step = duration / count
    return [round(step * (i + 0.5), 2) for i in range(count)]


def extract_frame(path: Path, at: float, width: int = 768) -> bytes:
    """One JPEG frame at ``at`` seconds, at most ``width`` pixels wide."""
    proc = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "1",
            "-ss",
            f"{max(at, 0):.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({int(width)},iw)':-2",
            "-filter_threads",
            "1",
            "-q:v",
            "4",
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "-",
        ],
        FFMPEG_TIMEOUT,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode(errors="replace")[-300:] or "no frame there"
        msg = f"No frame at {at:.2f}s: {err}"
        raise VideoError(msg)
    return proc.stdout


def extract_frames(
    path: Path, times: list[float], width: int = 768
) -> list[tuple[float, bytes]]:
    return [(at, extract_frame(path, at, width)) for at in times]


def timestamp(seconds: float) -> str:
    minutes, rest = divmod(max(seconds, 0), 60)
    return f"{int(minutes)}:{rest:05.2f}"


# -- Gemini ---------------------------------------------------------------------


@dataclass
class GeminiAnswer:
    text: str
    model: str
    usage: dict = field(default_factory=dict)

    def usage_line(self) -> str:
        u = self.usage
        return (
            f"{self.model}: {u.get('promptTokenCount', 0)} tokens in, "
            f"{u.get('candidatesTokenCount', 0) + u.get('thoughtsTokenCount', 0)} out"
        )


class Gemini:
    """The few Gemini API calls a video question needs."""

    def __init__(
        self, api_key: str, model: str = DEFAULT_GEMINI_MODEL, timeout: int = 120
    ):
        if not api_key:
            msg = "No Gemini API key"
            raise VideoError(msg)
        self.api_key = api_key
        self.model = model or DEFAULT_GEMINI_MODEL
        self.timeout = timeout

    def _request(  # noqa: PLR0913
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        stream=None,
        headers: dict | None = None,
        timeout: int = 0,
    ):
        headers = {"x-goog-api-key": self.api_key, **(headers or {})}
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(  # noqa: S310
            url, data=body if stream is None else stream, headers=headers, method=method
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout or self.timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            msg = f"Gemini {method} {urllib.parse.urlsplit(url).path} failed ({exc.code}): {detail}"
            raise VideoError(msg) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            msg = f"Couldn't reach Gemini: {exc}"
            raise VideoError(msg) from exc

    def _json(
        self, method: str, url: str, payload: dict | None = None, **kwargs
    ) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        with self._request(method, url, body=body, **kwargs) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def upload(self, path: Path, media_type: str, name: str = "") -> dict:
        """Upload ``path`` (streamed) and return the file once it's ACTIVE."""
        size = path.stat().st_size
        start = self._request(
            "POST",
            f"{GEMINI_BASE}/upload/v1beta/files",
            body=json.dumps({"file": {"display_name": name or path.name}}).encode(),
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": media_type,
            },
        )
        with start:
            upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            msg = "Gemini didn't return an upload URL"
            raise VideoError(msg)
        with path.open("rb") as handle:
            response = self._request(
                "POST",
                upload_url,
                stream=handle,
                headers={
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                },
                timeout=max(self.timeout, 600),
            )
            with response:
                uploaded = json.loads(response.read() or b"{}").get("file") or {}
        return self.wait_active(uploaded)

    def wait_active(self, file: dict, timeout: int = 300, poll: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while file.get("state") == "PROCESSING":
            if time.monotonic() > deadline:
                self.delete(file)
                msg = "Gemini is still processing the video; try again later"
                raise VideoError(msg)
            time.sleep(poll)
            file = self._json("GET", f"{GEMINI_BASE}/v1beta/{file['name']}")
        if file.get("state") != "ACTIVE":
            self.delete(file)
            error = (file.get("error") or {}).get("message") or file.get("state")
            msg = f"Gemini couldn't process the video: {error}"
            raise VideoError(msg)
        return file

    def delete(self, file: dict) -> None:
        if file.get("name"):
            with contextlib.suppress(VideoError):
                self._request("DELETE", f"{GEMINI_BASE}/v1beta/{file['name']}").close()

    def ask(  # noqa: PLR0913
        self,
        file: dict,
        question: str,
        *,
        system: str = "",
        start: float | None = None,
        end: float | None = None,
        fps: float | None = None,
    ) -> GeminiAnswer:
        video = {
            "file_data": {"mime_type": file.get("mimeType"), "file_uri": file["uri"]}
        }
        meta = {}
        if start is not None:
            meta["start_offset"] = f"{max(start, 0):g}s"
        if end is not None:
            meta["end_offset"] = f"{end:g}s"
        if fps:
            meta["fps"] = fps
        if meta:
            video["video_metadata"] = meta
        payload: dict = {
            "contents": [{"role": "user", "parts": [video, {"text": question}]}]
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        data = self._json(
            "POST",
            f"{GEMINI_BASE}/v1beta/models/{urllib.parse.quote(self.model)}:generateContent",
            payload,
            timeout=max(self.timeout, 300),
        )
        candidates = data.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts") if candidates else []
        text = "".join(p.get("text", "") for p in parts or [] if not p.get("thought"))
        if not text.strip():
            reason = (
                candidates[0].get("finishReason")
                if candidates
                else (data.get("promptFeedback") or {}).get("blockReason")
            )
            msg = f"Gemini gave no answer ({reason or 'empty response'})"
            raise VideoError(msg)
        return GeminiAnswer(
            text=text.strip(),
            model=data.get("modelVersion") or self.model,
            usage=data.get("usageMetadata") or {},
        )


def ask_gemini(  # noqa: PLR0913
    path: Path,
    media_type: str,
    question: str,
    *,
    api_key: str,
    model: str = DEFAULT_GEMINI_MODEL,
    system: str = "",
    start: float | None = None,
    end: float | None = None,
    fps: float | None = None,
) -> GeminiAnswer:
    """Upload a video, ask one question about it, and delete the upload."""
    client = Gemini(api_key, model)
    file = client.upload(path, media_type or "video/mp4")
    try:
        return client.ask(file, question, system=system, start=start, end=end, fps=fps)
    finally:
        client.delete(file)
