"""Tests for image, audio and document attachments on user messages."""

from __future__ import annotations

import base64
import hashlib

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import override_settings
from django_ergo.conversation.attachments import Attachment
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.runner import run_conversation_turn
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import run_structured_call

from tests.test_conversation_structured import VALID_PLAN
from tests.test_conversation_structured import FakeOpenAIClient
from tests.test_conversation_structured import Plan
from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_text
from tests.test_conversation_structured import claude_tool

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)

PNG = b"\x89PNG\r\n\x1a\nfake-image"
WAV = b"RIFF....WAVEfake-audio"
PDF = b"%PDF-1.4 fake"


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture()
def user():
    return User.objects.create_user(username="attach", password="x")


def make_session(user, engine_type="claude"):
    return ConversationSession.objects.create(
        user=user, engine_type=engine_type, transport_type="api", status="active"
    )


def openai_text(text):
    from types import SimpleNamespace

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


async def fake_transcriber(data, media_type, filename):
    return f"transcribed {filename} ({media_type}, {len(data)} bytes)"


def test_attachment_kind_and_validation(tmp_path):
    path = tmp_path / "fridge.jpg"
    path.write_bytes(PNG)
    attachment = Attachment.from_path(path)
    assert attachment.kind == "image"
    assert attachment.media_type == "image/jpeg"
    assert attachment.filename == "fridge.jpg"
    assert Attachment(media_type="audio/wav", data=WAV).kind == "audio"
    assert Attachment(media_type="application/pdf", data=PDF).kind == "document"
    with pytest.raises(ValueError, match="data or a url"):
        Attachment(media_type="image/png")


async def test_claude_sends_image_and_document_blocks_before_text(user):
    session = await sync_to_async(make_session)(user)
    engine = claude_engine(claude_text("A fridge"))

    events = [
        e
        async for e in run_conversation_turn(
            engine,
            session,
            "What is this?",
            attachments=[
                Attachment(media_type="image/png", data=PNG, filename="f.png"),
                Attachment(media_type="image/jpeg", url="https://example.com/a.jpg"),
                Attachment(media_type="application/pdf", data=PDF, filename="r.pdf"),
            ],
        )
    ]

    assert [e.text for e in events if e.event_type == "text"] == ["A fridge"]
    content = engine._client.calls[0]["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(PNG).decode(),
        },
    }
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.com/a.jpg"},
    }
    assert content[2]["type"] == "document"
    assert content[2]["source"]["media_type"] == "application/pdf"
    assert content[3] == {"type": "text", "text": "What is this?"}

    rows = [row async for row in ConversationAttachment.objects.filter(session=session)]
    assert [r.position for r in rows] == [0, 1, 2]
    assert rows[0].message_sequence == 0
    assert rows[0].size == len(PNG)
    assert rows[0].sha256 == hashlib.sha256(PNG).hexdigest()
    assert rows[1].file.name == ""


async def test_claude_gets_audio_as_transcript(user):
    session = await sync_to_async(make_session)(user)
    engine = claude_engine(claude_text("ok"), claude_text("ok"))

    await engine.append_user_message(
        session,
        "listen",
        [Attachment(media_type="audio/wav", data=WAV, transcript="add milk")],
    )
    await engine.append_user_message(
        session, "again", [Attachment(media_type="audio/wav", data=WAV)]
    )
    messages = await sync_to_async(engine.reconstruct_messages)(session)

    assert messages[0]["content"][0]["text"] == (
        "[Audio attachment audio/wav, transcript]: add milk"
    )
    assert "no transcript available" in messages[1]["content"][0]["text"]


async def test_audio_is_transcribed_when_transcriber_configured(user):
    session = await sync_to_async(make_session)(user)
    engine = claude_engine()
    with override_settings(
        DJANGO_ERGO={
            "AUDIO_TRANSCRIBER": (
                "tests.test_conversation_attachments.fake_transcriber"
            )
        }
    ):
        await engine.append_user_message(
            session,
            "voice note",
            [Attachment(media_type="audio/wav", data=WAV, filename="note.wav")],
        )

    row = await ConversationAttachment.objects.aget(session=session)
    assert row.transcript == f"transcribed note.wav (audio/wav, {len(WAV)} bytes)"


async def test_openai_parts(user):
    session = await sync_to_async(make_session)(user, "openai")
    engine = OpenAIAPIEngine(config={"model": "gpt-test"})
    engine._client = FakeOpenAIClient(openai_text("ok"))

    events = [
        e
        async for e in engine.send(
            session,
            "look",
            attachments=[
                Attachment(media_type="image/png", data=PNG),
                Attachment(media_type="audio/wav", data=WAV, transcript="hi"),
                Attachment(media_type="application/pdf", data=PDF, filename="r.pdf"),
            ],
        )
    ]

    assert events[0].text == "ok"
    content = engine._client.calls[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[2] == {
        "type": "text",
        "text": "[Audio attachment audio/wav, transcript]: hi",
    }
    assert content[3]["type"] == "file"
    assert content[3]["file"]["filename"] == "r.pdf"


async def test_openai_native_audio_input(user):
    session = await sync_to_async(make_session)(user, "openai")
    engine = OpenAIAPIEngine(config={"model": "gpt-audio", "audio_input": True})
    await engine.append_user_message(
        session, "hear", [Attachment(media_type="audio/wav", data=WAV)]
    )

    messages = await sync_to_async(engine.reconstruct_messages)(session)

    assert messages[0]["content"][1] == {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(WAV).decode(), "format": "wav"},
    }


async def test_structured_call_with_image(user):
    engine = claude_engine(claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(
        spec,
        user=user,
        message="Plan from this whiteboard",
        engine=engine,
        attachments=[Attachment(media_type="image/png", data=PNG)],
    )

    assert result.ok
    content = engine._client.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[1]["text"] == "Plan from this whiteboard"
    # The transcript keeps a placeholder, not the bytes.
    stored = result.call.transcript[0]["content"][0]
    assert stored == {"type": "text", "text": "[image attachment, not stored]"}
