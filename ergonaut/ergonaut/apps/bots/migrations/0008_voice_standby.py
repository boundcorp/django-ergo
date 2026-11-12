from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ergonaut_bots", "0007_voice_bridge_started_at")]

    operations = [
        migrations.AddField(
            model_name="realtimevoicesession",
            name="standby_state",
            field=models.CharField(default="live", max_length=16),
        ),
        migrations.AddField(
            model_name="realtimevoicesession",
            name="standby_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="realtimevoicesession",
            name="resumed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="realtimevoicesession",
            name="bridge_claim_id",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
