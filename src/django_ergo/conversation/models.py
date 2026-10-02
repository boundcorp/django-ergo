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


class CompactionMode(models.TextChoices):
    NONE = "none", "None"
    TIME = "time", "Time-based"
    CONTEXT_SIZE = "context_size", "Context size"
    STREAM = "stream", "Stream"


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
    # Overrides workflow.instructions when set.
    system_prompt = models.TextField(blank=True, default="")
    compaction_mode = models.CharField(
        max_length=20,
        choices=CompactionMode.choices,
        default=CompactionMode.NONE,
    )
    # Mode parameters; see conversation.compaction.DEFAULT_CONFIG.
    compaction_config = models.JSONField(default=dict, blank=True)
    # Set for sessions owned by an Ergo bot (django_ergo.bots).
    bot_name = models.CharField(max_length=100, blank=True, default="", db_index=True)
    # The session that created this one, e.g. a bot's root orchestrator.
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )

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


class StructuredCallStatus(models.TextChoices):
    IN_PROGRESS = "in_progress", "In progress"
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    TURN_LIMITED = "turn_limited", "Turn limited"


class StructuredCall(TimeStampedMixin):
    """One structured call: a request, a kind, and a validated response.

    A call stands on its own. Its tool loop is kept in ``transcript``
    (engine-native messages) so it can be audited and revised. A call made
    inside a conversation is a turn of that session instead: ``session`` is
    set, the loop lives in the session's messages, and first_sequence and
    last_sequence give its span. Sessions can mix structured calls with
    ordinary chat turns.

    ``parent`` links a revision ("make the title shorter") to the call it
    corrects.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Free-form label for the job ("planner", "summarizer"). A varchar so new
    # kinds don't need migrations.
    kind = models.CharField(max_length=64)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="structured_calls",
    )
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="structured_calls",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revisions",
    )
    request = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=StructuredCallStatus.choices,
        default=StructuredCallStatus.IN_PROGRESS,
    )
    response = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    error_category = models.CharField(max_length=20, blank=True, default="")
    turns_used = models.IntegerField(default=0)
    engine_type = models.CharField(max_length=20, blank=True, default="")
    model_name = models.CharField(max_length=100, blank=True, default="")
    # Standalone calls: what was sent, so revisions can replay it.
    system_prompt = models.TextField(blank=True, default="")
    transcript = models.JSONField(default=list, blank=True)
    # Calls inside a session: the span of session messages this call wrote.
    first_sequence = models.IntegerField(null=True, blank=True)
    last_sequence = models.IntegerField(null=True, blank=True)
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cache_creation_input_tokens = models.IntegerField(default=0)
    cache_read_input_tokens = models.IntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["kind", "-created_at"]),
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self):
        return f"{self.kind} call {self.id} ({self.status})"


class ConversationCompaction(TimeStampedMixin):
    """A summary standing in for a session's earlier messages in model context.

    Messages with sequence <= upto_sequence are replaced by ``summary`` when
    engines rebuild context. The rows themselves are kept, so history tools
    can still read them. Each summary folds in the previous one (rolling).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="compactions",
    )
    mode = models.CharField(max_length=20, choices=CompactionMode.choices)
    reason = models.CharField(max_length=255, blank=True, default="")
    from_sequence = models.IntegerField()
    upto_sequence = models.IntegerField()
    message_count = models.IntegerField(default=0)
    summary = models.TextField()
    # The structured call that wrote the summary, when one did.
    structured_call = models.ForeignKey(
        StructuredCall,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compactions",
    )

    class Meta:
        ordering = ["upto_sequence"]
        indexes = [models.Index(fields=["session", "-upto_sequence"])]

    def __str__(self):
        return f"{self.session_id} compaction <= {self.upto_sequence}"


class AttachmentKind(models.TextChoices):
    IMAGE = "image", "Image"
    AUDIO = "audio", "Audio"
    DOCUMENT = "document", "Document"


class AttachmentSource(models.TextChoices):
    MESSAGE = "message", "Sent with a message"
    UPLOAD = "upload", "Uploaded to the session"
    BOT = "bot", "Written by the bot"


class ConversationAttachment(TimeStampedMixin):
    """A file in a chat session: an image, audio clip or document.

    A file sent with a user message is linked to it by its sequence number,
    so it works for every engine's message table. A file uploaded to the
    session, or written by the bot, has no message (``message_sequence`` is
    null) and is read through the attachments tools instead. The bytes live
    in ``file`` (default storage) or at ``url``. Audio keeps a
    ``transcript`` for engines that can't take audio input.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    message_sequence = models.IntegerField(null=True, blank=True)
    source = models.CharField(
        max_length=20,
        choices=AttachmentSource.choices,
        default=AttachmentSource.MESSAGE,
    )
    position = models.IntegerField(default=0)
    kind = models.CharField(max_length=20, choices=AttachmentKind.choices)
    media_type = models.CharField(max_length=100)
    file = models.FileField(upload_to="ergo/attachments/%Y/%m/", blank=True)
    url = models.URLField(max_length=2000, blank=True, default="")
    filename = models.CharField(max_length=255, blank=True, default="")
    size = models.IntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    transcript = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["message_sequence", "position"]
        indexes = [models.Index(fields=["session", "message_sequence"])]

    def __str__(self):
        where = (
            f"#{self.message_sequence}"
            if self.message_sequence is not None
            else self.source
        )
        return f"{self.session_id} {where} {self.filename or self.kind}"


class ThreadMessageStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    DELIVERED = "delivered", "Delivered (the recipient is working on it)"
    WAITING = "waiting", "Waiting for the user's approval"
    ANSWERED = "answered", "Answered"
    FAILED = "failed", "Failed"


class ThreadMessage(TimeStampedMixin):
    """A message from one bot session to another (or a reply back).

    The recipient answers it in a turn of its own, and the answer goes back
    to ``sender_session`` as a new ThreadMessage (``in_reply_to`` this one).
    A message with no sender session came from a person; nothing is routed
    back. ``depth`` counts hops, so bots can't message each other forever.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender_session = models.ForeignKey(
        ConversationSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_thread_messages",
    )
    recipient_session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="thread_messages",
    )
    in_reply_to = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies"
    )
    text = models.TextField()
    depth = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=ThreadMessageStatus.choices,
        default=ThreadMessageStatus.QUEUED,
    )
    reply_text = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["recipient_session", "status"])]

    def __str__(self):
        return f"{self.sender_session_id or 'person'} -> {self.recipient_session_id} ({self.status})"


class ScheduleRun(models.Model):
    """One run of a bot's schedule for one person, so it never runs twice in a minute."""

    bot_name = models.CharField(max_length=100)
    schedule = models.CharField(max_length=100)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    minute = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bot_name", "schedule", "user", "minute"],
                name="unique_schedule_run",
            )
        ]

    def __str__(self):
        return f"{self.bot_name}/{self.schedule} {self.minute:%Y-%m-%d %H:%M}"


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
