from django.db import migrations
from django.db import models


class Migration(migrations.Migration):

    dependencies = [
        ("django_ergo", "0036_native_token_compaction"),
    ]

    operations = [
        migrations.CreateModel(
            name="UsageSync",
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
                ("attempted_at", models.FloatField(blank=True, null=True)),
                ("succeeded_at", models.FloatField(blank=True, null=True)),
                ("running_since", models.FloatField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("accounts", models.JSONField(blank=True, default=dict)),
            ],
        ),
    ]
