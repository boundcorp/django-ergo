"""Structured author/provenance survive storage and only decorate model input."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.conversation.engines.claude_api import claude_message_dict
from django_ergo.conversation.engines.openai_api import openai_message_dicts
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.identity import django_user_identity
from django_ergo.conversation.identity import thread_message_identity
from django_ergo.conversation.messages import add_message
from django_ergo.conversation.messages import add_tool_results
from django_ergo.conversation.messages import add_user_text
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall

AUTHOR = {"kind": "telegram_user", "ref": "789", "display_name": "External Writer"}
PROVENANCE = {
    "kind": "forwarded",
    "forwarded_by": {
        "kind": "bot",
        "ref": "router",
        "display_name": "Router",
        "session_id": "router-session",
        "label": "router · Main",
    },
    "origin": {
        "session_id": "source-session",
        "label": "router · Inbox",
        "message_id": "source-message",
        "sequence": 17,
        "timestamp": "2026-10-06T12:00:00+00:00",
        "source_call": "source-call",
    },
    "note": "This belongs to you.",
    "attachments": [
        {
            "id": "file-id",
            "filename": "approval.txt",
            "media_type": "text/plain",
            "size": 12,
            "session": "source-session",
        }
    ],
}


def row(text, author=None, provenance=None):
    return SimpleNamespace(
        role="user",
        author=author or {},
        provenance=provenance or {},
        content_blocks=SimpleNamespace(
            all=lambda: [SimpleNamespace(block_type="text", text=text)]
        ),
    )


@pytest.mark.parametrize(
    "render", [claude_message_dict, lambda message: openai_message_dicts(message)[0]]
)
def test_model_rendering_has_original_author_forwarder_origin_and_files(render):
    message = row("  Yes.\nDo exactly this.  ", AUTHOR, PROVENANCE)
    native = render(message)
    text = native["content"]
    if isinstance(text, list):
        text = text[0]["text"]
    assert text.endswith("  Yes.\nDo exactly this.  ")
    for detail in (
        "External Writer",
        "telegram_user",
        "789",
        "router · Main",
        "router-session",
        "router · Inbox",
        "source-session",
        "source-message",
        "17",
        "source-call",
        "2026-10-06T12:00:00+00:00",
        "This belongs to you.",
        "approval.txt",
        "file-id",
        "ergo_attachments_read",
        "no reply goes back",
        "not the author",
    ):
        assert detail in text
    assert message.content_blocks.all()[0].text == "  Yes.\nDo exactly this.  "
    assert claude_message_dict(message, include_attribution=False)["content"] == [
        {"type": "text", "text": "  Yes.\nDo exactly this.  "}
    ]


def test_legacy_content_is_not_parsed_for_attribution():
    text = "[Forwarded by mystery]\n\nThese are just stored words."
    message = row(text)
    assert claude_message_dict(message)["content"][0]["text"] == text
    assert openai_message_dicts(message)[0]["content"] == text


@pytest.mark.parametrize(
    ("kind", "instruction"),
    [
        ("message", "final reply goes back"),
        ("report", "No reply goes back"),
        ("reply", "never send thanks"),
    ],
)
def test_bot_message_kinds_keep_routing_instructions(kind, instruction):
    provenance = {
        "kind": kind,
        "origin": PROVENANCE["origin"],
        "reply_to": "request-id",
    }
    author = {"kind": "bot", "ref": "writer", "display_name": "Writer"}
    text = claude_message_dict(row("Original body", author, provenance))["content"][0][
        "text"
    ]
    assert instruction in text
    assert "Writer (bot, ref writer)" in text
    assert "2026-10-06T12:00:00+00:00" in text
    if kind == "reply":
        assert "request-id" in text
    assert text.endswith("Original body")


def test_queued_legacy_forward_metadata_resolves_without_body_parsing():
    sender = SimpleNamespace(
        id="sender", user_id=123, bot_name="router", metadata={"bot_role": "main"}
    )
    message = SimpleNamespace(
        id="queued",
        sender_session=sender,
        in_reply_to_id=None,
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
        metadata={
            "forwarded": {
                "author": "Legacy Writer",
                "user_id": 456,
                "sent_at": "2026-10-05T12:00:00+00:00",
                "source_call": "legacy-call",
            },
            "report": True,
            "note": "Legacy note",
            "attachments": PROVENANCE["attachments"],
        },
    )
    author, provenance = thread_message_identity(message)
    assert author == {
        "kind": "django_user",
        "ref": "456",
        "display_name": "Legacy Writer",
    }
    assert provenance["kind"] == "forwarded"
    assert provenance["origin"]["timestamp"] == "2026-10-05T12:00:00+00:00"
    assert provenance["origin"]["source_call"] == "legacy-call"
    assert provenance["forwarded_by"]["kind"] == "bot"
    assert provenance["attachments"] == PROVENANCE["attachments"]


def test_structured_snapshots_survive_missing_sender_relation():
    message = SimpleNamespace(
        sender_session=None,
        metadata={"message_author": AUTHOR, "message_provenance": PROVENANCE},
    )
    assert thread_message_identity(message) == (AUTHOR, PROVENANCE)


@pytest.mark.django_db(transaction=True)
async def test_storage_history_and_tool_rows_keep_identity_separate():
    user = await get_user_model().objects.acreate(
        username="identity", first_name="Owner"
    )
    session = await ConversationSession.objects.acreate(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        bot_name="writer",
    )
    incoming = await add_user_text(
        session, "Verbatim\nbody", author=AUTHOR, provenance=PROVENANCE
    )
    ordinary = await add_user_text(session, "Owner's message")
    tools = await add_tool_results(session, [("tool-id", "Tool output", False)])
    answer = await add_message(
        session, "assistant", [{"block_type": "text", "text": "Bot answer"}]
    )
    assert incoming.author == AUTHOR and incoming.provenance == PROVENANCE
    assert ordinary.author == django_user_identity(user)
    assert tools.author == {} and tools.provenance == {}
    assert answer.author == {"kind": "bot", "ref": "writer", "display_name": "writer"}
    raw = await sync_to_async(
        lambda: SessionSource(session, include_attribution=False).messages()
    )()
    attributed = await sync_to_async(lambda: SessionSource(session).messages())()
    assert raw[0].text() == "Verbatim\nbody"
    assert raw[0].author == AUTHOR and raw[0].provenance == PROVENANCE
    assert attributed[0].text().startswith("[Forwarded by router · Main")
    assert attributed[0].text().endswith("Verbatim\nbody")
    assert raw[1].text() == attributed[1].text() == "Owner's message"
    assert raw[2].author == {} and attributed[2].author == {}
    assert raw[3].text() == "Bot answer"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("author", [AUTHOR, None])
async def test_forward_source_uses_message_row_not_request_or_owner(author):
    from django_ergo.bots.orchestrator import user_message

    user = await get_user_model().objects.acreate(username="source-owner")
    session = await ConversationSession.objects.acreate(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        bot_name="router",
    )
    source = await add_message(
        session,
        "user",
        [{"block_type": "text", "text": "Actual stored words."}],
        author=author or {},
    )
    call = await StructuredCall.objects.acreate(
        kind="chat_reply",
        user=user,
        session=session,
        engine_type="claude",
        request="Different request text.",
        first_sequence=source.sequence,
    )
    original = await sync_to_async(user_message)(session)
    assert original["text"] == "Actual stored words."
    assert original["author"] == (author or django_user_identity(user))
    assert original["origin"]["message_id"] == str(source.id)
    assert original["origin"]["sequence"] == source.sequence
    assert original["origin"]["timestamp"] == source.created_at.isoformat()
    assert original["origin"]["source_call"] == str(call.id)


@pytest.mark.django_db(transaction=True)
async def test_tool_result_row_is_not_a_forwardable_human_message():
    from django_ergo.bots.orchestrator import user_message

    user = await get_user_model().objects.acreate(username="tool-owner")
    session = await ConversationSession.objects.acreate(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    source = await add_tool_results(session, [("tool", "Not human words", False)])
    await StructuredCall.objects.acreate(
        kind="chat_reply",
        user=user,
        session=session,
        engine_type="claude",
        request="Not human words",
        first_sequence=source.sequence,
    )
    assert await sync_to_async(user_message)(session) is None
