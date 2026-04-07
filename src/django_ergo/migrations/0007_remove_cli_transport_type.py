"""Convert CLI transport sessions to API and remove CLI transport choice."""

from django.db import migrations
from django.db import models


def convert_cli_to_api(apps, schema_editor):
    ConversationSession = apps.get_model("django_ergo", "ConversationSession")
    ConversationSession.objects.filter(transport_type="cli").update(
        transport_type="api"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0006_conversationkbusage"),
    ]

    operations = [
        # Data migration: convert any existing CLI sessions to API
        migrations.RunPython(convert_cli_to_api, migrations.RunPython.noop),
        # Schema migration: update choices to remove CLI option
        migrations.AlterField(
            model_name="conversationsession",
            name="transport_type",
            field=models.CharField(
                choices=[("api", "API")],
                default="api",
                max_length=10,
            ),
        ),
    ]
