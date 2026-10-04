"""Messages are stored the same way for every engine.

ClaudeMessage and ClaudeContentBlock become SessionMessage and MessageBlock
(the same tables, renamed), and OpenAI chats' OpenAIMessage rows are copied
into them. Each row keeps its sequence number, so attachments, compactions and
structured calls still point at the right messages. System rows aren't
copied: engines take the system prompt from the session. OpenAIMessage stays,
unread, for one release.
"""

import json

import django.db.models.deletion
from django.db import migrations
from django.db import models

USAGE = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_tokens",
    "model_name",
)


def _arguments(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments") or "{}"
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {"arguments": raw}
    return value if isinstance(value, dict) else {"value": value}


def _blocks(row) -> list[dict]:
    if row.role == "tool":
        text = row.content or ""
        content = (
            [*([{"type": "text", "text": text}] if text else []), *row.images]
            if row.images
            else text
        )
        return [
            {
                "block_type": "tool_result",
                "tool_result_for": row.tool_call_id or "",
                "tool_result_content": content,
            }
        ]
    blocks = []
    if row.content or row.role == "user":
        blocks.append({"block_type": "text", "text": row.content or ""})
    blocks += [
        {
            "block_type": "tool_use",
            "tool_use_id": call.get("id") or "",
            "tool_name": (call.get("function") or {}).get("name") or "",
            "tool_input": _arguments(call),
        }
        for call in row.tool_calls or []
    ]
    return blocks


def copy_openai_messages(apps, schema_editor):
    session_model = apps.get_model("django_ergo", "ConversationSession")
    openai_message = apps.get_model("django_ergo", "OpenAIMessage")
    message_model = apps.get_model("django_ergo", "SessionMessage")
    block_model = apps.get_model("django_ergo", "MessageBlock")

    sessions = session_model.objects.filter(
        legacy_openai_messages__isnull=False
    ).distinct()
    for session in sessions.iterator():
        if message_model.objects.filter(session=session).exists():
            continue  # already has messages of its own: they win
        rows = openai_message.objects.filter(session=session).exclude(role="system")
        for row in rows.order_by("sequence"):
            message = message_model.objects.create(
                session=session,
                role="assistant" if row.role == "assistant" else "user",
                sequence=row.sequence,
                stop_reason="tool_use" if row.tool_calls else None,
                **{name: getattr(row, name) for name in USAGE},
            )
            # Keep when it was said (created_at is set on insert).
            message_model.objects.filter(pk=message.pk).update(
                created_at=row.created_at, updated_at=row.updated_at
            )
            block_model.objects.bulk_create(
                block_model(message=message, sequence=i, **block)
                for i, block in enumerate(_blocks(row))
            )


def remove_copies(apps, schema_editor):
    message_model = apps.get_model("django_ergo", "SessionMessage")
    message_model.objects.filter(session__legacy_openai_messages__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0029_session_model")]

    operations = [
        migrations.RenameModel("ClaudeMessage", "SessionMessage"),
        migrations.RenameModel("ClaudeContentBlock", "MessageBlock"),
        migrations.AlterField(
            model_name="sessionmessage",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="messages",
                to="django_ergo.conversationsession",
            ),
        ),
        migrations.AlterField(
            model_name="openaimessage",
            name="session",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="legacy_openai_messages",
                to="django_ergo.conversationsession",
            ),
        ),
        migrations.AddField(
            model_name="sessionmessage",
            name="reasoning_tokens",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.RenameIndex(
            model_name="messageblock",
            new_name="django_ergo_message_3eb7a2_idx",
            old_name="django_ergo_message_c1c8c1_idx",
        ),
        migrations.RenameIndex(
            model_name="messageblock",
            new_name="django_ergo_block_t_49e134_idx",
            old_name="django_ergo_block_t_307e92_idx",
        ),
        migrations.RenameIndex(
            model_name="messageblock",
            new_name="django_ergo_tool_na_28e750_idx",
            old_name="django_ergo_tool_na_133d11_idx",
        ),
        migrations.RenameIndex(
            model_name="sessionmessage",
            new_name="django_ergo_session_700cf9_idx",
            old_name="django_ergo_session_ed808c_idx",
        ),
        migrations.RunPython(copy_openai_messages, remove_copies),
    ]
