from typing import Any

from django.contrib.auth import get_user_model
from django.db import models

User = get_user_model()


class MessageRole(models.TextChoices):
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


class Conversation(models.Model):
    """
    A model to store the conversation and workflow state between the user and the agent system.
    """

    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="conversations",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255, default="Untitled Conversation")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Message(models.Model):
    """
    A model to store individual messages in a conversation.
    """

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=20, choices=MessageRole.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:50]}..."


class Workflow(models.Model):
    """
    A workflow defines the AI logic and tools available for processing chat messages.
    Workflows can be associated with knowledgebases and have specific tools and instructions.
    """

    name = models.CharField(
        max_length=255, help_text="Human-readable name for the workflow"
    )
    description = models.TextField(help_text="Description of what this workflow does")
    instructions = models.TextField(help_text="System instructions for the AI agent")
    knowledgebases = models.ManyToManyField(
        "ergo.Knowledgebase",
        blank=True,
        related_name="workflows",
        help_text="Knowledgebases this workflow can access",
    )
    tools_config = models.JSONField(
        default=dict, help_text="Configuration for tools available to this workflow"
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this workflow is available for use"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_tools_config(self) -> dict[str, Any]:
        """Get the tools configuration as a dictionary."""
        return self.tools_config or {}

    def get_knowledgebases_list(self) -> list[str]:
        """Get list of knowledgebase names this workflow can access."""
        return [kb.name for kb in self.knowledgebases.all()]


class UserChat(models.Model):
    """
    A chat owned by a single user, associated with a specific workflow.
    This is the main model for user conversations with AI agents.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chats",
        help_text="The user who owns this chat",
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="chats",
        help_text="The workflow that processes messages in this chat",
    )
    title = models.CharField(
        max_length=255, default="New Chat", help_text="Title of the chat conversation"
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this chat is currently active"
    )
    metadata = models.JSONField(
        default=dict,
        help_text="Additional metadata for the chat (user preferences, context, etc.)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.user.username}: {self.title}"

    def get_messages(self) -> list["ChatMessage"]:
        """Get all messages in this chat, ordered by creation time."""
        return list(self.messages.all().order_by("created_at"))

    def add_message(
        self,
        message_type: str,
        content: str,
        role: str = "user",
        metadata: dict | None = None,
    ) -> "ChatMessage":
        """Add a new message to this chat."""
        return self.messages.create(
            message_type=message_type,
            content=content,
            role=role,
            metadata=metadata or {},
        )

    def get_context_messages(self, limit: int = 10) -> list["ChatMessage"]:
        """Get recent messages for context, excluding system messages."""
        return list(
            self.messages.exclude(message_type=MessageType.SYSTEM_MESSAGE).order_by(
                "-created_at"
            )[:limit]
        )


class ChatMessage(models.Model):
    """
    A message in a user chat. Messages can be of different types and contain metadata.
    """

    chat = models.ForeignKey(
        UserChat,
        on_delete=models.CASCADE,
        related_name="messages",
        help_text="The chat this message belongs to",
    )
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        help_text="Type of message (user input, assistant response, tool call, etc.)",
    )
    role = models.CharField(
        max_length=20,
        choices=MessageRole.choices,
        help_text="Role of the message sender",
    )
    content = models.TextField(help_text="The message content")
    metadata = models.JSONField(
        default=dict,
        help_text="Additional metadata (tool calls, responses, error details, etc.)",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

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
        self, tool_name: str, arguments: dict[str, Any], result: Any | None = None
    ) -> None:
        """Add tool call information to metadata."""
        tool_calls = self.metadata.get("tool_calls", [])
        tool_calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
                "timestamp": self.created_at.isoformat(),
            }
        )
        self.metadata["tool_calls"] = tool_calls
        self.save(update_fields=["metadata"])

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
