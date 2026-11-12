import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ergonaut_bots", "0005_realtime_voice")]

    operations = [
        migrations.AddField(
            model_name="realtimevoiceevent", name="received_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="realtimevoiceevent", name="processed_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="realtimevoiceevent", name="client_event_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.CreateModel(
            name="RealtimeVoiceActivity",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("description", models.TextField()),
                (
                    "event",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activity",
                        to="ergonaut_bots.realtimevoiceevent",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="ergonaut_bots.realtimevoicesession",
                    ),
                ),
            ],
        ),
    ]
