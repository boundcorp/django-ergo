from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0033_routing_text_switch"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionmessage",
            name="author",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="sessionmessage",
            name="provenance",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
