"""Rename the "stream" compaction mode to "rolling"."""

from django.db import migrations
from django.db import models

MODES = [
    ("none", "None"),
    ("time", "Time-based"),
    ("context_size", "Context size"),
    ("rolling", "Rolling"),
]
FIELDS = {"ConversationSession": "compaction_mode", "ConversationCompaction": "mode"}


def _rename(old, new):
    def run(apps, schema_editor):
        for name, field in FIELDS.items():
            model = apps.get_model("django_ergo", name)
            model.objects.filter(**{field: old}).update(**{field: new})

    return run


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0027_conversationattachment_archived_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversationsession",
            name="compaction_mode",
            field=models.CharField(choices=MODES, default="none", max_length=20),
        ),
        migrations.AlterField(
            model_name="conversationcompaction",
            name="mode",
            field=models.CharField(choices=MODES, max_length=20),
        ),
        migrations.RunPython(
            _rename("stream", "rolling"), _rename("rolling", "stream")
        ),
    ]
