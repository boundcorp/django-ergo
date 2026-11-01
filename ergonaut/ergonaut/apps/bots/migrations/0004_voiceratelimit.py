import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ergonaut_bots", "0003_illustrationjob_topics"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="VoiceRateLimit",
            fields=[
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("minute", models.BigIntegerField(default=0)),
                ("minute_count", models.PositiveIntegerField(default=0)),
                ("day", models.BigIntegerField(default=0)),
                ("day_count", models.PositiveIntegerField(default=0)),
            ],
        ),
    ]
