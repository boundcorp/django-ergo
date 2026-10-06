"""Adding message identities does not reinterpret or rewrite legacy words."""

import pytest
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("django_ergo", "0033_routing_text_switch")]
AFTER = [("django_ergo", "0034_sessionmessage_identity")]


@pytest.mark.django_db(transaction=True)
def test_legacy_message_content_survives_identity_migration():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        apps = executor.loader.project_state(BEFORE).apps
        user = apps.get_model(settings.AUTH_USER_MODEL).objects.create(
            username="legacy-forward"
        )
        session = apps.get_model("django_ergo", "ConversationSession").objects.create(
            user=user, engine_type="claude", transport_type="api", status="active"
        )
        message = apps.get_model("django_ergo", "SessionMessage").objects.create(
            session=session, role="user", sequence=0
        )
        words = "[Forwarded by kitchen · Main: legacy attribution]\n\nKeep my words."
        apps.get_model("django_ergo", "MessageBlock").objects.create(
            message=message, block_type="text", text=words, sequence=0
        )
        executor = MigrationExecutor(connection)
        executor.migrate(AFTER)
        apps = executor.loader.project_state(AFTER).apps
        saved = apps.get_model("django_ergo", "SessionMessage").objects.get(
            pk=message.pk
        )
        assert saved.author == {}
        assert saved.provenance == {}
        assert saved.role == "user"
        assert saved.content_blocks.get().text == words
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
