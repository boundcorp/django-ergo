import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_pre_permanence_model_can_insert_after_upgrade():
    old = [("django_ergo", "0052_hindsight_pr_sync")]
    new = [("django_ergo", "0054_permanence_initialized")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(old)
        old_apps = executor.loader.project_state(old).apps
        old_session = old_apps.get_model("django_ergo", "ConversationSession")
        old_user = old_apps.get_model("auth", "User")
        user = old_user.objects.create(username="pre-feature-writer")
        existing = old_session.objects.create(
            user_id=user.pk, engine_type="claude", status="active"
        )
        MigrationExecutor(connection).migrate(new)
        inserted = old_session.objects.create(
            user_id=user.pk, engine_type="claude", status="active"
        )
        new_apps = MigrationExecutor(connection).loader.project_state(new).apps
        session = new_apps.get_model("django_ergo", "ConversationSession")
        for pk in (existing.pk, inserted.pk):
            row = session.objects.get(pk=pk)
            assert row.is_permanent is False
            assert row.permanence_initialized is False
    finally:
        # Back to the latest schema, not just `new`: later tests in the same
        # database (any order) need every column.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
