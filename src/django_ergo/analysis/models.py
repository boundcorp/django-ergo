"""Application receipts; bytes and parsing stay in isolated jobs."""

import uuid

from django.db import models


class AnalysisWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "django_ergo.ConversationSession", on_delete=models.CASCADE
    )
    retired = models.BooleanField(default=False)
    touched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session"],
                condition=models.Q(retired=False),
                name="one_active_analysis_workspace",
            )
        ]

    def __str__(self):
        return str(self.id)


class AnalysisJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE)
    operation = models.CharField(max_length=20)
    state = models.CharField(max_length=20, default="submitting")
    # Canonical bounded submission bytes permit idempotent retry after HTTP loss.
    request = models.BinaryField()
    # No full result is persisted in the app ledger.
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.id)


class AnalysisDataset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE)
    filename = models.CharField(max_length=200)
    version = models.CharField(max_length=64)
    schema = models.JSONField(default=list)
    rows = models.PositiveBigIntegerField()
    job = models.OneToOneField(AnalysisJob, on_delete=models.CASCADE)

    def __str__(self):
        return str(self.id)
