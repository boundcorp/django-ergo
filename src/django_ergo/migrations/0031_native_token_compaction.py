"""Adopt native token compaction, retaining original policies for a real reverse.

The private metadata backup avoids a schema change and is written only on
changed rows. Re-runs leave it intact. Reverse restores policy keys only if
they still equal the migrated policy, preserving subsequent user edits.
No messages, tool results, summaries or call records are changed.
"""

from django.db import migrations

BACKUP = "_ergo_0031_compaction_policy"


def forward(apps, schema_editor):
    Session = apps.get_model("django_ergo", "ConversationSession")
    alias = schema_editor.connection.alias
    for session in (
        Session.objects.using(alias)
        .only("pk", "bot_name", "compaction_mode", "compaction_config", "metadata")
        .iterator(chunk_size=500)
    ):
        original = session.compaction_config
        config = dict(original) if isinstance(original, dict) else {}
        rolling = session.compaction_mode in ("rolling", "stream")
        window = bool(session.bot_name) and config.get("native_history") == "turn"
        if not (rolling or window):
            continue
        if window:
            config.pop("native_history", None)
        if rolling:
            for key in ("keep_recent", "batch", "min_tokens"):
                config.pop(key, None)
        metadata = dict(session.metadata) if isinstance(session.metadata, dict) else {}
        # Preserve an existing backup across interrupted or repeated runs.
        metadata.setdefault(
            BACKUP,
            {
                "mode": session.compaction_mode,
                "config": original,
                "applied_config": config,
                **(
                    {"original_metadata": session.metadata}
                    if not isinstance(session.metadata, dict)
                    else {}
                ),
            },
        )
        Session.objects.using(alias).filter(pk=session.pk).update(
            compaction_mode="context_size",
            compaction_config=config,
            metadata=metadata,
        )


def reverse(apps, schema_editor):
    Session = apps.get_model("django_ergo", "ConversationSession")
    alias = schema_editor.connection.alias
    for session in (
        Session.objects.using(alias)
        .only("pk", "bot_name", "compaction_mode", "compaction_config", "metadata")
        .iterator(chunk_size=500)
    ):
        metadata = session.metadata
        if not isinstance(metadata, dict) or BACKUP not in metadata:
            continue
        metadata = dict(metadata)
        backup = metadata.pop(BACKUP)
        fields = {
            "metadata": backup.get("original_metadata", metadata)
            if isinstance(backup, dict) and not metadata
            else metadata
        }
        if (
            isinstance(backup, dict)
            and session.compaction_mode == "context_size"
            and session.compaction_config == backup.get("applied_config")
        ):
            fields.update(
                compaction_mode=backup["mode"], compaction_config=backup["config"]
            )
        Session.objects.using(alias).filter(pk=session.pk).update(**fields)


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0030_engine_neutral_messages")]
    operations = [migrations.RunPython(forward, reverse)]
