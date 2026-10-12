from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0037_usage_sync"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuthorProfile",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("kind", models.CharField(max_length=40)),
                ("ref", models.CharField(max_length=200)),
                (
                    "display_name",
                    models.CharField(blank=True, default="", max_length=300),
                ),
                ("username", models.CharField(blank=True, default="", max_length=200)),
                ("avatar", models.BinaryField(blank=True, null=True)),
                (
                    "avatar_type",
                    models.CharField(blank=True, default="", max_length=100),
                ),
                (
                    "avatar_source",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                ("avatar_checked_at", models.DateTimeField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "ref"), name="author_profile_kind_ref"
                    )
                ],
            },
        ),
    ]
