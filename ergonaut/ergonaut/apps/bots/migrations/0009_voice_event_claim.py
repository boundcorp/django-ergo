from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ergonaut_bots", "0008_voice_standby")]

    operations = [
        migrations.AddField(
            model_name="realtimevoiceevent",
            name="claim_id",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
