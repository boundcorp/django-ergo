from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0042_schedule_workflows")]

    operations = [
        migrations.CreateModel(
            name="ModelCatalogSnapshot",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("scope", models.CharField(max_length=64, unique=True)),
                ("coverage", models.JSONField(blank=True, default=list)),
                ("models", models.JSONField(blank=True, default=dict)),
            ],
        ),
    ]
