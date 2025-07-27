import uuid
import json
from typing import Optional, List, Dict, Any

from django.db import models
from django.contrib.auth import get_user_model
from pgvector.django import VectorField, CosineDistance

from django_ergo.mixins import TimeStampedMixin
from django_ergo.fields import SemanticTextField, semantic_search, vector_search, generate_embedding

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
    """Custom QuerySet for Article model with advanced semantic search capabilities."""
    
    def semantic_search_content(self, query_text: str, top_k: int = 10):
        """
        Perform semantic search on article content.
        
        Args:
            query_text: User's search query
            top_k: Number of results to return
            
        Returns:
            QuerySet: Results ordered by semantic similarity to content
        """
        return SemanticTextField.search_field(
            self.model, 'content', query_text, top_k
        )
    
    def semantic_search_summary(self, query_text: str, top_k: int = 10):
        """
        Perform semantic search on article summaries.
        
        Args:
            query_text: User's search query
            top_k: Number of results to return
            
        Returns:
            QuerySet: Results ordered by semantic similarity to summary
        """
        return SemanticTextField.search_field(
            self.model, 'summary', query_text, top_k
        )
    
    def multi_field_semantic_search(self, query_text: str, top_k: int = 10, weights=None):
        """
        Perform semantic search across multiple fields with optional weighting.
        
        Args:
            query_text: User's search query
            top_k: Number of results to return
            weights: Dict with field weights, e.g. {'content': 0.7, 'summary': 0.3}
            
        Returns:
            QuerySet: Combined results from multiple semantic fields
        """
        if weights is None:
            weights = {'content': 0.6, 'summary': 0.4}
            
        # Generate embedding using the modular function
        query_vector = generate_embedding(query_text)
        
        # Use the modular approach for weighted search
        return self.multi_field_vector_search(query_vector, top_k, weights)
    
    def multi_field_vector_search(self, query_vector: List[float], top_k: int = 10, weights=None):
        """
        Perform vector search across multiple fields with optional weighting using a pre-computed vector.
        
        Args:
            query_vector: Pre-computed embedding vector
            top_k: Number of results to return
            weights: Dict with field weights, e.g. {'content': 0.7, 'summary': 0.3}
            
        Returns:
            QuerySet: Combined results from multiple semantic fields
        """
        if weights is None:
            weights = {'content': 0.6, 'summary': 0.4}
        
        # Build weighted semantic distance calculation
        content_distance = CosineDistance("content_embedding", query_vector) * weights.get('content', 0.6)
        summary_distance = CosineDistance("summary_embedding", query_vector) * weights.get('summary', 0.4)
        
        return (
            self.exclude(content_embedding__isnull=True, summary_embedding__isnull=True)
            .annotate(
                content_distance=content_distance,
                summary_distance=summary_distance,
                combined_distance=content_distance + summary_distance
            )
            .order_by("combined_distance")[:top_k]
        )
    
    def vector_search_content(self, query_vector: List[float], top_k: int = 10):
        """
        Perform low-level vector search on article content using a pre-computed vector.
        
        Args:
            query_vector: Pre-computed embedding vector
            top_k: Number of results to return
            
        Returns:
            QuerySet: Results ordered by semantic similarity to content
        """
        return vector_search(self.model, 'content_embedding', query_vector, top_k)
    
    def vector_search_summary(self, query_vector: List[float], top_k: int = 10):
        """
        Perform low-level vector search on article summaries using a pre-computed vector.
        
        Args:
            query_vector: Pre-computed embedding vector
            top_k: Number of results to return
            
        Returns:
            QuerySet: Results ordered by semantic similarity to summary
        """
        return vector_search(self.model, 'summary_embedding', query_vector, top_k)
    
    def hybrid_search(self, query_text: str, top_k: int = 10):
        """
        Legacy hybrid search method - now uses multi-field semantic search.
        
        Args:
            query_text: User's search query
            top_k: Number of results to return
            
        Returns:
            QuerySet: Top-k relevant results using multi-field semantic search
        """
        return self.multi_field_semantic_search(query_text, top_k)

    def by_hierarchy_prefix(self, prefix: str):
        """Get articles by hierarchy code prefix."""
        return self.filter(hierarchy_code__startswith=prefix)

    def to_prefetch_results(self, top_k: int = 5):
        """Convert queryset to prefetch-friendly format."""
        return [
            {
                "title": article.title,
                "content": article.content,
                "summary": article.summary,
                "hierarchy_code": article.hierarchy_code,
                "id": str(article.id),
            }
            for article in self[:top_k]
        ]


class Article(TimeStampedMixin):
    """
    A model to store a knowledge base article.
    Each article has both semantic text fields and their corresponding embeddings.
    Optimized with pgvector indexes for high-performance semantic search.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hierarchy_code = models.CharField(
        max_length=16,
        default="0",
        db_index=True,
        help_text="The hierarchy code of the article, e.g. '012' (0th chapter, 1st section, 2nd sub-section) or 'C3' (12th chapter, 3rd sub-section)"
    )
    title = models.CharField(max_length=512)
    
    # Semantic text fields with automatic embedding generation
    content = SemanticTextField(help_text="Main article content")
    content_embedding = VectorField(
        dimensions=1536, 
        null=True, 
        blank=True, 
        editable=False,
        help_text="Auto-generated embedding for content"
    )
    
    summary = SemanticTextField(null=True, blank=True, help_text="Article summary")
    summary_embedding = VectorField(
        dimensions=1536, 
        null=True, 
        blank=True, 
        editable=False,
        help_text="Auto-generated embedding for summary"
    )
    
    knowledgebase = models.ForeignKey(
        Knowledgebase,
        on_delete=models.CASCADE,
        related_name="articles"
    )

    objects = ArticleQuerySet.as_manager()

    class Meta:
        ordering = ["hierarchy_code"]
        unique_together = [["knowledgebase", "hierarchy_code"]]
        indexes = [
            models.Index(fields=["knowledgebase", "hierarchy_code"]),
            models.Index(fields=["hierarchy_code"]),
        ]
        # Database settings for vector search optimization
        db_table_comment = "Articles with semantic embeddings and pgvector indexes for fast similarity search"

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
