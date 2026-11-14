import uuid

import django.db.models.deletion
from django import VERSION as DJANGO_VERSION
from django.db import migrations
from django.db import models


def _constraint(condition, name):
    keyword = "condition" if DJANGO_VERSION >= (5, 1) else "check"
    return models.CheckConstraint(name=name, **{keyword: condition})


class Migration(migrations.Migration):
    dependencies = [("django_ergo", "0046_cost_ledger")]

    operations = [
        migrations.AddField(
            model_name="worker",
            name="recurring_origin",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="RecurringWakeLock",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
            ],
            options={
                "constraints": [_constraint(models.Q(id=1), "recurring_wake_single_lock")],
            },
        ),
        migrations.CreateModel(
            name="RecurringWake",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("bot_name", models.CharField(max_length=100)),
                ("note", models.TextField()),
                ("status", models.CharField(choices=[("active", "Active"), ("paused", "Paused"), ("blocked", "Blocked (explicit resume required)"), ("cancelled", "Cancelled"), ("completed", "Completed")], default="active", max_length=20)),
                ("interval_seconds", models.PositiveIntegerField()),
                ("next_run_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("max_occurrences", models.PositiveIntegerField(blank=True, null=True)),
                ("occurrences", models.PositiveIntegerField(default=0)),
                ("periodic_checkins", models.BooleanField(default=False)),
                ("blocked_reason", models.TextField(blank=True, default="")),
                ("depth", models.PositiveIntegerField(default=0)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recurring_wakes", to="django_ergo.conversationsession")),
            ],
            options={
                "ordering": ["created_at", "id"],
                "indexes": [
                    models.Index(fields=["status", "next_run_at"], name="recurring_wake_due_idx"),
                    models.Index(fields=["session", "status"], name="recurring_wake_session_idx"),
                ],
                "constraints": [
                    _constraint(models.Q(expires_at__isnull=False) | models.Q(max_occurrences__isnull=False), "recurring_wake_finite"),
                    _constraint(models.Q(interval_seconds__gte=1), "recurring_wake_interval"),
                    _constraint(models.Q(max_occurrences__isnull=True) | models.Q(max_occurrences__gte=1), "recurring_wake_positive_max"),
                ],
            },
        ),
        migrations.CreateModel(
            name="RecurringWakeOccurrence",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("scheduled_for", models.DateTimeField()),
                ("dispatched_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("delivered", "Delivered"), ("waiting", "Waiting for approval"), ("answered", "Answered"), ("failed", "Failed (not retried)"), ("cancelled", "Cancelled before delivery")], default="queued", max_length=20)),
                ("error", models.TextField(blank=True, default="")),
                ("schedule", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scheduled_occurrences", to="django_ergo.recurringwake")),
                ("thread_message", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="recurring_wake_occurrence", to="django_ergo.threadmessage")),
            ],
            options={
                "ordering": ["scheduled_for", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("schedule", "scheduled_for"), name="recurring_wake_unique_slot"),
                    models.UniqueConstraint(condition=models.Q(status__in=["queued", "delivered", "waiting"]), fields=("schedule",), name="recurring_wake_one_outstanding"),
                ],
            },
        ),
    ]
