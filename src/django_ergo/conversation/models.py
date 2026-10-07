import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Q

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
    ROLLING = "rolling", "Rolling"


# Deprecated: rolling compaction used to be called "stream". The old member
# name still resolves, and stored or configured "stream" values are read as
# "rolling" (see normalize_compaction_mode).
CompactionMode.STREAM = CompactionMode.ROLLING
LEGACY_COMPACTION_MODES = {"stream": CompactionMode.ROLLING}


def normalize_compaction_mode(mode: str | None) -> str | None:
    """Map a legacy compaction mode name to its current value."""
    return LEGACY_COMPACTION_MODES.get(mode, mode)


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
    # The provider/model picked for this chat (a providers.yaml ref, e.g.
    # "claude/claude-opus-5-5"); "" uses the bot's default. Messages are stored
    # engine-neutral (SessionMessage), so any model can take the next turn.
    model = models.CharField(max_length=200, blank=True, default="")
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
    # When the session's owner last looked at it (replies after this are unread).
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["engine_type"]),
        ]

    def __str__(self):
        return f"{self.user} - {self.engine_type} ({self.status})"


class SessionMessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"


class SessionMessage(TimeStampedMixin):
    """A message in a conversation, stored the same way for every engine.

    A message is a role and a list of content blocks (text, tool calls, tool
    results, thinking). Tool results go in user messages. Each engine renders
    these rows into its own API's format when it sends them (see
    ``claude_message_dict`` and ``openai_message_dicts``), so a chat can move
    between models on different engines. The system prompt isn't stored here:
    it comes from the session on every call.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(
        max_length=20,
        choices=SessionMessageRole.choices,
    )
    stop_reason = models.CharField(max_length=30, null=True, blank=True)  # noqa: DJ001
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    reasoning_tokens = models.IntegerField(null=True, blank=True)  # in output_tokens
    model_name = models.CharField(max_length=100, null=True, blank=True)  # noqa: DJ001
    cache_creation_input_tokens = models.IntegerField(null=True, blank=True)
    cache_read_input_tokens = models.IntegerField(null=True, blank=True)
    sequence = models.IntegerField()
    author = models.JSONField(default=dict, blank=True)
    provenance = models.JSONField(default=dict, blank=True)

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


class MessageBlock(TimeStampedMixin):
    """A content block within a SessionMessage."""

    message = models.ForeignKey(
        SessionMessage,
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


# The names these models had before messages were engine-neutral.
ClaudeMessage = SessionMessage
ClaudeContentBlock = MessageBlock


class OpenAIMessageRole(models.TextChoices):
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"
    SYSTEM = "system", "System"
    TOOL = "tool", "Tool"


class OpenAIMessage(TimeStampedMixin):
    """Legacy: OpenAI chats' messages before they moved to SessionMessage.

    Migration 0030 copied these rows into SessionMessage. Nothing reads or
    writes them now; the table is kept for one release, then dropped.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="legacy_openai_messages",
    )
    role = models.CharField(
        max_length=20,
        choices=OpenAIMessageRole.choices,
    )
    content = models.TextField(null=True, blank=True)  # noqa: DJ001
    tool_calls = models.JSONField(null=True, blank=True)
    tool_call_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    # A tool result's images, as image_ref items (conversation.images).
    images = models.JSONField(null=True, blank=True)
    function_name = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    input_tokens = models.IntegerField(null=True, blank=True)
    # Cached prompt tokens, billed at the cached-input rate; input_tokens excludes them.
    cache_read_input_tokens = models.IntegerField(null=True, blank=True)
    cache_creation_input_tokens = models.IntegerField(null=True, blank=True)
    reasoning_tokens = models.IntegerField(
        null=True, blank=True
    )  # part of output_tokens
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
    STOPPED = "stopped", "Stopped"


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
    reasoning_tokens = models.IntegerField(default=0)  # included in output_tokens
    # What the call cost, priced request by request as it ran, so per-request tiers
    # (the long-context surcharge) apply. None: unpriced, or recorded before this.
    cost_usd = models.DecimalField(
        max_digits=14, decimal_places=8, null=True, blank=True
    )
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
    # Set when a bot (or person) archives the file: it stays stored and readable
    # by id but drops out of the default file list and the bot's context.
    archived_at = models.DateTimeField(null=True, blank=True)

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


class BotJob(models.Model):
    """A piece of a bot's own code run in the background (a schedule step, a task)."""

    bot_name = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=200)  # e.g. "schedule weekly-stats, step 1"
    target = models.CharField(max_length=300)  # "tools/analytics.py:pull_stats"
    args = models.JSONField(default=dict, blank=True)
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=20, default="pending"
    )  # pending, in_progress, completed, failed
    progress = models.PositiveSmallIntegerField(default=0)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    traceback = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.bot_name} {self.name} ({self.status})"


class WorkerStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class Worker(TimeStampedMixin):
    """Long-running work a chat or thread started, e.g. an Orca coding agent, watched in
    the background (see django_ergo.bots.workers).

    The session shows as busy while it runs. When it finishes, its result is
    sent to the session as a message (unless ``notify`` is off), so the bot
    follows up with a reply.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="workers"
    )
    bot_name = models.CharField(max_length=100, db_index=True)
    title = models.CharField(max_length=200)
    # What runs: "task:<name>" (a @bot_task) or "<plugin>:<name>" (a plugin's worker).
    function = models.CharField(max_length=200)
    args = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20, choices=WorkerStatus.choices, default=WorkerStatus.QUEUED
    )
    progress = models.TextField(blank=True, default="")  # the latest status line
    state = models.JSONField(
        default=dict, blank=True
    )  # the function's own notes between polls
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    notify = models.BooleanField(default=True)
    polls = models.PositiveIntegerField(default=0)
    next_poll_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["session", "status"])]

    def __str__(self):
        return f"{self.bot_name} worker {self.title} ({self.status})"

    @property
    def active(self) -> bool:
        return self.status in (WorkerStatus.QUEUED, WorkerStatus.RUNNING)


class AgentUsage(TimeStampedMixin):
    """Token usage an Orca worker's coding agent wrote to its own session files."""

    worker = models.ForeignKey(
        Worker,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_usage",
    )
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="agent_usage"
    )
    bot_name = models.CharField(max_length=100, db_index=True)
    source = models.CharField(max_length=20, default="orca")
    agent = models.CharField(max_length=20)
    model = models.CharField(max_length=200)
    input_tokens = models.PositiveBigIntegerField(default=0)
    cache_write_tokens = models.PositiveBigIntegerField(default=0)
    cache_read_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    reasoning_tokens = models.PositiveBigIntegerField(default=0)
    requests = models.PositiveIntegerField(default=0)
    first_at = models.DateTimeField(null=True, blank=True)
    last_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["worker", "model"], name="agent_usage_worker_model"
            )
        ]
        indexes = [models.Index(fields=["session", "last_at"])]

    def __str__(self):
        return f"{self.agent} {self.model} for {self.worker_id or self.session_id}"


class AgentSession(TimeStampedMixin):
    """A durable native CLI session, independent of the Worker that observed it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_sessions",
    )
    cli = models.CharField(max_length=20)
    host_namespace = models.CharField(max_length=200)
    profile_namespace = models.CharField(max_length=200, default="default")
    native_session_id = models.CharField(max_length=500)
    parent_session = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_sessions",
    )
    initial_cwd = models.TextField(blank=True, default="")
    native_started_at = models.DateTimeField(null=True, blank=True)
    native_ended_at = models.DateTimeField(null=True, blank=True)
    first_observed_at = models.DateTimeField(null=True, blank=True)
    last_observed_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=20, default="unknown")
    transcript_completeness = models.CharField(max_length=20, default="partial")
    usage_completeness = models.CharField(max_length=20, default="partial")
    parser_version = models.CharField(max_length=100, blank=True, default="")
    source_manifest = models.JSONField(default=dict, blank=True)
    collection_status = models.CharField(max_length=20, default="pending")
    collection_error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "host_namespace",
                    "cli",
                    "profile_namespace",
                    "native_session_id",
                ],
                name="agent_session_native_identity",
            )
        ]


class AgentRunSession(TimeStampedMixin):
    """A launch segment attached to a native session; sessions can be resumed."""

    worker = models.ForeignKey(
        Worker,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_run_sessions",
    )
    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="run_sessions"
    )
    launch_key = models.CharField(max_length=200)
    segment_key = models.CharField(max_length=200)
    attached_at = models.DateTimeField(null=True, blank=True)
    detached_at = models.DateTimeField(null=True, blank=True)
    relation = models.CharField(max_length=20, default="primary")
    match_confidence = models.CharField(max_length=20, default="exact")
    evidence = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "launch_key", "segment_key"],
                name="agent_run_session_segment",
            )
        ]


class AgentWorkspaceObservation(TimeStampedMixin):
    """An immutable cwd/git identity observation made while a session ran."""

    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="workspace_observations"
    )
    run_session = models.ForeignKey(
        AgentRunSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_observations",
    )
    source_event_key = models.CharField(max_length=500)
    observed_at = models.DateTimeField(null=True, blank=True)
    observed_cwd = models.TextField()
    evidence_kind = models.CharField(max_length=40)
    host_namespace = models.CharField(max_length=200, default="local")
    repo_root = models.TextField(blank=True, default="")
    worktree_path = models.TextField(blank=True, default="")
    common_git_dir = models.TextField(blank=True, default="")
    canonical_remote = models.TextField(blank=True, default="")
    remotes = models.JSONField(default=list, blank=True)
    branch = models.CharField(max_length=500, blank=True, default="")
    head = models.CharField(max_length=100, blank=True, default="")
    project_key = models.CharField(max_length=100, db_index=True)
    confidence = models.CharField(max_length=20, default="unknown")
    resolution_error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "source_event_key"],
                name="agent_workspace_observation_source",
            )
        ]


class AgentUsageEvent(TimeStampedMixin):
    """One idempotent native request/delta contribution to the usage ledger."""

    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="usage_events"
    )
    run_session = models.ForeignKey(
        AgentRunSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_events",
    )
    workspace_observation = models.ForeignKey(
        AgentWorkspaceObservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_events",
    )
    source_event_key = models.CharField(max_length=500)
    native_request_id = models.CharField(max_length=500, blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    observed_at = models.DateTimeField(auto_now_add=True)
    provider = models.CharField(max_length=100, blank=True, default="")
    model = models.CharField(max_length=200, blank=True, default="")
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    cache_write_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    cache_read_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reasoning_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    requests = models.PositiveIntegerField(null=True, blank=True)
    billing_mode = models.CharField(max_length=20, default="unknown")
    reported_usd = models.DecimalField(
        max_digits=16, decimal_places=8, null=True, blank=True
    )
    estimated_usd = models.DecimalField(
        max_digits=16, decimal_places=8, null=True, blank=True
    )
    cost_status = models.CharField(max_length=20, default="missing")
    price_snapshot = models.JSONField(default=dict, blank=True)
    source_semantics = models.CharField(max_length=100, blank=True, default="")
    parser_version = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "source_event_key"],
                name="agent_usage_event_source",
            ),
            models.UniqueConstraint(
                fields=["session", "native_request_id"],
                condition=~Q(native_request_id=""),
                name="agent_usage_event_native_request",
            ),
        ]
        indexes = [models.Index(fields=["session", "occurred_at"])]


class AgentTranscriptArtifact(TimeStampedMixin):
    """Private immutable bytes and their verified object-storage metadata."""

    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="transcript_artifacts"
    )
    source_key = models.CharField(max_length=500)
    storage_key = models.TextField()
    format = models.CharField(max_length=40, default="native-jsonl")
    classification = models.CharField(max_length=20)
    redaction_version = models.CharField(max_length=40, blank=True, default="")
    sha256 = models.CharField(max_length=64)
    byte_count = models.PositiveBigIntegerField(default=0)
    compression = models.CharField(max_length=20, blank=True, default="")
    encryption_key_version = models.CharField(max_length=100, blank=True, default="")
    upload_state = models.CharField(max_length=20, default="pending")
    error = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "source_key", "classification"],
                name="agent_transcript_artifact_source",
            )
        ]


class AgentSessionThreadLink(TimeStampedMixin):
    """A session-to-chat/worker link, deliberately not a ConversationAttachment."""

    session = models.ForeignKey(
        AgentSession, on_delete=models.CASCADE, related_name="thread_links"
    )
    conversation = models.ForeignKey(
        ConversationSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_session_links",
    )
    worker = models.ForeignKey(
        Worker,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_session_links",
    )
    owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_session_links",
    )
    title = models.CharField(max_length=200, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, default="active")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "conversation"], name="agent_session_thread_link"
            )
        ]


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


class ProviderUsage(models.Model):
    """The latest subscription windows a provider's engine reported, e.g.
    ``{"five_hour": {"used": 42.0, "resets_at": 1791170000}}`` (see bots.routing)."""

    provider = models.CharField(max_length=100, unique=True)
    windows = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.provider


class RoutingPolicy(models.Model):
    """routing.md compiled into routing rules, keyed by the text's hash."""

    source_sha = models.CharField(max_length=64, unique=True)
    source = models.TextField(blank=True, default="")
    rules = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.source_sha[:12]


class RoutingText(models.Model):
    """Routing priorities saved from Ergonaut's Routing page. While a row
    exists its text replaces routing.md (see bots.routing.routing_text)."""

    text = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.text[:40]


class RoutingSwitch(models.Model):
    """A chat or coding agent that the router moved off its first choice."""

    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routing_switches",
    )
    label = models.CharField(max_length=300, blank=True, default="")
    tier = models.CharField(max_length=20)
    from_model = models.CharField(max_length=200, blank=True, default="")
    to_model = models.CharField(max_length=200)
    reason = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.from_model} -> {self.to_model}"
