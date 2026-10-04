"""Moving a chat's history to another engine (django_ergo.conversation.engine_switch)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.engine_switch import MISSING_RESULT
from django_ergo.conversation.engine_switch import switch_engine
from django_ergo.conversation.engines.claude_api import claude_message_dict
from django_ergo.conversation.engines.openai_api import openai_message_dict
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationCompaction
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import OpenAIMessage
from django_ergo.conversation.models import StructuredCall

User = get_user_model()

IMAGE = {"type": "image_ref", "attachment_id": "a1", "name": "shelf.png"}


def openai_chat(user):
    session = ConversationSession.objects.create(
        user=user,
        engine_type="openai",
        transport_type="api",
        status="active",
        system_prompt="You run the kitchen.",
    )
    rows = [
        {"role": "system", "content": "You run the kitchen."},
        {"role": "user", "content": "How many eggs?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "pantry", "arguments": '{"item": "eggs"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "photo", "arguments": "{}"},
                },
            ],
            "model_name": "gpt-6-luna",
            "input_tokens": 120,
        },
        {"role": "tool", "content": "4", "tool_call_id": "call_1"},
        {
            "role": "tool",
            "content": "shelf photo",
            "tool_call_id": "call_2",
            "images": [IMAGE],
        },
        {"role": "assistant", "content": "Four eggs."},
        {"role": "user", "content": "And milk?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {"name": "pantry", "arguments": "{}"},
                }
            ],
        },  # the turn died before the result
        {"role": "user", "content": "Hello?"},
    ]
    for seq, row in enumerate(rows):
        OpenAIMessage.objects.create(session=session, sequence=seq, **row)
    return session


@pytest.mark.django_db
def test_an_openai_chat_moves_to_claude_and_back():
    user = User.objects.create_user("cook", "cook@example.com", "pw")
    session = openai_chat(user)
    ConversationAttachment.objects.create(
        session=session, message_sequence=6, filename="milk.png"
    )
    ConversationCompaction.objects.create(
        session=session,
        mode="rolling",
        from_sequence=1,
        upto_sequence=5,
        summary="Four eggs.",
    )
    call = StructuredCall.objects.create(
        kind="chat_reply",
        session=session,
        user=user,
        request="And milk?",
        first_sequence=6,
        last_sequence=8,
    )

    assert switch_engine(session, "claude", "cli") is True
    session.refresh_from_db()
    assert (session.engine_type, session.transport_type) == ("claude", "cli")
    assert not session.openai_messages.exists()

    messages = [
        claude_message_dict(m)
        for m in session.claude_messages.prefetch_related("content_blocks")
    ]
    assert messages == [
        {"role": "user", "content": [{"type": "text", "text": "How many eggs?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "pantry",
                    "input": {"item": "eggs"},
                },
                {"type": "tool_use", "id": "call_2", "name": "photo", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "4",
                    "is_error": False,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_2",
                    "content": [{"type": "text", "text": "shelf photo"}, IMAGE],
                    "is_error": False,
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Four eggs."}]},
        {"role": "user", "content": [{"type": "text", "text": "And milk?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_3", "name": "pantry", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_3",
                    "content": MISSING_RESULT,
                    "is_error": True,
                }
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "Hello?"}]},
    ]
    assert session.claude_messages.get(sequence=1).model_name == "gpt-6-luna"

    # The sequences other rows point at follow the messages (the system row is gone).
    attachment = session.attachments.get()
    assert attachment.message_sequence == 5
    assert (
        session.claude_messages.get(sequence=5).content_blocks.get().text == "And milk?"
    )
    compaction = session.compactions.get()
    assert (compaction.from_sequence, compaction.upto_sequence) == (0, 4)
    call.refresh_from_db()
    assert (call.first_sequence, call.last_sequence) == (5, 8)

    # And back: the system prompt goes first again, the results become tool rows.
    assert switch_engine(session, "openai", "api") is True
    session.refresh_from_db()
    assert not session.claude_messages.exists()
    rows = [openai_message_dict(m) for m in session.openai_messages.all()]
    assert [(m["role"], m.get("tool_call_id")) for m in rows] == [
        ("system", None),
        ("user", None),
        ("assistant", None),
        ("tool", "call_1"),
        ("tool", "call_2"),
        ("assistant", None),
        ("user", None),
        ("assistant", None),
        ("tool", "call_3"),
        ("user", None),
    ]
    assert rows[2]["tool_calls"][0]["function"] == {
        "name": "pantry",
        "arguments": '{"item": "eggs"}',
    }
    assert session.openai_messages.get(sequence=4).images == [IMAGE]
    assert session.attachments.get().message_sequence == 6
    call.refresh_from_db()
    assert (call.first_sequence, call.last_sequence) == (6, 9)


@pytest.mark.django_db
def test_switching_to_the_same_engine_changes_nothing():
    user = User.objects.create_user("cook", "cook@example.com", "pw")
    session = openai_chat(user)
    assert switch_engine(session, "openai", "api") is False
    assert session.openai_messages.count() == 9
    with pytest.raises(ValueError, match="gemini"):
        switch_engine(session, "gemini")
