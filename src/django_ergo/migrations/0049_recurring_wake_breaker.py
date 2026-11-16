from django.db import migrations
from django.db import models


def count_existing_invocations(apps, schema_editor):
    wake_model = apps.get_model("django_ergo", "RecurringWake")
    db = schema_editor.connection.alias
    # Dispatched work may have invoked the model, even with an unknown outcome.
    for wake in (
        wake_model.objects.using(db)
        .filter(status__in=["active", "paused", "blocked"], occurrences__gt=0)
        .iterator()
    ):
        wake_model.objects.using(db).filter(pk=wake.pk).update(
            invocations=min(12, wake.occurrences)
        )


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0048_merge_recurring_wakes_inference_costs")]

    operations = [
        migrations.AddField(
            model_name="recurringwake",
            name="invocations",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(count_existing_invocations, migrations.RunPython.noop),
    ]
