import uuid

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0044_merge_wake_and_catalog")]

    operations = [
        migrations.CreateModel(
            name="LifecycleEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("key", models.CharField(max_length=255, unique=True)),
                ("stage", models.CharField(max_length=64)),
                ("occurred_at", models.DateTimeField()),
                ("observed_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "task_id",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                (
                    "thread_id",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                (
                    "turn_id",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                ("tool_id", models.CharField(blank=True, default="", max_length=128)),
                (
                    "worker_id",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                (
                    "question_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "message_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "related_thread_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "related_task_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("category", models.CharField(default="unknown", max_length=32)),
                ("actor", models.CharField(default="unknown", max_length=32)),
                ("state", models.CharField(default="unknown", max_length=32)),
                ("attempt", models.PositiveIntegerField(default=1)),
            ],
        ),
    ]
