import django.db.models.deletion
from django.conf import settings
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("django_ergo", "0031_provider_usage_routing_policy"),
    ]

    operations = [
        migrations.CreateModel(
            name="RoutingText",
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
                ("text", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RoutingSwitch",
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
                ("label", models.CharField(blank=True, default="", max_length=300)),
                ("tier", models.CharField(max_length=20)),
                (
                    "from_model",
                    models.CharField(blank=True, default="", max_length=200),
                ),
                ("to_model", models.CharField(max_length=200)),
                ("reason", models.CharField(blank=True, default="", max_length=300)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "session",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="routing_switches",
                        to="django_ergo.conversationsession",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
