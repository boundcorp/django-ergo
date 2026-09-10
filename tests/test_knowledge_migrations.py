import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_existing_article_upgrade_preserves_identity_hierarchy_and_content():
    previous = [("django_ergo", "0009_knowledgebase_organization_strategy")]
    current = [("django_ergo", "0010_article_status")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    try:
        historical = executor.loader.project_state(previous).apps
        knowledgebase = historical.get_model(
            "django_ergo", "Knowledgebase"
        ).objects.create(name="Existing virtual KB")
        article = historical.get_model("django_ergo", "Article").objects.create(
            knowledgebase=knowledgebase,
            hierarchy_code="A1",
            title="Existing",
            content="No filesystem required",
        )
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        migrated = (
            executor.loader.project_state(current)
            .apps.get_model("django_ergo", "Article")
            .objects.get(pk=article.pk)
        )
        assert migrated.status == "active"
        assert migrated.hierarchy_code == "A1"
        assert migrated.content == "No filesystem required"
        assert migrated.knowledgebase_id == knowledgebase.pk
    finally:
        MigrationExecutor(connection).migrate(current)
