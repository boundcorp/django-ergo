from django.db import migrations
from django.db import models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("ergo_knowledge", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="CorpusHead",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("collection_id", models.CharField(max_length=255)),
                ("scope", models.CharField(max_length=255)),
                ("revision", models.CharField(max_length=64)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("collection_id", "scope"), name="ergo_corpus_head_unique")]},
        ),
        migrations.CreateModel(
            name="CorpusOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payload", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("head", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="operations", to="ergo_knowledge.corpushead")),
            ],
        ),
    ]
