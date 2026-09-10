from django.db import migrations
from django.db import models
from pgvector.django import VectorField


def check_hierarchy_conflicts(apps, schema_editor):
    articles = apps.get_model("django_ergo", "Article").objects.using(
        schema_editor.connection.alias
    )
    conflicts = (
        articles.exclude(hierarchy_code=None)
        .values("knowledgebase_id", "hierarchy_code")
        .annotate(total=models.Count("pk"))
        .filter(total__gt=1)
    )
    if conflicts.exists():
        raise RuntimeError(
            "Duplicate Article hierarchy codes from the fs-vector schema require host reconciliation before migration 0013. No codes are reassigned automatically; source-only Articles may use NULL."
        )


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0012_merge_corpus_and_sources")]
    operations = [
        migrations.RunPython(check_hierarchy_conflicts, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="article", options={"ordering": ["hierarchy_code"]}
        ),
        migrations.AlterField(
            model_name="article",
            name="content_embedding",
            field=VectorField(
                blank=True,
                dimensions=1536,
                editable=False,
                help_text="Auto-generated embedding for content",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="article",
            name="summary_embedding",
            field=VectorField(
                blank=True,
                dimensions=1536,
                editable=False,
                help_text="Auto-generated embedding for summary",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="article",
            name="hierarchy_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="0",
                help_text="The hierarchy code of the article, e.g. '012' (0th chapter, 1st section, 2nd sub-section) or 'C3' (12th chapter, 3rd sub-section)",
                max_length=16,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="article",
            name="relative_path",
            field=models.CharField(
                db_index=True,
                default="",
                help_text="Optional source locator within its knowledgebase.",
                max_length=1024,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="article", unique_together={("knowledgebase", "hierarchy_code")}
        ),
        migrations.AddIndex(
            model_name="article",
            index=models.Index(
                fields=["knowledgebase", "hierarchy_code"],
                name="django_ergo_knowled_094e64_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="article",
            index=models.Index(
                fields=["hierarchy_code"], name="django_ergo_hierarc_b752b8_idx"
            ),
        ),
    ]
