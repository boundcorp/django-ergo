import uuid

from django.contrib.auth import get_user_model
from django.db import models

from django_ergo.mixins import TimeStampedMixin
from django_ergo.models import Workflow

User = get_user_model()


class EngineType(models.TextChoices):
    CLAUDE = "claude", "Claude"
    OPENAI = "openai", "OpenAI"


class TransportType(models.TextChoices):
    API = "api", "API"


class SessionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"


class ConversationSession(TimeStampedMixin):
    """A conversation session with an AI engine."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="conversation_sessions",
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversation_sessions",
    )
    engine_type = models.CharField(
        max_length=20,
        choices=EngineType.choices,
    )
    transport_type = models.CharField(
        max_length=20,
        choices=TransportType.choices,
    )
    session_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=SessionStatus.choices,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["engine_type"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.engine_type} ({self.status})"


class ClaudeMessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"


class ClaudeMessage(TimeStampedMixin):
    """A message in a Claude conversation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="claude_messages",
    )
    role = models.CharField(
        max_length=20,
        choices=ClaudeMessageRole.choices,
    )
    stop_reason = models.CharField(max_length=30, null=True, blank=True)  # noqa: DJ001
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    cache_creation_input_tokens = models.IntegerField(null=True, blank=True)
    cache_read_input_tokens = models.IntegerField(null=True, blank=True)
    sequence = models.IntegerField()

    class Meta:
        ordering = ["sequence"]
        indexes = [
            models.Index(fields=["session", "sequence"]),
        ]

    def __str__(self):
        return f"{self.session} - {self.role} [{self.sequence}]"


class ContentBlockType(models.TextChoices):
    TEXT = "text", "Text"
    TOOL_USE = "tool_use", "Tool Use"
    TOOL_RESULT = "tool_result", "Tool Result"
    THINKING = "thinking", "Thinking"


class ClaudeContentBlock(TimeStampedMixin):
    """A content block within a Claude message."""

    message = models.ForeignKey(
        ClaudeMessage,
        on_delete=models.CASCADE,
        related_name="content_blocks",
    )
    block_type = models.CharField(
        max_length=20,
        choices=ContentBlockType.choices,
    )
    sequence = models.IntegerField()

    # Text / thinking content
    text = models.TextField(null=True, blank=True)  # noqa: DJ001
    thinking = models.TextField(null=True, blank=True)  # noqa: DJ001

    # Tool use fields
    tool_use_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    tool_name = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    tool_input = models.JSONField(null=True, blank=True)

    # Tool result fields
    tool_result_for = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    tool_result_content = models.JSONField(null=True, blank=True)
    is_error = models.BooleanField(default=False)

    class Meta:
        ordering = ["sequence"]
        indexes = [
            models.Index(fields=["message", "sequence"]),
            models.Index(fields=["block_type"]),
            models.Index(fields=["tool_name"]),
        ]

    def __str__(self):
        return f"{self.message} - {self.block_type} [{self.sequence}]"


class OpenAIMessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"
    SYSTEM = "system", "System"
    TOOL = "tool", "Tool"


class OpenAIMessage(TimeStampedMixin):
    """A message in an OpenAI conversation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="openai_messages",
    )
    role = models.CharField(
        max_length=20,
        choices=OpenAIMessageRole.choices,
    )
    content = models.TextField(null=True, blank=True)  # noqa: DJ001
    tool_calls = models.JSONField(null=True, blank=True)
    tool_call_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    function_name = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    sequence = models.IntegerField()

    class Meta:
        ordering = ["sequence"]
        indexes = [
            models.Index(fields=["session", "sequence"]),
        ]

    def __str__(self):
        return f"{self.session} - {self.role} [{self.sequence}]"


class KBUsageMode(models.TextChoices):
    READ = "read", "Read"
    WRITE = "write", "Write"
    SUGGEST = "suggest", "Suggest"


class ConversationKBUsage(TimeStampedMixin):
    """Tracks which knowledgebases are used in which conversations and how."""

    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="kb_usages",
    )
    knowledgebase = models.ForeignKey(
        "django_ergo.Knowledgebase",
        on_delete=models.CASCADE,
        related_name="conversation_usages",
    )
    mode = models.CharField(max_length=10, choices=KBUsageMode.choices)

    class Meta:
        unique_together = [["session", "knowledgebase", "mode"]]

    def __str__(self):
        return f"{self.session_id} -> {self.knowledgebase_id} ({self.mode})"
