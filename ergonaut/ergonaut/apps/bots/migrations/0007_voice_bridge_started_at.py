from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ergonaut_bots", "0006_voice_timing")]

    operations = [
        migrations.AddField(
            model_name="realtimevoicesession",
            name="bridge_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
