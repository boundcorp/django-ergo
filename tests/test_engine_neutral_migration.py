"""Migration 0030 copies OpenAI chats' messages into the engine-neutral tables."""

import datetime

import pytest
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("django_ergo", "0029_session_model")]
AFTER = [("django_ergo", "0030_engine_neutral_messages")]
IMAGE = {"type": "image_ref", "attachment_id": "a1", "name": "shelf.png"}
SAID = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)


def _old_chats(apps):
    user = apps.get_model(settings.AUTH_USER_MODEL).objects.create(username="legacy")
    sessions = apps.get_model("django_ergo", "ConversationSession")
    openai = sessions.objects.create(
        user=user, engine_type="openai", transport_type="api", status="active"
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
                }
            ],
            "model_name": "gpt-6-luna",
            "input_tokens": 120,
            "reasoning_tokens": 7,
        },
        {
            "role": "tool",
            "content": "4",
            "tool_call_id": "call_1",
            "images": [IMAGE],
        },
        {"role": "assistant", "content": "Four eggs."},
    ]
    message = apps.get_model("django_ergo", "OpenAIMessage")
    for seq, row in enumerate(rows):
        made = message.objects.create(session=openai, sequence=seq, **row)
        message.objects.filter(pk=made.pk).update(created_at=SAID)

    claude = sessions.objects.create(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    claude_message = apps.get_model("django_ergo", "ClaudeMessage").objects.create(
        session=claude, role="user", sequence=0
    )
    apps.get_model("django_ergo", "ClaudeContentBlock").objects.create(
        message=claude_message, block_type="text", sequence=0, text="hello"
    )
    return openai.pk, claude.pk


@pytest.mark.django_db(transaction=True)
def test_openai_chats_move_into_session_messages():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        openai_pk, claude_pk = _old_chats(executor.loader.project_state(BEFORE).apps)

        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        # Current engine code needs the current schema, not the historical one.
        executor.migrate(executor.loader.graph.leaf_nodes())

        from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
        from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
        from django_ergo.conversation.models import ConversationSession

        session = ConversationSession.objects.get(pk=openai_pk)
        rows = list(session.messages.all())
        # Same sequence numbers (the system row is left out), same timestamps.
        assert [(r.sequence, r.role) for r in rows] == [
            (1, "user"),
            (2, "assistant"),
            (3, "user"),
            (4, "assistant"),
        ]
        assert {r.created_at.replace(tzinfo=None) for r in rows} == {
            SAID.replace(tzinfo=None)
        }
        assert (rows[1].model_name, rows[1].input_tokens) == ("gpt-6-luna", 120)
        assert rows[1].reasoning_tokens == 7
        assert rows[2].content_blocks.get().tool_result_content == [
            {"type": "text", "text": "4"},
            IMAGE,
        ]

        claude_context = ClaudeAPIEngine({}).reconstruct_messages(session)
        assert claude_context[1]["content"][1] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "pantry",
            "input": {"item": "eggs"},
        }
        result = claude_context[2]["content"][0]
        assert (result["type"], result["tool_use_id"]) == ("tool_result", "call_1")

        openai_context = OpenAIAPIEngine({}).reconstruct_messages(session)
        assert [m["role"] for m in openai_context] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert openai_context[1]["tool_calls"][0]["function"] == {
            "name": "pantry",
            "arguments": '{"item": "eggs"}',
        }

        claude = ConversationSession.objects.get(pk=claude_pk)
        assert [m.content_blocks.get().text for m in claude.messages.all()] == ["hello"]
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
