from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="CorpusRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("collection_id", models.CharField(max_length=255)),
                ("scope", models.CharField(max_length=255)),
                ("revision", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("collection_id", "scope", "revision"), name="ergo_corpus_revision_unique")]},
        ),
    ]
