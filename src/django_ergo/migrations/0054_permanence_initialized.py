from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0053_permanent_chats")]

    operations = [
        migrations.AddField(
            model_name="conversationsession",
            name="permanence_initialized",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
