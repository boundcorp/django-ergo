import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "starting",
    [
        "0009_knowledgebase_organization_strategy",
        "0010_article_status",
        "0010_knowledgesource_sourcedocument_alter_article_options_and_more",
        "0011_knowledgesource_index_config_hash_and_more",
    ],
)
def test_existing_article_upgrade_preserves_identity_hierarchy_and_content(starting):
    previous = [("django_ergo", starting)]
    current = [("django_ergo", "0013_reconcile_article_compatibility")]
    executor = MigrationExecutor(connection)
    executor.migrate([("django_ergo", "0009_knowledgebase_organization_strategy")])
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
        source = None
        if starting.startswith("0011") or "knowledgesource_sourcedocument" in starting:
            source = historical.get_model(
                "django_ergo", "KnowledgeSource"
            ).objects.create(
                knowledgebase=knowledgebase,
                repository_alias="not-accessed",
                last_synced_commit="a" * 40,
            )
        unit = None
        if starting.startswith("0011"):
            source_file = historical.get_model(
                "django_ergo", "SourceFile"
            ).objects.create(
                source=source,
                relative_path="old.py",
                git_blob_oid="b" * 40,
                language="python",
                source_role="runtime",
                content_hash="c" * 64,
                last_indexed_commit="a" * 40,
            )
            unit = historical.get_model("django_ergo", "SourceUnit").objects.create(
                source_file=source_file,
                unit_key="target",
                kind="function",
                start_line=1,
                end_line=2,
                lexical_text="target",
                evidence_text="Retained",
                content_hash="d" * 64,
                embedding_input_hash="e" * 64,
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
        if starting.startswith("0011") or "knowledgesource_sourcedocument" in starting:
            assert migrated.relative_path == ""
            preserved = (
                executor.loader.project_state(current)
                .apps.get_model("django_ergo", "KnowledgeSource")
                .objects.get(pk=source.pk)
            )
            assert preserved.last_synced_commit == "a" * 40
        if unit is not None:
            preserved = (
                executor.loader.project_state(current)
                .apps.get_model("django_ergo", "SourceUnit")
                .objects.get(pk=unit.pk)
            )
            assert preserved.evidence_text == "Retained"
    finally:
        MigrationExecutor(connection).migrate(current)
