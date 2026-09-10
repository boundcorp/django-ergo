from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0013_reconcile_article_compatibility")]
    operations = [
        migrations.AlterUniqueTogether(name="article", unique_together=set()),
        migrations.AlterModelOptions(
            name="article", options={"ordering": ["relative_path", "id"]}
        ),
        migrations.AlterField(
            model_name="article",
            name="hierarchy_code",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="0",
                help_text="Legacy compatibility code; not a path or unique identity.",
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
                help_text="Primary logical path within its knowledgebase; empty means explicitly unmapped.",
                max_length=1024,
            ),
        ),
    ]
