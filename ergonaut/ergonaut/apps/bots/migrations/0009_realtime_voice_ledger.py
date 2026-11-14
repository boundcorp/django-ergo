import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ergonaut_bots", "0008_voice_standby")]

    operations = [
        migrations.CreateModel(
            name="RealtimeVoiceLedgerEntry",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("namespace", models.CharField(max_length=16)),
                ("key_sha256", models.CharField(max_length=64)),
                ("identity", models.TextField()),
                ("data", models.JSONField(default=dict)),
                ("pending", models.BooleanField(default=False)),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="native_ledger",
                        to="ergonaut_bots.realtimevoicesession",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["session", "namespace", "-id"], name="voice_ledger_recent"),
                    models.Index(fields=["session", "pending"], name="voice_ledger_pending"),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("session", "namespace", "key_sha256"), name="unique_voice_ledger_identity"
                    ),
                ],
            },
        ),
    ]
