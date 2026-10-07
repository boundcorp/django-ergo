"""What people did through bot pages."""

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
