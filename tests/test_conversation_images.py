"""Tests for images in tool results and the images-in-context window."""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.attachments import Attachment
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.images import ToolImage
from django_ergo.conversation.images import ToolResult
from django_ergo.conversation.images import image_ref
from django_ergo.conversation.images import prepare_image
from django_ergo.conversation.images import prepare_messages
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import run_structured_call
from django_ergo.conversation.toolkit import Toolkit
from tests.test_conversation_structured import VALID_PLAN
from tests.test_conversation_structured import FakeOpenAIClient
from tests.test_conversation_structured import Plan
from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_tool
from tests.test_conversation_structured import openai_tool

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)

PNG = b"\x89PNG\r\n\x1a\nfake-chart"
CHART_B64 = base64.b64encode(PNG).decode()


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture
def user():
    return User.objects.create_user(username="images", password="x")


class ChartToolkit(Toolkit):
    """A tool that answers with a picture."""

    def has_tool(self, tool_name):
        return tool_name == "chart"

    def execute_tool(self, tool_name, arguments):
        name = arguments.get("name", "chart.png")
        return ToolResult(f"Chart {name}", [ToolImage(PNG, "image/png", name=name)])

    def get_tools_schema(self, adapter):
        return [{"name": "chart", "input_schema": {"type": "object"}}]

    def render_overview(self):
        return ""


def make_session(user, engine_type):
    return ConversationSession.objects.create(
        user=user, engine_type=engine_type, transport_type="api", status="active"
    )


def openai_engine(*responses):
    engine = OpenAIAPIEngine(config={"model": "gpt-test"})
    engine._client = FakeOpenAIClient(*responses)
    return engine


# -- Claude ---------------------------------------------------------------------


async def test_claude_sends_tool_images_inside_the_tool_result(user):
    engine = claude_engine(
        claude_tool("chart", {}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[ChartToolkit()]
    )

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    tool_result = engine._client.calls[1]["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"] == [
        {"type": "text", "text": "Chart chart.png"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": CHART_B64},
        },
    ]
    # A standalone call has no session to keep the file in: the transcript
    # notes the image instead of storing its bytes.
    stored = result.call.transcript[2]["content"][0]["content"]
    assert stored[1] == {
        "type": "text",
        "text": "[image attachment chart.png, not stored]",
    }


async def test_claude_session_keeps_a_reference_to_a_saved_file(user):
    session = await sync_to_async(make_session)(user, "claude")
    engine = claude_engine(
        claude_tool("chart", {}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[ChartToolkit()]
    )

    result = await run_structured_call(spec, "Plan", session=session, engine=engine)

    assert result.ok
    row = await ConversationAttachment.objects.aget(session=session)
    assert (row.filename, row.source, row.message_sequence) == (
        "chart.png",
        "bot",
        None,
    )
    block = await MessageBlock.objects.aget(
        block_type="tool_result", tool_result_for="toolu_1"
    )
    assert block.tool_result_content == [
        {"type": "text", "text": "Chart chart.png"},
        {
            "type": "image_ref",
            "name": "chart.png",
            "media_type": "image/png",
            "text": "[image: chart.png]",
            "attachment_id": str(row.id),
        },
    ]
    sent = engine._client.calls[1]["messages"][-1]["content"][0]["content"]
    assert sent[1]["source"]["data"] == CHART_B64


# -- OpenAI ---------------------------------------------------------------------


async def test_openai_moves_tool_images_into_a_user_message(user):
    session = await sync_to_async(make_session)(user, "openai")
    engine = openai_engine(
        openai_tool("chart", {}),
        openai_tool("submit_output", VALID_PLAN, call_id="call_2"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[ChartToolkit()]
    )

    result = await run_structured_call(spec, "Plan", session=session, engine=engine)

    assert result.ok
    messages = engine._client.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    row = await ConversationAttachment.objects.aget(session=session)
    assert messages[2]["content"] == (
        f"Chart chart.png\n[image: chart.png (id={row.id}), shown below]"
    )
    images = messages[3]["content"]
    assert images[0]["text"] == "Images from the tool results above:"
    assert images[2] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{CHART_B64}"},
    }
    block = await MessageBlock.objects.aget(
        message__session=session, tool_result_for="call_1"
    )
    text, ref = block.tool_result_content
    assert text == {"type": "text", "text": "Chart chart.png"}
    assert ref["attachment_id"] == str(row.id)
    assert "data" not in ref


async def test_openai_standalone_call_sends_tool_images(user):
    engine = openai_engine(
        openai_tool("chart", {}),
        openai_tool("submit_output", VALID_PLAN, call_id="call_2"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[ChartToolkit()]
    )

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    messages = engine._client.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "user"]
    assert messages[2]["content"].startswith("Chart chart.png\n[image: chart.png")
    assert messages[3]["content"][2]["image_url"]["url"].endswith(CHART_B64)
    assert not await ConversationAttachment.objects.aexists()


# -- the window -------------------------------------------------------------------


def test_only_the_latest_images_are_sent():
    first = image_ref(name="a.png", media_type="image/png", data=PNG)
    second = image_ref(name="b.png", media_type="image/png", data=PNG)
    third = image_ref(name="c.png", media_type="image/png", data=PNG)
    messages = [
        {"role": "user", "content": [first, {"type": "text", "text": "Look"}]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "two"}, second, third],
                }
            ],
        },
    ]

    sent = prepare_messages(messages, "claude", keep=2)

    assert sent[0]["content"][0] == {"type": "text", "text": "[image omitted: a.png]"}
    inner = sent[1]["content"][0]["content"]
    assert [b["type"] for b in inner] == ["text", "image", "image"]
    # The input keeps its references, so the next call can window again.
    assert messages[0]["content"][0] is first

    none = prepare_messages(messages, "claude", keep=0)
    assert none[1]["content"][0]["content"][2]["text"] == "[image omitted: c.png]"


async def test_user_photos_share_the_window_and_name_their_id(user, settings):
    settings.DJANGO_ERGO = {"IMAGES_IN_CONTEXT": 1}
    session = await sync_to_async(make_session)(user, "openai")
    engine = openai_engine()

    await engine.append_user_message(
        session,
        "My fridge",
        [Attachment(media_type="image/png", data=PNG, filename="fridge.png")],
    )
    await engine.append_user_message(
        session,
        "And the pantry",
        [Attachment(media_type="image/png", data=PNG, filename="pantry.png")],
    )
    messages = await sync_to_async(engine.reconstruct_messages)(session)

    fridge = await ConversationAttachment.objects.aget(filename="fridge.png")
    assert messages[0]["content"] == [
        {"type": "text", "text": "My fridge"},
        {"type": "text", "text": f"[image omitted: fridge.png (id={fridge.id})]"},
    ]
    assert messages[1]["content"][1]["type"] == "image_url"


def test_missing_and_unsendable_images_become_placeholders():
    gone = image_ref(name="gone.png", media_type="image/png", attachment_id="nope")
    svg = image_ref(name="logo.svg", media_type="image/svg+xml", data=b"<svg/>")
    sent = prepare_messages([{"role": "user", "content": [gone, svg]}], "openai")
    assert sent[0]["content"] == [
        {"type": "text", "text": "[image unavailable: gone.png (id=nope)]"},
        {"type": "text", "text": "[image unavailable: logo.svg]"},
    ]


# -- bot tools ----------------------------------------------------------------------


def test_bot_tools_can_return_images():
    @bot_tool
    def snapshot() -> ToolImage:
        """Take a picture."""
        return ToolImage(PNG, name="snap.png")

    @bot_tool
    def labelled() -> ToolResult:
        """A picture with words."""
        return ToolResult("Here", [ToolImage(PNG)])

    toolkit = FunctionToolkit([snapshot.__bot_tool__, labelled.__bot_tool__])
    bare = toolkit.execute_tool("snapshot", {})
    assert isinstance(bare, ToolResult)
    assert bare.images[0].name == "snap.png"
    assert str(toolkit.execute_tool("labelled", {})) == "Here"
    with pytest.raises(ValueError, match="needs data"):
        ToolImage()


# -- downscaling -----------------------------------------------------------------


def test_large_images_are_downscaled():
    image_module = pytest.importorskip("PIL.Image")
    buffer = BytesIO()
    image_module.new("RGB", (3000, 1500), "red").save(buffer, "PNG")

    data, media_type = prepare_image(buffer.getvalue(), "image/png")

    assert media_type == "image/jpeg"
    with image_module.open(BytesIO(data)) as small:
        assert small.size == (1024, 512)

    tiny = BytesIO()
    image_module.new("RGBA", (10, 10)).save(tiny, "PNG")
    assert prepare_image(tiny.getvalue(), "image/png") == (tiny.getvalue(), "image/png")


def test_without_pillow_images_go_as_they_are(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pil(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pil)
    assert prepare_image(PNG, "image/png") == (PNG, "image/png")
    assert prepare_image(b"x" * 5_000_001, "image/png") is None
    assert prepare_image(b"BM", "image/bmp") is None
