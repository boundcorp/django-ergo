import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ergonaut_bots", "0004_voiceratelimit"),
        ("django_ergo", "0034_sessionmessage_identity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RealtimeVoiceSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(default="active", max_length=12)),
                ("state", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "chat",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="voice_sessions",
                        to="django_ergo.conversationsession",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="realtime_voice_sessions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RealtimeVoiceEvent",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=200)),
                ("kind", models.CharField(max_length=40)),
                ("delivery", models.CharField(default="unknown", max_length=20)),
                ("text", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="ergonaut_bots.realtimevoicesession",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("session", "event_id"), name="unique_realtime_voice_event")
                ]
            },
        ),
        migrations.CreateModel(
            name="RealtimeVoiceDelegation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=200)),
                ("text", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="voice_delegation",
                        to="django_ergo.threadmessage",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="delegations",
                        to="ergonaut_bots.realtimevoicesession",
                    ),
                ),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("session", "event_id"), name="unique_voice_delegation")]
            },
        ),
    ]
