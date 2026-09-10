from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("ergo_knowledge", "0002_corpus_writes")]
    operations = [
        migrations.CreateModel(
            name="CorpusUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("collection_id", models.CharField(max_length=255)),
                ("scope", models.CharField(max_length=255)),
                ("context_id", models.CharField(blank=True, max_length=255)),
                ("event_id", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("collection_id", "scope", "event_id"), name="ergo_corpus_usage_unique")],
                "indexes": [models.Index(fields=["collection_id", "scope", "context_id"], name="ergo_corpus_usage_context")],
            },
        ),
    ]
