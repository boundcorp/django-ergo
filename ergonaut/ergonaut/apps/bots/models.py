"""What people did through bot pages."""

import uuid

from django.conf import settings
from django.db import models


class PageActionCall(models.Model):
    """One run of a bot's page action (``@page_action``), called from a page by a viewer.

    Calls that ran are recorded, whether they worked or not; calls refused before the
    function ran (unknown action, bad arguments, approval asked for) are not. When the
    page was opened from a chat, the bot sees its latest calls on its next turn (see
    ``ergonaut.apps.bots.pageactions``).
    """

    bot = models.CharField(max_length=200, db_index=True)
    action = models.CharField(max_length=200)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="page_action_calls")
    session = models.ForeignKey(
        "django_ergo.ConversationSession",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="page_action_calls",
    )
    page = models.CharField(max_length=500, blank=True)  # a bot-folder path or a chat file's id
    args = models.JSONField(default=dict, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    approved = models.BooleanField(default=False)  # the viewer confirmed it first
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["session", "created_at"])]

    def __str__(self):
        return f"{self.user} ran {self.bot}.{self.action}"

    @property
    def ok(self) -> bool:
        return not self.error


class IllustrationJob(models.Model):
    """A durable request to generate one static chat illustration."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        RETRYING = "retrying", "Retrying"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "django_ergo.ConversationSession", on_delete=models.CASCADE, related_name="illustration_jobs"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="illustration_jobs")
    request_key = models.CharField(max_length=64)
    snapshot_digest = models.CharField(max_length=64)
    as_of_line = models.IntegerField()
    evidence = models.JSONField()
    selected = models.JSONField(default=list)
    custom = models.TextField(blank=True)
    # The selected suggestions as shown to the user (id, title, purpose, kind) plus the free-form request.
    topics = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=Status, default=Status.QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    task_id = models.CharField(max_length=100, blank=True)
    run_token = models.UUIDField(default=uuid.uuid4, editable=False)
    attachment = models.ForeignKey(
        "django_ergo.ConversationAttachment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    summary = models.CharField(max_length=500, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["session", "created_at"])]
        constraints = [
            models.UniqueConstraint(fields=["session", "user", "request_key"], name="unique_illustration_job")
        ]


class VoiceRateLimit(models.Model):
    """Per-user fixed-window transcription counters; no audio or transcript is stored."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True)
    minute = models.BigIntegerField(default=0)
    minute_count = models.PositiveIntegerField(default=0)
    day = models.BigIntegerField(default=0)
    day_count = models.PositiveIntegerField(default=0)


class RealtimeVoiceSession(models.Model):
    """A logical voice conversation, independent of transport reconnects."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey("django_ergo.ConversationSession", on_delete=models.CASCADE, related_name="voice_sessions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="realtime_voice_sessions")
    status = models.CharField(max_length=12, default="active")
    state = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    # Root wall-clock deadline anchor, never reset by a resumed connection.
    bridge_started_at = models.DateTimeField(null=True, blank=True)
    standby_state = models.CharField(max_length=16, default="live")
    standby_at = models.DateTimeField(null=True, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    bridge_claim_id = models.CharField(max_length=32, blank=True, default="")


class RealtimeVoiceEvent(models.Model):
    session = models.ForeignKey(RealtimeVoiceSession, on_delete=models.CASCADE, related_name="events")
    event_id = models.CharField(max_length=200)
    kind = models.CharField(max_length=40)
    text = models.TextField()
    delivery = models.CharField(max_length=20, default="unknown")
    created_at = models.DateTimeField(auto_now_add=True)
    received_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    client_event_at = models.DateTimeField(null=True, blank=True)
    # Bridge claim that recorded this turn; a new claim is a reconnect boundary in the transcript.
    claim_id = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "event_id"], name="unique_realtime_voice_event")]


class RealtimeVoiceActivity(models.Model):
    """A user-reported action, anchored to when its report reached Django."""

    session = models.ForeignKey(RealtimeVoiceSession, on_delete=models.CASCADE, related_name="activities")
    event = models.OneToOneField(RealtimeVoiceEvent, on_delete=models.CASCADE, related_name="activity")
    description = models.TextField()


class RealtimeVoiceDelegation(models.Model):
    session = models.ForeignKey(RealtimeVoiceSession, on_delete=models.CASCADE, related_name="delegations")
    event_id = models.CharField(max_length=200)
    text = models.TextField()
    message = models.OneToOneField(
        "django_ergo.ThreadMessage", on_delete=models.CASCADE, related_name="voice_delegation"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "event_id"], name="unique_voice_delegation")]
