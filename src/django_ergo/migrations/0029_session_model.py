"""ConversationSession.model: the chat's picked model, moved out of metadata["model"]."""

from django.db import migrations
from django.db import models


def forwards(apps, schema_editor):
    sessions = apps.get_model("django_ergo", "ConversationSession")
    for session in sessions.objects.filter(metadata__has_key="model").only(
        "id", "metadata"
    ):
        metadata = dict(session.metadata or {})
        session.model = str(metadata.pop("model") or "")
        session.metadata = metadata
        session.save(update_fields=["model", "metadata"])


def backwards(apps, schema_editor):
    sessions = apps.get_model("django_ergo", "ConversationSession")
    for session in sessions.objects.exclude(model="").only("id", "model", "metadata"):
        session.metadata = {**(session.metadata or {}), "model": session.model}
        session.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0028_rolling_compaction_mode")]

    operations = [
        migrations.AddField(
            model_name="conversationsession",
            name="model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.RunPython(forwards, backwards),
    ]
