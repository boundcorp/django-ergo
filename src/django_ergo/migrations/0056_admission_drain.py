from django import VERSION as DJANGO_VERSION
from django.db import migrations
from django.db import models


def create_singleton(apps, schema_editor):
    apps.get_model("django_ergo", "AdmissionDrain").objects.using(
        schema_editor.connection.alias
    ).create(id=1)


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0055_merge_0054")]

    operations = [
        migrations.CreateModel(
            name="AdmissionDrain",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("owner", models.UUIDField(null=True)),
                ("expires_at", models.DateTimeField(null=True)),
                ("deadline", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        name="admission_drain_singleton",
                        **{
                            (
                                "condition" if DJANGO_VERSION >= (5, 1) else "check"
                            ): models.Q(id=1)
                        },
                    )
                ]
            },
        ),
        migrations.RunPython(create_singleton, migrations.RunPython.noop),
    ]
