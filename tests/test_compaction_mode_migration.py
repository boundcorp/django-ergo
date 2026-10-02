"""The "stream" compaction mode is renamed to "rolling" in stored rows."""

import pytest
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("django_ergo", "0027_conversationattachment_archived_at")]
AFTER = [("django_ergo", "0028_rolling_compaction_mode")]


@pytest.mark.django_db(transaction=True)
def test_stream_rows_become_rolling():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        apps = executor.loader.project_state(BEFORE).apps
        user = apps.get_model(settings.AUTH_USER_MODEL).objects.create(
            username="legacy-stream"
        )
        session = apps.get_model("django_ergo", "ConversationSession").objects.create(
            user=user,
            engine_type="claude",
            transport_type="api",
            status="active",
            compaction_mode="stream",
        )
        apps.get_model("django_ergo", "ConversationCompaction").objects.create(
            session=session,
            mode="stream",
            from_sequence=0,
            upto_sequence=3,
            summary="old",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        apps = executor.loader.project_state(AFTER).apps
        session_model = apps.get_model("django_ergo", "ConversationSession")
        compaction_model = apps.get_model("django_ergo", "ConversationCompaction")
        assert session_model.objects.get(pk=session.pk).compaction_mode == "rolling"
        assert compaction_model.objects.get(session_id=session.pk).mode == "rolling"
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
