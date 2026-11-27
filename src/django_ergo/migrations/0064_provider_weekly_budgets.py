# Provider identities/recurring consent start unapproved; existing caps remain cumulative.

import datetime as dt

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def original_weeks(apps, schema_editor):
    """Backfill chronology only, never infer billing identity, consent or zero usage."""
    Attempt = apps.get_model("django_ergo", "InferenceRequest")
    for row in Attempt.objects.using(schema_editor.connection.alias).all().iterator():
        created = row.created_at
        if created.tzinfo is not None:
            created = created.astimezone(dt.timezone.utc)
        day = created.date()
        # The ISO week (Monday 00:00 UTC start) the attempt was created in.
        Attempt.objects.using(schema_editor.connection.alias).filter(pk=row.pk).update(
            budget_week=day - dt.timedelta(days=day.weekday())
        )


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0063_google_engine_signatures"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="inferencerequest",
            name="budget_week",
            field=models.DateField(db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="inferencerequest",
            name="vendor",
            field=models.CharField(blank=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="spendfunding",
            name="budget_period",
            field=models.CharField(default="cumulative", max_length=24),
        ),
        migrations.AddField(
            model_name="spendgrant",
            name="budget_period",
            field=models.CharField(default="cumulative", max_length=24),
        ),
        migrations.CreateModel(
            name="ProviderAccountMapping",
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
                ("identity", models.CharField(max_length=1000, unique=True)),
                ("account_label", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assertion", models.CharField(max_length=200)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="inferencerequest",
            name="account_mapping",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="attempts",
                to="django_ergo.provideraccountmapping",
            ),
        ),
        migrations.CreateModel(
            name="ProviderBudgetFamily",
            fields=[
                (
                    "id",
                    models.CharField(max_length=16, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assertion", models.CharField(max_length=200)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="provideraccountmapping",
            name="family",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="accounts",
                to="django_ergo.providerbudgetfamily",
            ),
        ),
        migrations.AddField(
            model_name="inferencerequest",
            name="provider_family",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="attempts",
                to="django_ergo.providerbudgetfamily",
            ),
        ),
        migrations.CreateModel(
            name="ProviderWeeklyConsent",
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
                ("version", models.PositiveIntegerField()),
                ("weekly_usd", models.DecimalField(decimal_places=4, max_digits=16)),
                ("enabled", models.BooleanField(default=False)),
                ("valid_from", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("timezone", models.CharField(default="UTC", max_length=16)),
                ("note", models.CharField(max_length=200)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "family",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="consents",
                        to="django_ergo.providerbudgetfamily",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("family", "version"), name="provider_consent_version"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("weekly_usd__gte", 0), ("weekly_usd__lte", 100)
                        ),
                        name="provider_weekly_max_100",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("timezone", "UTC")),
                        name="provider_weekly_utc",
                    ),
                ],
            },
        ),
        migrations.RunPython(original_weeks, migrations.RunPython.noop),
    ]
