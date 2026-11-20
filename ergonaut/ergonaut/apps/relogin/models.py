"""Audit of self-service re-logins (docs/relogin.md).

One row per session. It never holds the sign-in URL, the device code, the pasted code or
redirect URL, or the CLI's output: those live in the worker's memory and, for the page, in
short-lived Redis keys (``handoff``)."""

import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q


def _session_id() -> str:
    return secrets.token_urlsafe(24)


class ReloginSession(models.Model):
    STATES = ["starting", "waiting", "submitted", "checking", "ended"]
    OUTCOMES = ["ok", "failed", "expired", "cancelled"]

    id = models.CharField(primary_key=True, max_length=64, default=_session_id, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    bot_name = models.CharField(max_length=80)
    store = models.CharField(max_length=80)  # the bot's store key, e.g. omp/anthropic
    command = models.CharField(max_length=80)  # the fixed command from the allow-list
    shape = models.CharField(max_length=20)  # device_code, paste_code, paste_redirect
    # Stores that need the same local port share a group; only one may be active.
    port_group = models.CharField(max_length=20, blank=True)
    state = models.CharField(max_length=20, default="starting")
    outcome = models.CharField(max_length=20, blank=True)  # blank while active
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)  # the one accepted submission
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    # Our own wording only (never CLI output): why it failed, and what to run by hand.
    error = models.CharField(max_length=300, blank=True)
    # {"ok": bool, "state": "...", "detail": "..."} from the bot's post-check.
    post_check = models.JSONField(default=dict, blank=True)
    account = models.CharField(max_length=20, blank=True)  # same, different, unknown

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["bot_name", "store"],
                condition=Q(outcome=""),
                name="relogin_one_active_per_store",
            ),
            models.UniqueConstraint(
                fields=["port_group"],
                condition=Q(outcome="") & ~Q(port_group=""),
                name="relogin_one_active_per_port",
            ),
        ]

    def __str__(self):
        return f"{self.bot_name}:{self.store} {self.outcome or self.state}"

    @property
    def active(self) -> bool:
        return not self.outcome
