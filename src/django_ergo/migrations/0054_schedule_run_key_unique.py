from django.db import migrations
from django.db import models


def backfill_keys(apps, schema_editor):
    """Runs from before ids existed were keyed by their name."""
    apps.get_model("django_ergo", "ScheduleRun").objects.filter(
        schedule_key=""
    ).update(schedule_key=models.F("schedule"))


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0053_schedule_run_key"),
    ]

    operations = [
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="schedulerun", name="unique_schedule_run"
        ),
        migrations.AddConstraint(
            model_name="schedulerun",
            constraint=models.UniqueConstraint(
                fields=["bot_name", "schedule_key", "user", "minute"],
                name="unique_schedule_run",
            ),
        ),
    ]
