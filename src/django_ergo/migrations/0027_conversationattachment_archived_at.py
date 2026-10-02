from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("django_ergo", "0026_openaimessage_images"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversationattachment",
            name="archived_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
