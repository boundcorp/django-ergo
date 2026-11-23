from django.db import migrations
from django.db import models
from django.db.models import Q


def mark_configured_chats(apps, schema_editor):
    session = apps.get_model("django_ergo", "ConversationSession")
    session.objects.filter(
        Q(metadata__bot_role__in=["main", "chat"])
        | Q(metadata__has_key="telegram_thread"),
        bot_name__gt="",
    ).update(is_permanent=True)


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0052_hindsight_pr_sync")]

    operations = [
        migrations.AddField(
            model_name="conversationsession",
            name="is_permanent",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_configured_chats, migrations.RunPython.noop),
    ]
