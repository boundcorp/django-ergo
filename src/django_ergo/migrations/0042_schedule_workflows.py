from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0041_session_user_title")]

    operations = [
        migrations.AddField(
            model_name="schedulerun",
            name="status",
            field=models.CharField(default="pending", max_length=20),
        ),
        migrations.AddField(
            model_name="schedulerun",
            name="steps",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="schedulerun",
            name="error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="schedulerun",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="schedulerun",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
