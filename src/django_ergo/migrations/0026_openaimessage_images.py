from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0025_session_read_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="openaimessage",
            name="images",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
