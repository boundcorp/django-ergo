"""Realistic policy migration fixtures: history stays byte-for-byte intact."""

import importlib

import pytest
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("django_ergo", "0030_engine_neutral_messages")]
AFTER = [("django_ergo", "0031_native_token_compaction")]


@pytest.mark.django_db(transaction=True)
def test_native_policy_forward_reverse_and_rerun():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        apps = executor.loader.project_state(BEFORE).apps
        user = apps.get_model(settings.AUTH_USER_MODEL).objects.create(
            username="native-migration"
        )
        session_model = apps.get_model("django_ergo", "ConversationSession")
        message_model = apps.get_model("django_ergo", "SessionMessage")
        block_model = apps.get_model("django_ergo", "MessageBlock")
        cases = [
            (
                "octo",
                "none",
                {"native_history": "turn"},
                "active",
                {"bot_role": "main"},
            ),
            (
                "kitchen",
                "rolling",
                {
                    "keep_recent": 15,
                    "batch": 10,
                    "min_tokens": 0,
                    "native_history": "turn",
                    "other": [1, None],
                },
                "paused",
                {"bot_role": "chat", "chat": "meals"},
            ),
            ("octo", "context_size", {}, "active", {"bot_role": "main"}),
            ("", "none", {"native_history": "turn"}, "active", {}),
            (
                "",
                "stream",
                {
                    "keep_recent": -1,
                    "batch": None,
                    "min_tokens": "odd",
                    "max_context_tokens": 4000,
                },
                "completed",
                {"custom": True},
            ),
            (
                "empty",
                "none",
                {"native_history": "turn", "keep_recent": 8},
                "active",
                {},
            ),
            ("odd", "rolling", ["invalid", None], "failed", {}),
            ("kitchen", "time", {"idle_seconds": 300}, "active", {}),
        ]
        originals = []
        for bot, mode, config, status, metadata in cases:
            session = session_model.objects.create(
                user=user,
                bot_name=bot,
                compaction_mode=mode,
                compaction_config=config,
                status=status,
                metadata=metadata,
            )
            originals.append((session.pk, mode, config, metadata))
        # A long main chat with real tools, mixed usage and large outputs.
        for sequence in range(120):
            message = message_model.objects.create(
                session_id=originals[0][0],
                role="user" if sequence % 4 in (0, 2) else "assistant",
                sequence=sequence,
                input_tokens=sequence * 10,
            )
            kind = ("text", "tool_use", "tool_result", "text")[sequence % 4]
            block_model.objects.create(
                message=message,
                sequence=0,
                block_type=kind,
                text=f"message {sequence}",
                tool_name="read_file" if kind == "tool_use" else "",
                tool_use_id=f"t{sequence // 4}" if kind == "tool_use" else "",
                tool_result_for=f"t{sequence // 4}" if kind == "tool_result" else "",
                tool_result_content="/repo/path.py\n" + "x" * 20000
                if kind == "tool_result"
                else "",
            )
        messages = list(message_model.objects.values())
        blocks = list(block_model.objects.values())
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        apps = executor.loader.project_state(AFTER).apps
        session_model = apps.get_model("django_ergo", "ConversationSession")
        expected_configs = [
            {},
            {"other": [1, None]},
            {},
            {"native_history": "turn"},
            {"max_context_tokens": 4000},
            {"keep_recent": 8},
            {},
            {"idle_seconds": 300},
        ]
        for i, (pk, _, _, _) in enumerate(originals):
            session = session_model.objects.get(pk=pk)
            assert session.compaction_config == expected_configs[i]
            assert session.compaction_mode == (
                "none" if i == 3 else "time" if i == 7 else "context_size"
            )
        migrated = list(session_model.objects.order_by("pk").values())
        module = importlib.import_module(
            "django_ergo.migrations.0031_native_token_compaction"
        )
        with connection.schema_editor() as editor:
            module.forward(apps, editor)
            module.forward(apps, editor)
        assert list(session_model.objects.order_by("pk").values()) == migrated
        assert list(message_model.objects.values()) == messages
        assert list(block_model.objects.values()) == blocks
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        for pk, mode, config, metadata in originals:
            session = session_model.objects.get(pk=pk)
            assert (
                session.compaction_mode,
                session.compaction_config,
                session.metadata,
            ) == (mode, config, metadata)
        with connection.schema_editor() as editor:
            module.reverse(apps, editor)
        for pk, _mode, config, _ in originals:
            assert session_model.objects.get(pk=pk).compaction_config == config
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        assert list(session_model.objects.order_by("pk").values()) == migrated
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_reverse_preserves_later_policy_edits_and_non_dict_metadata():
    module = importlib.import_module(
        "django_ergo.migrations.0031_native_token_compaction"
    )
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        apps = executor.loader.project_state(BEFORE).apps
        user = apps.get_model(settings.AUTH_USER_MODEL).objects.create(
            username="edited-migration"
        )
        session_model = apps.get_model("django_ergo", "ConversationSession")
        edited = session_model.objects.create(
            user=user,
            bot_name="octo",
            compaction_mode="rolling",
            compaction_config={"keep_recent": 15},
            metadata={"title": "Keep me"},
        )
        odd = session_model.objects.create(
            user=user,
            bot_name="odd",
            compaction_mode="stream",
            compaction_config={"batch": 3},
            metadata=["legacy", None],
        )
        with connection.schema_editor() as editor:
            module.forward(apps, editor)
        session_model.objects.filter(pk=edited.pk).update(
            compaction_config={"keep_tokens": 10000}
        )
        edited = session_model.objects.get(pk=edited.pk)
        edited.metadata["title"] = "New title"
        edited.save(update_fields=["metadata"])
        with connection.schema_editor() as editor:
            module.reverse(apps, editor)
            module.reverse(apps, editor)
        edited.refresh_from_db()
        odd.refresh_from_db()
        assert edited.compaction_mode == "context_size"
        assert edited.compaction_config == {"keep_tokens": 10000}
        assert edited.metadata == {"title": "New title"}
        assert odd.metadata == ["legacy", None]
        assert odd.compaction_mode == "stream"
        assert odd.compaction_config == {"batch": 3}
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
