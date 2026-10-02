from django.db import migrations


def root_to_main(apps, schema_editor):
    ConversationSession = apps.get_model("django_ergo", "ConversationSession")
    for session in ConversationSession.objects.filter(metadata__bot_role="root"):
        session.metadata = {**session.metadata, "bot_role": "main"}
        session.save(update_fields=["metadata"])


def main_to_root(apps, schema_editor):
    ConversationSession = apps.get_model("django_ergo", "ConversationSession")
    for session in ConversationSession.objects.filter(metadata__bot_role="main"):
        session.metadata = {**session.metadata, "bot_role": "root"}
        session.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    """Bots' root chats are now called main chats."""

    dependencies = [("django_ergo", "0018_schedule_runs")]

    operations = [migrations.RunPython(root_to_main, main_to_root)]
