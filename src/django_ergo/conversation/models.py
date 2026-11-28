import uuid

from django import VERSION as DJANGO_VERSION
from django.contrib.auth import get_user_model
from django.db import models
from django.db import router
from django.db import transaction
from django.db.models import Q

from django_ergo.mixins import TimeStampedMixin
from django_ergo.models import Workflow

User = get_user_model()


class EngineType(models.TextChoices):
    CLAUDE = "claude", "Claude"
    OPENAI = "openai", "OpenAI"
    FIREWORKS = "fireworks", "Fireworks (private pool)"
    GOOGLE = "google", "Google Gemini"


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
# "context_size" (see normalize_compaction_mode).
CompactionMode.STREAM = CompactionMode.ROLLING
LEGACY_COMPACTION_MODES = {
    "stream": CompactionMode.CONTEXT_SIZE,
    "rolling": CompactionMode.CONTEXT_SIZE,
}


def normalize_compaction_mode(mode: str | None) -> str | None:
    """Map a legacy compaction mode name to its current value."""
    return LEGACY_COMPACTION_MODES.get(mode, mode)


class PermanentChatError(ValueError):
    """A permanent chat cannot be closed."""


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
    is_permanent = models.BooleanField(default=False, db_default=False)
    permanence_initialized = models.BooleanField(
        default=False, db_default=False, editable=False
    )
    # Opt-in per chat: the bot may revise its replies in place
    # (conversation.reply_updates). Off keeps every reply a new message.
    reply_updates = models.BooleanField(default=False, db_default=False)
    # Kept separate from metadata so stale metadata writers cannot undo a rename.
    user_title = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001 - NULL means automatic
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

    def save(self, *args, **kwargs):
        # A full save of a stale instance must not overwrite a concurrent rename.
        # Explicit updates to user_title remain possible through update_fields.
        update_fields = kwargs.get("update_fields", args[3] if len(args) > 3 else None)  # noqa: PLR2004
        if (
            not self._state.adding
            and self.status == SessionStatus.COMPLETED
            and (update_fields is None or "status" in update_fields)
        ):
            using = kwargs.get("using") or router.db_for_write(
                type(self), instance=self
            )
            with transaction.atomic(using=using):
                previous = (
                    type(self)
                    .objects.using(using)
                    .select_for_update()
                    .only("status", "is_permanent")
                    .get(pk=self.pk)
                )
                if previous.status != SessionStatus.COMPLETED:
                    previous.require_closable()
                return self._save_preserving_user_fields(*args, **kwargs)
        return self._save_preserving_user_fields(*args, **kwargs)

    def _save_preserving_user_fields(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields", args[3] if len(args) > 3 else None)  # noqa: PLR2004
        preserve = (
            not self._state.adding
            and update_fields is None
            and "user_title" in self.__dict__
        )
        if not preserve:
            return super().save(*args, **kwargs)
        title, permanent, initialized, updates = (
            self.user_title,
            self.is_permanent,
            self.permanence_initialized,
            self.reply_updates,
        )
        try:
            self.user_title = models.F("user_title")
            self.is_permanent = models.F("is_permanent")
            self.permanence_initialized = models.F("permanence_initialized")
            self.reply_updates = models.F("reply_updates")
            return super().save(*args, **kwargs)
        finally:
            (
                self.user_title,
                self.is_permanent,
                self.permanence_initialized,
                self.reply_updates,
            ) = (title, permanent, initialized, updates)

    def require_closable(self):
        if self.is_permanent:
            msg = "This chat is permanent; remove permanence before resolving it."
            raise PermanentChatError(msg)

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


class ReplyRevision(TimeStampedMixin):
    """One accepted revision of a bot's reply (see ``conversation.reply_updates``).

    The reply itself stays as its tool call in the transcript; each revision is
    the tool call that changed it, so history keeps every version. ``revision``
    counts from 1 (the original is 0); the unique constraints make a stale or
    concurrent update fail and a replayed tool call return its first receipt.
    """

    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="reply_revisions",
    )
    reply_id = models.CharField(max_length=255)  # the original reply's tool_use id
    revision = models.PositiveIntegerField()
    tool_use_id = models.CharField(max_length=255)  # the call that made this revision
    tool_name = models.CharField(max_length=255)
    content = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "reply_id", "revision"],
                name="reply_revision_unique",
            ),
            models.UniqueConstraint(
                fields=["session", "tool_use_id"],
                name="reply_revision_tool_use_unique",
            ),
        ]

    def __str__(self):
        return f"{self.reply_id} r{self.revision}"


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
    # Engine-specific data to send back unchanged, keyed by engine type, e.g.
    # {"google": {"thought_signature": "..."}} on a Gemini tool call. Engines
    # read only their own key.
    provider_metadata = models.JSONField(null=True, blank=True)

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
    private_export = models.ForeignKey(
        "PrivateExport",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="copies",
    )

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
    # The schedule's stable id (bot.yaml ``id``, default its name): what a run
    # is unique by. Rows from before ids existed were backfilled with their name.
    schedule_key = models.CharField(max_length=100, blank=True, default="")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    minute = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default="pending")
    steps = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bot_name", "schedule_key", "user", "minute"],
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
    # Restricts renewal after a recurring turn's worker result, even if its
    # originating schedule has since been deleted.
    recurring_origin = models.UUIDField(null=True, blank=True)
    # Causal identity is independent of mutable worker-owned state.
    originating_message = models.ForeignKey(
        "ThreadMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="started_workers",
    )
    polls = models.PositiveIntegerField(default=0)
    next_poll_at = models.DateTimeField(null=True, blank=True)
    # Set while one step runs, so a late or duplicate runner call skips it.
    step_started_at = models.DateTimeField(null=True, blank=True)
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


class AgentLaunch(TimeStampedMixin):
    """Private launch journal: committed before effects, scoped to one chat and bot.

    No public serializer/admin registration: briefs and external handles inherit
    the session's access boundary. Never treat these references as new authority.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ConversationSession, on_delete=models.CASCADE)
    bot_name = models.CharField(max_length=100)
    key = models.CharField(max_length=200)
    request = models.JSONField(default=dict)
    spec_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=32, default="launching")
    evidence = models.JSONField(default=dict)
    handle = models.JSONField(default=dict)
    watch_state = models.JSONField(default=dict)
    watch_title = models.CharField(max_length=200, default="")
    worker = models.OneToOneField(
        Worker, null=True, blank=True, on_delete=models.RESTRICT, related_name="launch"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "bot_name", "key"], name="agent_launch_key"
            )
        ]


class WakeStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    FIRED = "fired", "Fired (its message went to the chat)"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed (not retried)"


class Wake(TimeStampedMixin):
    """A turn a bot scheduled for itself in one chat or thread (see django_ergo.bots.wakes).

    It fires at most once: the firer claims the pending row under a row lock, and the
    message it sends is a ThreadMessage, which is itself claimed once. ``chain`` counts
    wakes in a row with no human message (or bot.yaml schedule prompt) between them;
    ``depth`` is the delegation depth of the message the scheduling turn answered,
    which the wake's message keeps (``bots.messaging.MAX_DEPTH``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="wakes"
    )
    bot_name = models.CharField(max_length=100, db_index=True)
    note = models.TextField()
    due_at = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=WakeStatus.choices, default=WakeStatus.PENDING
    )
    chain = models.PositiveSmallIntegerField(default=1)
    depth = models.PositiveIntegerField(default=0)
    thread_message = models.ForeignKey(
        ThreadMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    fired_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "due_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["session"],
                condition=models.Q(status="pending"),
                name="one_pending_wake_per_session",
            )
        ]

    def __str__(self):
        return f"{self.bot_name} wake {self.due_at:%Y-%m-%d %H:%M} ({self.status})"


def _recurring_constraint(condition, name):
    keyword = "condition" if DJANGO_VERSION >= (5, 1) else "check"
    return models.CheckConstraint(name=name, **{keyword: condition})


class RecurringWakeStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    BLOCKED = "blocked", "Blocked (explicit resume required)"
    CANCELLED = "cancelled", "Cancelled"
    COMPLETED = "completed", "Completed"


class AdmissionDrain(models.Model):
    """Singleton used to serialize a drain lease with durable work claims."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    owner = models.UUIDField(null=True)
    expires_at = models.DateTimeField(null=True)
    deadline = models.DateTimeField(null=True)

    class Meta:
        constraints = [_recurring_constraint(Q(id=1), "admission_drain_singleton")]

    def __str__(self):
        return "Admission drain lease"


class RecurringWakeLock(models.Model):
    """One durable admission mutex, including when there are no schedules yet."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        constraints = [_recurring_constraint(Q(id=1), "recurring_wake_single_lock")]

    def __str__(self):
        return "Recurring wake admission lock"


class RecurringWake(TimeStampedMixin):
    """A finite UTC elapsed-time schedule owned by one conversation thread."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="recurring_wakes"
    )
    bot_name = models.CharField(max_length=100)
    note = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=RecurringWakeStatus.choices,
        default=RecurringWakeStatus.ACTIVE,
    )
    interval_seconds = models.PositiveIntegerField()
    next_run_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    max_occurrences = models.PositiveIntegerField(null=True, blank=True)
    occurrences = models.PositiveIntegerField(default=0)
    periodic_checkins = models.BooleanField(default=False)
    invocations = models.PositiveSmallIntegerField(default=0)
    blocked_reason = models.TextField(blank=True, default="")
    depth = models.PositiveIntegerField(default=0)
    # Execution restrictions inherited from the scheduling turn, never its text.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["status", "next_run_at"], name="recurring_wake_due_idx"
            ),
            models.Index(
                fields=["session", "status"], name="recurring_wake_session_idx"
            ),
        ]
        constraints = [
            _recurring_constraint(
                Q(expires_at__isnull=False) | Q(max_occurrences__isnull=False),
                "recurring_wake_finite",
            ),
            _recurring_constraint(
                Q(interval_seconds__gte=1), "recurring_wake_interval"
            ),
            _recurring_constraint(
                Q(max_occurrences__isnull=True) | Q(max_occurrences__gte=1),
                "recurring_wake_positive_max",
            ),
        ]

    def __str__(self):
        return f"{self.bot_name} recurring wake {self.id} ({self.status})"


class RecurringWakeOccurrenceStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    DELIVERED = "delivered", "Delivered"
    WAITING = "waiting", "Waiting for approval"
    ANSWERED = "answered", "Answered"
    FAILED = "failed", "Failed (not retried)"
    CANCELLED = "cancelled", "Cancelled before delivery"


class RecurringWakeOccurrence(TimeStampedMixin):
    """One claimed slot, committed atomically with its automated ThreadMessage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(
        RecurringWake, on_delete=models.CASCADE, related_name="scheduled_occurrences"
    )
    scheduled_for = models.DateTimeField()
    dispatched_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=RecurringWakeOccurrenceStatus.choices,
        default=RecurringWakeOccurrenceStatus.QUEUED,
    )
    thread_message = models.OneToOneField(
        ThreadMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="recurring_wake_occurrence",
    )
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["scheduled_for", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "scheduled_for"], name="recurring_wake_unique_slot"
            ),
            models.UniqueConstraint(
                fields=["schedule"],
                condition=Q(status__in=["queued", "delivered", "waiting"]),
                name="recurring_wake_one_outstanding",
            ),
        ]

    def __str__(self):
        return f"{self.schedule_id} occurrence {self.scheduled_for.isoformat()} ({self.status})"


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


class UsageSync(models.Model):
    """State of the last fetch of the subscription limits (``omp usage``): one
    row (pk 1). The windows themselves live in :class:`ProviderUsage`; this says
    when they were last fetched, whether that worked and why not (see
    bots.usage_sync). Times are epoch seconds, like the windows' own."""

    attempted_at = models.FloatField(null=True, blank=True)
    succeeded_at = models.FloatField(null=True, blank=True)
    running_since = models.FloatField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    # {omp provider: {"error": str, "fetched_at": epoch | None, "targets": [ProviderUsage.provider]}}
    accounts = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"usage sync {self.succeeded_at or 'never'}"


class HindsightPrSync(models.Model):
    """Where a bot's pull request sync to Hindsight memory got to, per repo
    (see plugins.hindsight_pr_events). ``cursor`` is the newest ``updatedAt`` fully
    written; ``hashes`` maps a PR number to the hash of what was last written, so an
    unchanged PR is skipped. Losing a row only means a re-run of the backfill: the
    documents are replaced, never duplicated."""

    bot_name = models.CharField(max_length=100)
    repo = models.CharField(max_length=200)
    cursor = models.DateTimeField(null=True, blank=True)
    hashes = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bot_name", "repo"], name="hindsight_pr_sync_unique"
            )
        ]

    def __str__(self):
        return f"{self.bot_name} {self.repo} {self.cursor or 'never'}"


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


class ModelPoolState(models.Model):
    """Runtime gates of one providers.yaml pool (``bots.pools``), set by its
    owner (or an admin) on Ergonaut's Pools page. A null field keeps the
    providers.yaml value. Spend authority is never here: it is a
    :class:`SpendGrant`. ``version`` guards concurrent edits."""

    name = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(null=True, blank=True)
    rollover_out = models.BooleanField(null=True, blank=True)
    allow_auto_entry = models.BooleanField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class SpendEnvelope(models.Model):
    """The stable spending identity behind a line of spend authority
    (``bots.pools``): one cumulative ledger and one lock for every revision
    (:class:`SpendGrant`) of that authority. Spend and reservations (held
    and uncertain ones included) are summed over all its revisions, revoked
    ones too, and every reservation takes the advisory lock ``lock_key``.

    ``key`` is ``config:<pool>:<owner id>`` for the owner's own authority
    derived from providers.yaml: editing, removing and restoring the config
    revise and revoke revisions of the same envelope, so its money is never
    replenished by a config change. Its identity survives a rename: a new
    pool whose metered accounts (``accounts``) overlap an envelope's
    continues that envelope (re-keyed to the new name) or waits for an
    admin's explicit link (``linked_by``); config sync never funds one.

    A runtime-issued grant is its own envelope (``issued:<grant id>``) whose
    ``parent`` is the owner's config envelope it was issued from: every send
    on it is intersected with the parent's current authority, and the
    cumulative spend of the parent and all its children stays within the
    parent's current budget."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=160, unique=True)
    pool = models.CharField(max_length=64, db_index=True)
    source = models.CharField(max_length=16, default="issued")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    grantee = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    # Issued: the owner's config envelope this authority is drawn from (set
    # once, at creation). Null on a config envelope, or on an issued one
    # whose owner had no config authority (it then authorizes nothing).
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )
    # Config: every metered account it has ever funded (provider names, key
    # env names, account names; only grows), to recognize a renamed pool.
    accounts = models.JSONField(default=list, blank=True)
    # An admin's explicit continuation or funding of this envelope for a
    # config pool that overlapped another envelope (``bots.pools``).
    linked_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    linked_at = models.DateTimeField(null=True, blank=True)
    link_note = models.CharField(max_length=200, blank=True, default="")
    # An admin's explicit lift of a person's revocation (only that: a link
    # or an approval of accounts never lifts one).
    restored_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.key

    @property
    def lock_key(self) -> str:
        return f"envelope:{self.pk}"


class BudgetRecordQuerySet(models.QuerySet):
    """Audit identities and consent revisions cannot be rewritten."""

    def update(self, **kwargs):
        msg = "Budget audit records are immutable"
        raise TypeError(msg)

    def bulk_update(self, objs, fields, batch_size=None):
        msg = "Budget audit records are immutable"
        raise TypeError(msg)

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts") or kwargs.get("ignore_conflicts"):
            msg = "Budget audit records are immutable"
            raise TypeError(msg)
        return super().bulk_create(objs, **kwargs)

    def delete(self):
        msg = "Budget audit records are immutable"
        raise TypeError(msg)


class BudgetRecord(models.Model):
    objects = BudgetRecordQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            msg = "Budget audit records are immutable"
            raise TypeError(msg)
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        msg = "Budget audit records are immutable"
        raise TypeError(msg)


class ProviderBudgetFamily(BudgetRecord):
    """Deployment-wide provider identity; neither config nor key aliases reset it."""

    # Exactly one bucket per vendor, across every owner/account/model/bot.
    id = models.CharField(max_length=16, primary_key=True)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    assertion = models.CharField(max_length=200)

    def __str__(self):
        return self.pk


class ProviderAccountMapping(BudgetRecord):
    """Explicit admin assertion about a non-secret transport/account identity."""

    identity = models.CharField(max_length=1000, unique=True)
    family = models.ForeignKey(
        ProviderBudgetFamily, on_delete=models.PROTECT, related_name="accounts"
    )
    account_label = models.CharField(max_length=200)
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    assertion = models.CharField(max_length=200)

    def __str__(self):
        return f"{self.family_id}: {self.account_label}"


class ProviderWeeklyConsent(BudgetRecord):
    """Explicit recurring authority, independently switchable and append-only."""

    family = models.ForeignKey(
        ProviderBudgetFamily, on_delete=models.PROTECT, related_name="consents"
    )
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    version = models.PositiveIntegerField()
    weekly_usd = models.DecimalField(max_digits=16, decimal_places=4)
    enabled = models.BooleanField(default=False)
    valid_from = models.DateTimeField()
    expires_at = models.DateTimeField()
    timezone = models.CharField(max_length=16, default="UTC")
    note = models.CharField(max_length=200)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["family", "version"], name="provider_consent_version"
            ),
            models.CheckConstraint(
                condition=Q(weekly_usd__gte=0, weekly_usd__lte=100),
                name="provider_weekly_max_100",
            ),
            models.CheckConstraint(
                condition=Q(timezone="UTC"), name="provider_weekly_utc"
            ),
        ]

    def __str__(self):
        return f"{self.family_id} weekly consent v{self.version}"


class SpendFunding(models.Model):
    """Explicit authenticated funding revision, independent of config ceilings.

    Revisions share the envelope's cumulative ledger, including uncertain holds.
    Prior rows remain as the audit of who approved which exact terms.
    """

    budget_period = models.CharField(max_length=24, default="cumulative")
    envelope = models.ForeignKey(
        SpendEnvelope, on_delete=models.PROTECT, related_name="funding"
    )
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    budget_usd = models.DecimalField(max_digits=16, decimal_places=4)
    valid_from = models.DateTimeField()
    expires_at = models.DateTimeField()
    accounts = models.JSONField(default=list)
    note = models.CharField(max_length=200)

    def __str__(self):
        return f"Funding {self.pk} for envelope {self.envelope_id}"


class PaidDispatchState(models.Model):
    """Singleton latch: activation is permanent; pause preserves all ledgers."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    paused = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, related_name="+"
    )

    def __str__(self):
        return "Paid dispatch pause" if self.paused else "Paid dispatch state"


class SpendControlAudit(models.Model):
    """Audit of explicit funding, continuation, restoration and paid pause."""

    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=32)
    envelope = models.ForeignKey(
        SpendEnvelope, on_delete=models.PROTECT, null=True, related_name="control_audit"
    )
    details = models.JSONField(default=dict)

    def __str__(self):
        return f"{self.action} by actor {self.actor_id}"


class SpendGrant(models.Model):
    """One immutable revision of spend authority on a pool's metered routes,
    for one user (``grantee``), issued by the pool's owner (``bots.pools``).
    Changing the amount, routes or window makes a new revision. Revisions of
    the same authority share a :class:`SpendEnvelope`: its cumulative ledger
    and its lock. A superseded revision (``superseded_at``) still bounds
    queued work that captured it, intersected with the current revision; a
    revoked one authorizes nothing. Every metered send reserves against the
    envelope under its lock and re-checks the grantee, the current config,
    routes, window, ceiling and revocation.

    ``source`` is ``config`` for the owner's own revision derived from the
    pool's providers.yaml ``budget_usd``/``expires`` (``config_key``
    fingerprints it), or ``issued`` for one made at runtime."""

    IMMUTABLE_AFTER_CREATE = frozenset(
        {"revoked_at", "revoked_reason", "superseded_at"}
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    envelope = models.ForeignKey(
        SpendEnvelope, on_delete=models.CASCADE, related_name="revisions"
    )
    pool = models.CharField(max_length=64, db_index=True)
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="issued_spend_grants"
    )
    grantee = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="spend_grants"
    )
    routes = models.JSONField(default=list)  # exact provider/model refs
    budget_usd = models.DecimalField(max_digits=16, decimal_places=4)
    per_launch_usd = models.DecimalField(
        max_digits=16, decimal_places=4, null=True, blank=True
    )
    max_output_tokens = models.PositiveIntegerField()
    valid_from = models.DateTimeField()
    expires_at = models.DateTimeField()
    source = models.CharField(max_length=16, default="issued")
    budget_period = models.CharField(max_length=24, default="cumulative")
    config_key = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=200, blank=True, default="")
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.pool} grant {self.pk} for user {self.grantee_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            fields = kwargs.get("update_fields")
            if fields is None or set(fields) - self.IMMUTABLE_AFTER_CREATE:
                msg = "A spend grant is immutable; revoke it and issue a new one"
                raise ValueError(msg)
        elif self.envelope_id is None:
            if self.source == "config":
                msg = "A config grant is a revision of its pool owner's envelope"
                raise ValueError(msg)
            # A grant issued on its own is a new authorization: its own
            # envelope, drawn from the owner's config envelope on the pool.
            with transaction.atomic():
                self.envelope = SpendEnvelope.objects.create(
                    key=f"issued:{self.pk}",
                    pool=self.pool,
                    source=self.source,
                    owner_id=self.owner_id,
                    grantee_id=self.grantee_id,
                    parent=SpendEnvelope.objects.filter(
                        key=f"config:{self.pool}:{self.owner_id}", source="config"
                    ).first(),
                )
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)


class ModelPoolPreference(models.Model):
    """One user's pool choices (``key`` ``user:<id>``; ``deployment`` for
    work without a user): preferred order, the manually selected pool ("" =
    automatic, preferred order) and the auto-rollover gate (null = the
    providers.yaml default, which is off)."""

    key = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="model_pool_preferences",
    )
    order = models.JSONField(default=list, blank=True)
    active_pool = models.CharField(max_length=64, blank=True, default="")
    # Set when ``active_pool`` came from an automatic rollover: the pool it
    # left. Work stays on the rolled-over pool until the person switches
    # back or picks "use preferred order" (clearing ``active_pool``).
    rolled_from = models.CharField(max_length=64, blank=True, default="")
    auto_rollover = models.BooleanField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key}: {self.active_pool or 'auto'}"


class ProviderToggle(models.Model):
    """A runtime on/off switch for one providers.yaml provider (account).
    Off removes it from listings, pins, retries and new workers everywhere."""

    name = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    # The provider's account identity when it was switched off
    # (``bots.pools.provider_accounts``): metered sends stay off for it even
    # after providers.yaml renames the provider.
    accounts = models.JSONField(default=list, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name}: {'on' if self.enabled else 'off'}"


class RouteDecision(models.Model):
    """A receipt for one pool selection (``bots.pools.select_route``): what was
    asked for, which pool and route were chosen or why it was refused, and the
    candidates left out. Never a prompt, message text or credential."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="route_decisions",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="route_decisions",
    )
    bot_name = models.CharField(max_length=100, blank=True, default="")
    consumer = models.CharField(max_length=32)
    origin = models.CharField(max_length=200, blank=True, default="")  # privacy
    mode = models.CharField(max_length=16, blank=True, default="")  # auto/manual
    requested = models.CharField(max_length=200, blank=True, default="")
    tier = models.CharField(max_length=20, blank=True, default="")
    pool = models.CharField(max_length=64, blank=True, default="")
    route = models.CharField(max_length=200, blank=True, default="")
    billing = models.CharField(max_length=16, blank=True, default="")
    outcome = models.CharField(max_length=16)  # selected or refused
    code = models.CharField(max_length=32, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    rejected = models.JSONField(default=list, blank=True)
    rollover = models.JSONField(default=list, blank=True)  # pools passed through
    reserved_usd = models.DecimalField(
        max_digits=16, decimal_places=10, null=True, blank=True
    )
    # The spend grant a metered route was selected under, and the versions of
    # the user's preference, the pool's gates and the chat override (or the
    # queued job's captured policy) the decision was made from.
    grant = models.UUIDField(null=True, blank=True)
    revision = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.consumer}: {self.pool}/{self.route or self.code}"


class AuthorProfile(models.Model):
    """Who an external message author is, for apps to show: a name and a
    small picture, keyed like a message's ``author`` (``kind``, ``ref``), e.g.
    a Telegram user the Telegram plugin has heard from."""

    kind = models.CharField(max_length=40)
    ref = models.CharField(max_length=200)
    display_name = models.CharField(max_length=300, blank=True, default="")
    username = models.CharField(max_length=200, blank=True, default="")
    # A small picture, stored inline (a Telegram profile photo is a few KB).
    avatar = models.BinaryField(null=True, blank=True)
    avatar_type = models.CharField(max_length=100, blank=True, default="")
    # Where the picture came from (a Telegram file_unique_id), so it's fetched once.
    avatar_source = models.CharField(max_length=200, blank=True, default="")
    avatar_checked_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "ref"], name="author_profile_kind_ref"
            )
        ]

    def __str__(self):
        return f"{self.display_name or self.ref} ({self.kind})"


class InferenceOutcome(models.TextChoices):
    PENDING = "pending", "Sending (cost reserved)"
    OK = "ok", "Answered"
    ERROR = "error", "Failed after it may have reached the provider"
    NOT_SENT = "not_sent", "Refused before any HTTP request"


class InferenceRequest(TimeStampedMixin):
    """One HTTP attempt to an API-billed private model (a ``private_pools``
    member, see ``conversation.inference_policy``), recorded before it's sent.

    ``cost_usd`` is exact pricing by (provider, model ID) from the token
    counts the provider reported; None (``cost_status`` unknown) when the
    price or the usage is missing, never 0. ``reserved_usd`` is the
    worst-case cost held against the pool's spend cap while it's in flight;
    failed attempts keep it, since the provider may have billed them.
    ``reasoning_tokens`` is part of ``output_tokens``. No credential or grant
    is ever stored here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_family = models.ForeignKey(
        ProviderBudgetFamily,
        on_delete=models.PROTECT,
        null=True,
        related_name="attempts",
    )
    account_mapping = models.ForeignKey(
        ProviderAccountMapping,
        on_delete=models.PROTECT,
        null=True,
        related_name="attempts",
    )
    budget_week = models.DateField(null=True, db_index=True)
    # The vendor family this attempt was recorded against (blank: unknown).
    vendor = models.CharField(max_length=16, blank=True, default="")
    structured_call = models.ForeignKey(
        StructuredCall,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inference_requests",
    )
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inference_requests",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inference_requests",
    )
    bot_name = models.CharField(max_length=100, db_index=True)
    selector = models.CharField(
        max_length=200
    )  # what the bot asked for: personal/heavy
    pool = models.CharField(max_length=64)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)  # the exact model ID sent
    purpose = models.CharField(max_length=32, blank=True, default="")
    billing_mode = models.CharField(max_length=16, default="api")
    attempt = models.PositiveIntegerField(default=1)  # within one engine (one turn)
    upstream_request_id = models.CharField(max_length=200, blank=True, default="")
    prompt_tokens = models.IntegerField(null=True, blank=True)  # all input
    cached_tokens = models.IntegerField(null=True, blank=True)  # input read from cache
    output_tokens = models.IntegerField(null=True, blank=True)
    reasoning_tokens = models.IntegerField(null=True, blank=True)  # part of output
    # USD per million tokens used, with where they came from.
    rates = models.JSONField(default=dict, blank=True)
    reserved_usd = models.DecimalField(
        max_digits=16, decimal_places=10, null=True, blank=True
    )
    cost_usd = models.DecimalField(
        max_digits=16, decimal_places=10, null=True, blank=True
    )
    cost_status = models.CharField(max_length=16, default="pending")
    outcome = models.CharField(
        max_length=16,
        choices=InferenceOutcome.choices,
        default=InferenceOutcome.PENDING,
    )
    error = models.TextField(blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)
    # Empty means not enrolled; pending is durable accounting work, not provider work.
    cost_ledger_state = models.CharField(max_length=16, default="", db_index=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["pool", "created_at"]),
            models.Index(fields=["structured_call"]),
        ]

    def __str__(self):
        return f"{self.bot_name} {self.selector} {self.outcome}"


class ModelCatalogSnapshot(TimeStampedMixin):
    """Last successful non-billable discovery baseline for a schedule/user.

    Model records contain allowlisted catalog metadata only, never credentials,
    provider configuration, raw HTTP responses, or exception text.
    """

    scope = models.CharField(max_length=64, unique=True)
    coverage = models.JSONField(default=list, blank=True)
    models = models.JSONField(default=dict, blank=True)


class PrivateExport(TimeStampedMixin):
    """Privacy-safe receipt; bodies live only in the destination message/files."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        ConversationSession,
        on_delete=models.PROTECT,
        related_name="private_exports",
    )
    destination = models.ForeignKey(
        ConversationSession,
        on_delete=models.PROTECT,
        related_name="received_exports",
    )
    call = models.ForeignKey(StructuredCall, on_delete=models.PROTECT)
    authorization_id = models.CharField(max_length=100)
    actor_id = models.CharField(max_length=100)
    tool_use_id = models.CharField(max_length=200)
    manifest_sha256 = models.CharField(max_length=64)
    artifacts = models.JSONField(default=list)
    result = models.OneToOneField(
        ThreadMessage,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["call", "tool_use_id"],
                name="ergo_private_export_once",
            ),
        ]


class RuntimeReport(models.Model):
    """One running process's own report of the code it runs (see
    django_ergo.bots.live_watch).

    Each long-running process of the host (a web server, a Celery worker, beat,
    the bots runner) upserts one row per role it serves: the revision baked into
    its build, and whether it has finished starting. It refreshes ``seen_at`` on
    an interval and deletes its rows on a clean shutdown; a crashed process's rows
    go stale and stop counting. Nothing else writes here: a merged PR, a pushed
    image or a deployment manifest is never a report.
    """

    instance = models.CharField(max_length=200)
    role = models.CharField(max_length=50)
    revision = models.CharField(max_length=100, blank=True, default="")
    ready = models.BooleanField(default=False)
    started_at = models.DateTimeField()
    seen_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["role", "instance"]
        constraints = [
            models.UniqueConstraint(
                fields=["instance", "role"], name="one_runtime_report_per_role"
            )
        ]

    def __str__(self):
        return f"{self.role} {self.instance} at {self.revision[:12] or '?'}"


class LiveWatchStatus(models.TextChoices):
    PENDING = "pending", "Waiting for the revision to be live"
    LIVE = "live", "Live (the chat was told)"
    EXPIRED = "expired", "Not live in time (the chat was told)"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Sending the event failed (not retried)"


class LiveWatch(TimeStampedMixin):
    """A chat asked to be told when ``revision`` is running everywhere (see
    django_ergo.bots.live_watch).

    One per (chat, revision), so a chat hears about a revision at most once. The
    checker claims the pending row with a conditional UPDATE in the same
    transaction that creates the chat's event message, so two checkers, a
    double beat tick or a restart send it once. Past ``expires_at`` the chat
    gets a "not live" event instead.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="live_watches"
    )
    bot_name = models.CharField(max_length=100, db_index=True)
    revision = models.CharField(max_length=100)
    requested = models.CharField(max_length=200, blank=True, default="")
    note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=LiveWatchStatus.choices,
        default=LiveWatchStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    depth = models.PositiveIntegerField(default=0)
    settled_at = models.DateTimeField(null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    thread_message = models.ForeignKey(
        ThreadMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "expires_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "revision"], name="one_live_watch_per_chat_revision"
            )
        ]

    def __str__(self):
        return f"{self.bot_name} watch {self.revision[:12]} ({self.status})"


class RevisionAncestry(models.Model):
    """Cached answer to "does ``revision`` contain ``target``?" (``target`` is
    ``revision`` or one of its ancestors). Only definite answers are kept."""

    target = models.CharField(max_length=100)
    revision = models.CharField(max_length=100)
    contains = models.BooleanField()
    checked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["target", "revision"], name="one_revision_ancestry_answer"
            )
        ]

    def __str__(self):
        return f"{self.revision[:12]} {'contains' if self.contains else 'lacks'} {self.target[:12]}"
