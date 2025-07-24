import uuid
import json
from typing import Optional, List, Dict, Any

from django.db import models
from django.contrib.auth import get_user_model
# from pgvector.django import VectorField, CosineDistance

from django_ergo.mixins import TimeStampedMixin
# from django_ergo.fields import SummarizedVectorField, generate_embedding

User = get_user_model()


class MessageRole(models.TextChoices):
    """Roles for chat messages."""
    USER = "user", "User"
    ASSISTANT = "assistant", "Assistant"
    SYSTEM = "system", "System"
    TOOL = "tool", "Tool"


class MessageType(models.TextChoices):
    """Types of messages that can be sent in a chat."""
    USER_INPUT = "user_input", "User Input"
    ASSISTANT_MESSAGE = "assistant_message", "Assistant Message"
    TOOL_REQUEST = "tool_request", "Tool Request"
    TOOL_RESPONSE = "tool_response", "Tool Response"
    SYSTEM_MESSAGE = "system_message", "System Message"
    ERROR = "error", "Error"


class Workflow(TimeStampedMixin):
    """
    A workflow defines the AI logic and tools available for processing chat messages.
    Workflows can be associated with knowledgebases and have specific tools and instructions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255, 
        help_text="Human-readable name for the workflow"
    )
    description = models.TextField(
        help_text="Description of what this workflow does"
    )
    instructions = models.TextField(
        help_text="System instructions for the AI agent"
    )
    tools_config = models.JSONField(
        default=dict, 
        help_text="Configuration for tools available to this workflow"
    )
    is_active = models.BooleanField(
        default=True, 
        help_text="Whether this workflow is available for use"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_tools_config(self) -> Dict[str, Any]:
        """Get the tools configuration as a dictionary."""
        return self.tools_config or {}

    def get_knowledgebases_list(self) -> List[str]:
        """Get list of knowledgebase names this workflow can access."""
        return [kb.name for kb in self.knowledgebases.all()]


class Knowledgebase(TimeStampedMixin):
    """
    A model to store a knowledgebase. A KB is a nested hierarchy of articles.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField()
    owner_id = models.CharField(
        max_length=255, 
        null=True, 
        blank=True, 
        db_index=True,
        help_text="Owner identifier for multi-tenant support"
    )
    workflows = models.ManyToManyField(
        Workflow,
        blank=True,
        related_name="knowledgebases",
        help_text="Workflows that can access this knowledgebase"
    )

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["owner_id"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return self.name

    def to_markdown(self):
        """Export knowledgebase metadata to markdown format."""
        return f"# KB ID: {self.id}\nName: {self.name}\nDescription: {self.description}"

    def get_table_of_contents(self):
        """Get table of contents for top-level articles."""
        return "\n".join([
            f"# {article.hierarchy_code} {article.title}"
            for article in self.articles.filter(hierarchy_code__regex=r"^.$")
        ])


class ArticleQuerySet(models.QuerySet):
    """Custom QuerySet for Article model with search capabilities."""
    
    def hybrid_search(self, query_text: str, top_k: int = 10):
        """
        Perform a hybrid search combining PostgreSQL full-text and embedding similarity.
        
        For SQLite development, this is simplified to text search only.
        
        Args:
            query_text: User's search query
            top_k: Number of results to return
            
        Returns:
            QuerySet: Top-k relevant results, balanced between text and semantic search.
        """
        # For SQLite development - simple text search
        return (
            self.filter(
                models.Q(title__icontains=query_text) |
                models.Q(content__icontains=query_text) |
                models.Q(summary__icontains=query_text)
            )[:top_k]
        )
        
        # For PostgreSQL with pgvector (commented out for dev):
        # embedding = generate_embedding(query_text)
        # return (
        #     self.annotate(semantic_distance=CosineDistance("embedding", embedding))
        #     .order_by("semantic_distance")[:top_k]
        # )

    def by_hierarchy_prefix(self, prefix: str):
        """Get articles by hierarchy code prefix."""
        return self.filter(hierarchy_code__startswith=prefix)

    def to_prefetch_results(self, top_k: int = 5):
        """Convert queryset to prefetch-friendly format."""
        return [
            {
                "title": article.title,
                "content": article.content,
                "hierarchy_code": article.hierarchy_code,
                "id": str(article.id),
            }
            for article in self[:top_k]
        ]


class Article(TimeStampedMixin):
    """
    An article or document in a knowledgebase.

    An article has a title (short synopsis, up to 100 tokens), and a content (longer text, up to 10,000 tokens).

    Each article has a hierarchy code, which is a string of digits and letters that represent the article's position in the knowledgebase (0-indexed hexadecimal).
    For example, the article with hierarchy code "C3" is the 3rd article in the 12th chapter of the knowledgebase.
    We can retrieve full articles by their hierarchy code, search for articles by semantic similarity, or request an index of articles in a given chapter.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    knowledgebase = models.ForeignKey(
        Knowledgebase, 
        on_delete=models.CASCADE, 
        related_name="articles"
    )
    hierarchy_code = models.CharField(
        max_length=16,
        db_index=True,
        default="0",
        help_text="The hierarchy code of the article, e.g. '012' (0th chapter, 1st section, 2nd sub-section) or 'C3' (12th chapter, 3rd sub-section)"
    )
    title = models.CharField(max_length=512)
    content = models.TextField()  # Simplified for SQLite
    # content = SummarizedVectorField()  # For PostgreSQL
    # embedding = VectorField(dimensions=1536, null=True, editable=False)  # For PostgreSQL
    summary = models.TextField(null=True, blank=True)

    objects = ArticleQuerySet.as_manager()

    class Meta:
        ordering = ["hierarchy_code"]
        indexes = [
            models.Index(fields=["knowledgebase", "hierarchy_code"]),
            models.Index(fields=["hierarchy_code"]),
        ]
        unique_together = [["knowledgebase", "hierarchy_code"]]

    def __str__(self):
        return f"{self.hierarchy_code}: {self.title}"


class UserChat(TimeStampedMixin):
    """
    A chat owned by a single user, associated with a specific workflow.
    This is the main model for user conversations with AI agents.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chats",
        help_text="The user who owns this chat"
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="chats",
        help_text="The workflow that processes messages in this chat"
    )
    title = models.CharField(
        max_length=255, 
        default="New Chat", 
        help_text="Title of the chat conversation"
    )
    is_active = models.BooleanField(
        default=True, 
        help_text="Whether this chat is currently active"
    )
    metadata = models.JSONField(
        default=dict,
        help_text="Additional metadata for the chat (user preferences, context, etc.)"
    )
    # Workflow state persistence for pause/resume
    workflow_state = models.JSONField(
        default=dict,
        help_text="Serialized workflow state for pause/resume functionality"
    )

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "-updated_at"]),
            models.Index(fields=["workflow"]),
        ]

    def __str__(self):
        return f"{self.user.username}: {self.title}"

    def get_messages(self) -> List["ChatMessage"]:
        """Get all messages in this chat, ordered by creation time."""
        return list(self.messages.all().order_by("created_at"))

    def add_message(
        self,
        message_type: str,
        content: str,
        role: str = "user",
        metadata: Optional[Dict] = None,
    ) -> "ChatMessage":
        """Add a new message to this chat."""
        return self.messages.create(
            message_type=message_type,
            content=content,
            role=role,
            metadata=metadata or {},
        )

    def get_context_messages(self, limit: int = 10) -> List["ChatMessage"]:
        """Get recent messages for context, excluding system messages."""
        return list(
            self.messages.exclude(message_type=MessageType.SYSTEM_MESSAGE)
            .order_by("-created_at")[:limit]
        )

    def save_workflow_state(self, state: Dict[str, Any]) -> None:
        """Save workflow state for pause/resume functionality."""
        self.workflow_state = state
        self.save(update_fields=["workflow_state", "updated_at"])

    def get_workflow_state(self) -> Dict[str, Any]:
        """Get saved workflow state."""
        return self.workflow_state or {}


class ChatMessage(TimeStampedMixin):
    """
    A message in a user chat. Messages can be of different types and contain metadata.
    Supports OpenAI agent context serialization for workflow pause/resume.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chat = models.ForeignKey(
        UserChat,
        on_delete=models.CASCADE,
        related_name="messages",
        help_text="The chat this message belongs to"
    )
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        help_text="Type of message (user input, assistant response, tool call, etc.)"
    )
    role = models.CharField(
        max_length=20,
        choices=MessageRole.choices,
        help_text="Role of the message sender"
    )
    content = models.TextField(help_text="The message content")
    metadata = models.JSONField(
        default=dict,
        help_text="Additional metadata (tool calls, responses, error details, etc.)"
    )
    # OpenAI agent context for workflow pause/resume
    agent_context = models.JSONField(
        default=dict,
        help_text="Serialized OpenAI agent context for workflow continuity"
    )

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["chat", "created_at"]),
            models.Index(fields=["message_type"]),
        ]

    def __str__(self):
        return f"{self.chat.title}: {self.message_type} - {self.content[:50]}..."

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get a specific metadata value."""
        return self.metadata.get(key, default)

    def set_metadata(self, key: str, value: Any) -> None:
        """Set a metadata value."""
        self.metadata[key] = value
        self.save(update_fields=["metadata"])

    def add_tool_call(
        self, 
        tool_name: str, 
        arguments: Dict[str, Any], 
        result: Optional[Any] = None,
        requires_approval: bool = False
    ) -> None:
        """Add tool call information to metadata."""
        tool_calls = self.metadata.get("tool_calls", [])
        tool_calls.append({
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "requires_approval": requires_approval,
            "timestamp": self.created_at.isoformat(),
        })
        self.metadata["tool_calls"] = tool_calls
        self.save(update_fields=["metadata"])

    def save_agent_context(self, context: Dict[str, Any]) -> None:
        """Save OpenAI agent context for workflow continuity."""
        self.agent_context = context
        self.save(update_fields=["agent_context"])

    def get_agent_context(self) -> Dict[str, Any]:
        """Get saved agent context."""
        return self.agent_context or {}

    def is_tool_message(self) -> bool:
        """Check if this is a tool-related message."""
        return self.message_type in [
            MessageType.TOOL_REQUEST,
            MessageType.TOOL_RESPONSE,
        ]

    def is_user_message(self) -> bool:
        """Check if this is a user message."""
        return self.message_type == MessageType.USER_INPUT

    def is_assistant_message(self) -> bool:
        """Check if this is an assistant message."""
        return self.message_type == MessageType.ASSISTANT_MESSAGE
