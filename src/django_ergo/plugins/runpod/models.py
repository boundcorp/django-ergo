"""Durable authorization/ownership index, independent of chat/Worker lifetime.

The existing per-rental Experiment ledger retains provider receipts, uncertainty,
stop intent and exports. This index serializes admission across those ledgers.
"""

import uuid

from django.db import models


class RunpodAccount(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    installation = models.UUIDField(default=uuid.uuid4, editable=False)
    maintenance_at = models.FloatField(null=True)
    maintenance_error = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.key


class RunpodJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(RunpodAccount, on_delete=models.PROTECT)
    bot_name = models.CharField(max_length=100)
    owner_id = models.CharField(max_length=255)
    session_identity = models.UUIDField()
    session = models.ForeignKey(
        "django_ergo.ConversationSession",
        null=True,
        on_delete=models.SET_NULL,
    )
    worker = models.OneToOneField(
        "django_ergo.Worker",
        null=True,
        on_delete=models.SET_NULL,
    )
    request_key = models.CharField(max_length=200)
    spec_hash = models.CharField(max_length=64)
    profile = models.JSONField()
    ledger_path = models.TextField()
    reservation_usd = models.DecimalField(max_digits=12, decimal_places=2)
    created = models.FloatField()
    deadline = models.FloatField()
    # Kept charged against the installation cap after termination until actual
    # billing is reconciled by an operator; restarting cannot top up the cap.
    cleanup_complete = models.BooleanField(default=False)
    manifest = models.JSONField(default=dict)
    error = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "bot_name", "owner_id", "request_key"],
                name="runpod_request_identity",
            ),
        ]

    def __str__(self):
        return f"{self.bot_name} RunPod {self.pk}"


class RunpodArtifact(models.Model):
    job = models.ForeignKey(RunpodJob, on_delete=models.PROTECT)
    name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    attachment = models.ForeignKey(
        "django_ergo.ConversationAttachment",
        null=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "name"], name="runpod_artifact_identity"
            ),
        ]

    def __str__(self):
        return f"{self.job_id} {self.name}"
