"""Default-on upgrade, old writers, and persistent owner opt-outs."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_reply_updates_upgrade_enables_existing_and_new_chats_once():
    before = [("django_ergo", "0065_provider_weekly_budgets")]
    after = [("django_ergo", "0066_reply_updates")]
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        owner = old.get_model("auth", "User").objects.create(username="upgrade-owner")
        old_session = old.get_model("django_ergo", "ConversationSession")
        existing = old_session.objects.create(user_id=owner.pk, engine_type="claude")
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        session = executor.loader.project_state(after).apps.get_model(
            "django_ergo", "ConversationSession"
        )
        assert session.objects.get(pk=existing.pk).reply_updates is True
        assert session.objects.create(user_id=owner.pk).reply_updates is True
        # An old process omits the column; its insert uses the database default.
        inserted = old_session.objects.create(user_id=owner.pk, engine_type="claude")
        assert session.objects.get(pk=inserted.pk).reply_updates is True
        session.objects.filter(pk=existing.pk).update(reply_updates=False)
        MigrationExecutor(connection).migrate(after)
        assert session.objects.get(pk=existing.pk).reply_updates is False
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
