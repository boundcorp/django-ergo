"""Videos: frames with ffmpeg and questions answered by Gemini."""

from __future__ import annotations

import io
import json
import shutil
import subprocess

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots.tools import ToolContext
from django_ergo.conversation import video
from django_ergo.conversation.attachments import save_session_file
from tests.test_bot_plugins import files_bot

User = get_user_model()

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are required",
)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A 4-second 320x240 test pattern with a tone."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg and ffprobe are required")
    path = tmp_path_factory.mktemp("video") / "ad.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc=duration=4:size=320x240:rate=10", "-f", "lavfi", "-i",
            "sine=duration=4", "-shortest", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def test_even_times_sit_in_the_middle_of_equal_slices():
    assert video.even_times(10, 5) == [1.0, 3.0, 5.0, 7.0, 9.0]
    assert video.even_times(0, 3) == [0.0]


def test_is_video_by_type_or_extension():
    assert video.is_video("video/mp4")
    assert video.is_video("application/octet-stream", "ad.MOV")
    assert not video.is_video("image/png", "a.png")


@needs_ffmpeg
def test_probe_and_extract_frames(clip):
    info = video.probe(clip)
    assert info.width == 320
    assert info.has_audio
    assert 3.5 < info.duration < 4.5
    frames = video.extract_frames(clip, [0.5, 2.0], width=160)
    assert [at for at, _ in frames] == [0.5, 2.0]
    from PIL import Image

    image = Image.open(io.BytesIO(frames[0][1]))
    assert image.format == "JPEG"
    assert image.size == (160, 120)


@needs_ffmpeg
def test_probe_refuses_a_file_that_isnt_video(tmp_path):
    bogus = tmp_path / "x.mp4"
    bogus.write_bytes(b"not a video")
    with pytest.raises(video.VideoError):
        video.probe(bogus)


@needs_ffmpeg
@pytest.mark.django_db(transaction=True)
def test_frames_tool_returns_images_the_bot_sees(tmp_path, settings, clip):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, _, plugin = files_bot(tmp_path)
    user = User.objects.create(username="frames")
    root = async_to_sync(bot.root_session)(user)
    row = save_session_file(root, "ad.mp4", clip.read_bytes(), media_type="video/mp4")
    ctx = ToolContext(bot=bot, session=root, user=user)

    result = plugin.frames(ctx, "ad.mp4", count=3)
    assert "ad.mp4: 4.0s, 320x240, with sound" in result.text
    assert len(result.images) == 3
    assert result.images[0].media_type == "image/jpeg"
    assert result.images[0].name == "ad@000.67s.jpg"

    # Timestamps past the end are pulled back inside the video.
    result = plugin.frames(ctx, str(row.id), times=[1, 99])
    assert len(result.images) == 2
    assert "0:01.00" in result.text

    save_session_file(root, "a.txt", b"hi")
    with pytest.raises(ValueError, match="isn't a video"):
        plugin.frames(ctx, "a.txt")


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def gemini(monkeypatch):
    """Fakes the Gemini API: records requests and answers each endpoint."""
    calls = []
    state = {"polls": 0}

    def urlopen(request, timeout=0):
        url, method = request.full_url, request.get_method()
        data = request.data
        if hasattr(data, "read"):
            data = data.read()
        calls.append((method, url, dict(request.header_items()), data))
        if url.endswith("/upload/v1beta/files"):
            return FakeResponse(headers={"x-goog-upload-url": "https://up.example/1"})
        if url == "https://up.example/1":
            file = {"name": "files/abc", "uri": "https://g/files/abc",
                    "mimeType": "video/mp4", "state": "PROCESSING"}  # fmt: skip
            return FakeResponse(json.dumps({"file": file}).encode())
        if method == "GET":
            state["polls"] += 1
            file = {"name": "files/abc", "uri": "https://g/files/abc",
                    "mimeType": "video/mp4", "state": "ACTIVE"}  # fmt: skip
            return FakeResponse(json.dumps(file).encode())
        if method == "DELETE":
            return FakeResponse(b"{}")
        if ":generateContent" in url:
            body = {
                "candidates": [
                    {"content": {"parts": [
                        {"text": "thinking...", "thought": True},
                        {"text": "0:00 a hook card, then a demo."},
                    ]}}
                ],
                "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 30},
                "modelVersion": "gemini-test",
            }  # fmt: skip
            return FakeResponse(json.dumps(body).encode())
        raise AssertionError(url)

    monkeypatch.setattr(video.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(video.time, "sleep", lambda s: None)
    return calls


@pytest.mark.django_db(transaction=True)
def test_looking_at_a_video_asks_gemini(tmp_path, settings, monkeypatch, gemini):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    bot, _, plugin = files_bot(tmp_path, config="max_bytes: 100, video_model: gemini-x")
    user = User.objects.create(username="watcher")
    root = async_to_sync(bot.root_session)(user)
    row = save_session_file(root, "ad.mp4", b"v" * 64, media_type="video/mp4")
    ctx = ToolContext(bot=bot, session=root, user=user)

    answer = plugin.look(ctx, str(row.id), "What's the hook?", end_seconds=10)

    assert answer == (
        "ad.mp4 (0s to 10s): 0:00 a hook card, then a demo.\n\n"
        "[gemini-test: 1200 tokens in, 30 out]"
    )
    methods = [(m, u.split("?")[0]) for m, u, _, _ in gemini]
    assert methods[0] == ("POST", f"{video.GEMINI_BASE}/upload/v1beta/files")
    assert methods[1] == ("POST", "https://up.example/1")
    assert methods[2] == ("GET", f"{video.GEMINI_BASE}/v1beta/files/abc")
    assert methods[3][1].endswith("/models/gemini-x:generateContent")
    assert methods[-1] == ("DELETE", f"{video.GEMINI_BASE}/v1beta/files/abc")
    start_headers = {k.lower(): v for k, v in gemini[0][2].items()}
    assert start_headers["x-goog-api-key"] == "g-key"
    assert start_headers["x-goog-upload-header-content-length"] == "64"
    assert gemini[1][3] == b"v" * 64  # the bytes went up as a stream
    payload = json.loads(gemini[3][3])
    video_part, question = payload["contents"][0]["parts"]
    assert video_part["file_data"]["file_uri"] == "https://g/files/abc"
    assert video_part["video_metadata"] == {"end_offset": "10s"}
    assert question == {"text": "What's the hook?"}
    assert "can't see video" in payload["system_instruction"]["parts"][0]["text"]


@pytest.mark.django_db(transaction=True)
def test_gemini_errors_become_tool_errors(tmp_path, settings, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")

    def urlopen(request, timeout=0):
        reason = "no route"
        raise video.urllib.error.URLError(reason)

    monkeypatch.setattr(video.urllib.request, "urlopen", urlopen)
    bot, _, plugin = files_bot(tmp_path)
    user = User.objects.create(username="watcher2")
    root = async_to_sync(bot.root_session)(user)
    save_session_file(root, "ad.webm", b"v", media_type="video/webm")
    ctx = ToolContext(bot=bot, session=root, user=user)
    with pytest.raises(
        ValueError, match="Could not watch ad.webm: Couldn't reach Gemini"
    ):
        plugin.look(ctx, "ad.webm")


@needs_ffmpeg
@pytest.mark.django_db(transaction=True)
async def test_bot_sees_frames_and_they_are_saved_in_the_chat(tmp_path, settings, clip):
    from asgiref.sync import sync_to_async

    from tests.test_bots import say
    from tests.test_conversation_structured import claude_tool

    settings.MEDIA_ROOT = str(tmp_path / "media")
    bot, engine, _ = files_bot(
        tmp_path,
        claude_tool("ergo_attachments_frames", {"attachment_id": "ad.mp4", "count": 2}),
        say("A test pattern."),
    )
    user = await User.objects.acreate(username="frame-bot")
    root = await bot.root_session(user)
    await sync_to_async(save_session_file)(
        root, "ad.mp4", clip.read_bytes(), media_type="video/mp4"
    )
    result = await bot.ask(root, "What's in the ad?")
    assert result.text == "A test pattern."
    blocks = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "image", "image"]
    names = [r.filename async for r in root.attachments.filter(media_type="image/jpeg")]
    assert sorted(names) == ["ad@001.00s.jpg", "ad@003.00s.jpg"]
