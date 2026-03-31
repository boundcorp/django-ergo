# Multi-Engine Conversation Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lossless, engine-native conversation storage with Claude CLI and API engines, tool adapters, session lifecycle management, conversation import, and one-shot typed generation.

**Architecture:** Layered protocol — storage models at bottom, engine ABC in the middle, session manager and tool adapters on top, importers alongside. New code lives in `src/django_ergo/conversation/` subpackage. Existing models/code untouched.

**Tech Stack:** Django 4.2+, PostgreSQL, asyncio, anthropic SDK, openai SDK, Claude CLI (subprocess with stream-json)

**Spec:** `docs/superpowers/specs/2026-03-27-multi-engine-conversation-framework-design.md`

---

## File Structure

```
src/django_ergo/
├── conversation/
│   ├── __init__.py              # Public API exports
│   ├── models.py                # ConversationSession, ClaudeMessage, ClaudeContentBlock, OpenAIMessage
│   ├── engine.py                # Engine ABC, EngineResponse dataclass, TransportFailover exception
│   ├── adapters.py              # ToolAdapter ABC, ClaudeToolAdapter, OpenAIToolAdapter
│   ├── manager.py               # SessionManager
│   ├── runner.py                # run_conversation_turn() async generator
│   ├── engines/
│   │   ├── __init__.py          # Engine registry dict
│   │   ├── claude_cli.py        # ClaudeCLIEngine
│   │   ├── claude_api.py        # ClaudeAPIEngine
│   │   └── openai_api.py        # OpenAIAPIEngine
│   └── importers/
│       ├── __init__.py          # ImportService
│       ├── claude_cli.py        # ClaudeCLIImporter
│       └── openai.py            # OpenAIImporter
├── management/commands/
│   └── import_conversations.py  # Bulk import management command
tests/
├── test_conversation_models.py  # Model creation, relationships, ordering
├── test_conversation_adapters.py # Tool adapter schema conversion
├── test_conversation_engine.py  # Engine ABC contract, reconstruct_messages
├── test_conversation_claude_api.py # ClaudeAPIEngine reconstruct + tools
├── test_conversation_claude_cli.py # ClaudeCLIEngine health check, failover
├── test_conversation_openai_api.py # OpenAIAPIEngine reconstruct
├── test_conversation_runner.py  # run_conversation_turn with mock engine
├── test_conversation_manager.py # SessionManager lifecycle, failover
├── test_conversation_import.py  # Importer parsing, format detection
├── test_conversation_generate.py # Engine.generate() one-shot typed output
├── test_import_command.py       # Management command
```

---

### Task 1: Conversation Models

**Files:**
- Create: `src/django_ergo/conversation/__init__.py`
- Create: `src/django_ergo/conversation/models.py`
- Modify: `src/django_ergo/apps.py`
- Test: `tests/test_conversation_models.py`

- [ ] **Step 1: Write failing tests for conversation models**

```python
# tests/test_conversation_models.py
"""Tests for conversation storage models."""
import pytest
import uuid
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import (
    ConversationSession,
    ClaudeMessage,
    ClaudeContentBlock,
    OpenAIMessage,
)

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="cli",
        status="active",
    )


class TestConversationSession:
    def test_create_session(self, user):
        session = ConversationSession.objects.create(
            user=user,
            engine_type="claude",
            transport_type="cli",
            status="active",
            metadata={"model": "claude-opus-4-6"},
        )
        assert session.id is not None
        assert session.user == user
        assert session.engine_type == "claude"
        assert session.transport_type == "cli"
        assert session.status == "active"
        assert session.workflow is None
        assert session.metadata["model"] == "claude-opus-4-6"
        assert session.created_at is not None

    def test_session_with_workflow(self, user):
        from django_ergo.models import Workflow

        workflow = Workflow.objects.create(
            name="Test Workflow",
            description="Test",
            instructions="You are a test assistant.",
        )
        session = ConversationSession.objects.create(
            user=user,
            workflow=workflow,
            engine_type="openai",
            transport_type="api",
            status="paused",
        )
        assert session.workflow == workflow

    def test_session_ordering(self, user):
        s1 = ConversationSession.objects.create(
            user=user, engine_type="claude", transport_type="cli", status="active"
        )
        s2 = ConversationSession.objects.create(
            user=user, engine_type="claude", transport_type="api", status="active"
        )
        sessions = list(ConversationSession.objects.filter(user=user))
        # Most recent first
        assert sessions[0] == s2
        assert sessions[1] == s1


class TestClaudeMessage:
    def test_create_message(self, session):
        msg = ClaudeMessage.objects.create(
            session=session,
            role="user",
            sequence=0,
        )
        assert msg.id is not None
        assert msg.role == "user"
        assert msg.sequence == 0
        assert msg.stop_reason is None

    def test_message_ordering(self, session):
        m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
        m1 = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=1, stop_reason="end_turn"
        )
        msgs = list(session.claude_messages.all())
        assert msgs[0] == m0
        assert msgs[1] == m1

    def test_message_with_usage(self, session):
        msg = ClaudeMessage.objects.create(
            session=session,
            role="assistant",
            sequence=0,
            input_tokens=100,
            output_tokens=50,
            stop_reason="end_turn",
        )
        assert msg.input_tokens == 100
        assert msg.output_tokens == 50


class TestClaudeContentBlock:
    def test_text_block(self, session):
        msg = ClaudeMessage.objects.create(session=session, role="assistant", sequence=0)
        block = ClaudeContentBlock.objects.create(
            message=msg,
            block_type="text",
            sequence=0,
            text="Hello, world!",
        )
        assert block.block_type == "text"
        assert block.text == "Hello, world!"

    def test_thinking_block(self, session):
        msg = ClaudeMessage.objects.create(session=session, role="assistant", sequence=0)
        block = ClaudeContentBlock.objects.create(
            message=msg,
            block_type="thinking",
            sequence=0,
            thinking="Let me analyze this...",
        )
        assert block.block_type == "thinking"
        assert block.thinking == "Let me analyze this..."

    def test_tool_use_block(self, session):
        msg = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=0, stop_reason="tool_use"
        )
        block = ClaudeContentBlock.objects.create(
            message=msg,
            block_type="tool_use",
            sequence=0,
            tool_use_id="toolu_01ABC",
            tool_name="Bash",
            tool_input={"command": "ls -la"},
        )
        assert block.tool_use_id == "toolu_01ABC"
        assert block.tool_name == "Bash"
        assert block.tool_input == {"command": "ls -la"}

    def test_tool_result_block(self, session):
        msg = ClaudeMessage.objects.create(session=session, role="user", sequence=1)
        block = ClaudeContentBlock.objects.create(
            message=msg,
            block_type="tool_result",
            sequence=0,
            tool_result_for="toolu_01ABC",
            tool_result_content="file1.py\nfile2.py",
            is_error=False,
        )
        assert block.tool_result_for == "toolu_01ABC"
        assert block.is_error is False

    def test_block_ordering_within_message(self, session):
        msg = ClaudeMessage.objects.create(session=session, role="assistant", sequence=0)
        b0 = ClaudeContentBlock.objects.create(
            message=msg, block_type="text", sequence=0, text="I'll read the file."
        )
        b1 = ClaudeContentBlock.objects.create(
            message=msg,
            block_type="tool_use",
            sequence=1,
            tool_use_id="toolu_01",
            tool_name="Read",
            tool_input={"file_path": "/tmp/test.py"},
        )
        blocks = list(msg.content_blocks.all())
        assert blocks[0] == b0
        assert blocks[1] == b1


class TestOpenAIMessage:
    def test_create_user_message(self, user):
        session = ConversationSession.objects.create(
            user=user, engine_type="openai", transport_type="api", status="active"
        )
        msg = OpenAIMessage.objects.create(
            session=session,
            role="user",
            content="Hello",
            sequence=0,
        )
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_create_assistant_with_tool_calls(self, user):
        session = ConversationSession.objects.create(
            user=user, engine_type="openai", transport_type="api", status="active"
        )
        tool_calls = [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": "test"}'},
            }
        ]
        msg = OpenAIMessage.objects.create(
            session=session,
            role="assistant",
            content=None,
            tool_calls=tool_calls,
            sequence=0,
        )
        assert msg.tool_calls[0]["id"] == "call_abc"

    def test_create_tool_response(self, user):
        session = ConversationSession.objects.create(
            user=user, engine_type="openai", transport_type="api", status="active"
        )
        msg = OpenAIMessage.objects.create(
            session=session,
            role="tool",
            content='{"results": []}',
            tool_call_id="call_abc",
            function_name="search",
            sequence=1,
        )
        assert msg.tool_call_id == "call_abc"
        assert msg.function_name == "search"

    def test_message_ordering(self, user):
        session = ConversationSession.objects.create(
            user=user, engine_type="openai", transport_type="api", status="active"
        )
        m0 = OpenAIMessage.objects.create(session=session, role="user", content="Hi", sequence=0)
        m1 = OpenAIMessage.objects.create(
            session=session, role="assistant", content="Hello!", sequence=1
        )
        msgs = list(session.openai_messages.all())
        assert msgs[0] == m0
        assert msgs[1] == m1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'django_ergo.conversation'`

- [ ] **Step 3: Create the conversation subpackage and models**

Create `src/django_ergo/conversation/__init__.py`:
```python
"""
Multi-engine conversation framework for django-ergo.

Provides lossless, engine-native conversation storage and management
for Claude (CLI + API) and OpenAI engines.
"""
```

Create `src/django_ergo/conversation/models.py`:
```python
import uuid

from django.contrib.auth import get_user_model
from django.db import models

from django_ergo.mixins import TimeStampedMixin
from django_ergo.models import Workflow

User = get_user_model()


class ConversationSession(TimeStampedMixin):
    """Engine-agnostic conversation session container."""

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
        choices=[("claude", "Claude"), ("openai", "OpenAI")],
    )
    transport_type = models.CharField(
        max_length=20,
        choices=[("cli", "CLI"), ("api", "API")],
    )
    session_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=[
            ("active", "Active"),
            ("paused", "Paused"),
            ("completed", "Completed"),
            ("failed", "Failed"),
        ],
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
        return f"{self.user} [{self.engine_type}/{self.transport_type}] {self.status}"


class ClaudeMessage(TimeStampedMixin):
    """A single message in a Claude conversation. Contains content blocks."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="claude_messages",
    )
    role = models.CharField(
        max_length=20,
        choices=[("user", "User"), ("assistant", "Assistant")],
    )
    stop_reason = models.CharField(max_length=30, null=True, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    sequence = models.IntegerField()

    class Meta:
        ordering = ["sequence"]
        indexes = [
            models.Index(fields=["session", "sequence"]),
        ]

    def __str__(self):
        return f"[{self.sequence}] {self.role}"


class ClaudeContentBlock(TimeStampedMixin):
    """A single content block within a Claude message."""

    message = models.ForeignKey(
        ClaudeMessage,
        on_delete=models.CASCADE,
        related_name="content_blocks",
    )
    block_type = models.CharField(
        max_length=20,
        choices=[
            ("text", "Text"),
            ("tool_use", "Tool Use"),
            ("tool_result", "Tool Result"),
            ("thinking", "Thinking"),
        ],
    )
    sequence = models.IntegerField()

    # Text blocks
    text = models.TextField(null=True, blank=True)

    # Thinking blocks
    thinking = models.TextField(null=True, blank=True)

    # Tool use blocks
    tool_use_id = models.CharField(max_length=255, null=True, blank=True)
    tool_name = models.CharField(max_length=255, null=True, blank=True)
    tool_input = models.JSONField(null=True, blank=True)

    # Tool result blocks
    tool_result_for = models.CharField(max_length=255, null=True, blank=True)
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
        return f"[{self.sequence}] {self.block_type}"


class OpenAIMessage(TimeStampedMixin):
    """A single message in an OpenAI conversation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="openai_messages",
    )
    role = models.CharField(
        max_length=20,
        choices=[
            ("user", "User"),
            ("assistant", "Assistant"),
            ("system", "System"),
            ("tool", "Tool"),
        ],
    )
    content = models.TextField(null=True, blank=True)
    tool_calls = models.JSONField(null=True, blank=True)
    tool_call_id = models.CharField(max_length=255, null=True, blank=True)
    function_name = models.CharField(max_length=255, null=True, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    sequence = models.IntegerField()

    class Meta:
        ordering = ["sequence"]
        indexes = [
            models.Index(fields=["session", "sequence"]),
        ]

    def __str__(self):
        return f"[{self.sequence}] {self.role}"
```

Update `src/django_ergo/apps.py` to ensure the conversation models are discovered:
```python
from django.apps import AppConfig


class DjangoErgoConfig(AppConfig):
    name = "django_ergo"
    verbose_name = "django-ergo"
    default_auto_field = "django.db.models.AutoField"
```

No change needed in `apps.py` — Django auto-discovers models in submodules. But we do need to import the models in `conversation/__init__.py` so they're registered:

Update `src/django_ergo/conversation/__init__.py`:
```python
"""
Multi-engine conversation framework for django-ergo.
"""
from django_ergo.conversation.models import (
    ConversationSession,
    ClaudeMessage,
    ClaudeContentBlock,
    OpenAIMessage,
)

__all__ = [
    "ConversationSession",
    "ClaudeMessage",
    "ClaudeContentBlock",
    "OpenAIMessage",
]
```

- [ ] **Step 4: Generate and run migration**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m django makemigrations django_ergo --settings=tests.example_app.settings`
Then: `cd /home/linked/p/boundcorp/django-ergo && python -m django migrate --settings=tests.example_app.settings`

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_models.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/conversation/ tests/test_conversation_models.py src/django_ergo/migrations/
git commit -m "feat: add conversation storage models (ConversationSession, ClaudeMessage, ClaudeContentBlock, OpenAIMessage)"
```

---

### Task 2: Engine Protocol and EngineResponse

**Files:**
- Create: `src/django_ergo/conversation/engine.py`
- Test: `tests/test_conversation_engine.py`

- [ ] **Step 1: Write failing tests for engine protocol**

```python
# tests/test_conversation_engine.py
"""Tests for the engine protocol ABC and EngineResponse."""
import pytest
from dataclasses import asdict
from django_ergo.conversation.engine import EngineResponse, Engine, TransportFailover


class TestEngineResponse:
    def test_text_response(self):
        r = EngineResponse(event_type="text", raw={"type": "text"}, text="Hello")
        assert r.event_type == "text"
        assert r.text == "Hello"
        assert r.tool_use is None
        assert r.thinking is None

    def test_tool_use_response(self):
        tool = {"id": "toolu_01", "name": "Bash", "input": {"command": "ls"}}
        r = EngineResponse(event_type="tool_use", raw={"type": "tool_use"}, tool_use=tool)
        assert r.event_type == "tool_use"
        assert r.tool_use["name"] == "Bash"

    def test_thinking_response(self):
        r = EngineResponse(
            event_type="thinking", raw={"type": "thinking"}, thinking="Let me think..."
        )
        assert r.thinking == "Let me think..."

    def test_done_response(self):
        r = EngineResponse(event_type="done", raw={"stop_reason": "end_turn"})
        assert r.event_type == "done"

    def test_error_response(self):
        r = EngineResponse(event_type="error", raw={"error": "timeout"}, text="Request timed out")
        assert r.event_type == "error"


class TestTransportFailover:
    def test_failover_exception(self):
        exc = TransportFailover(original="cli", fallback="api", reason="CLI session not found")
        assert exc.original == "cli"
        assert exc.fallback == "api"
        assert "CLI session not found" in str(exc)


class TestEngineABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            Engine()

    def test_engine_requires_all_methods(self):
        """Verify the ABC enforces the full protocol."""
        required_methods = [
            "start_session",
            "resume_session",
            "send",
            "submit_tool_result",
            "get_tools_schema",
            "reconstruct_messages",
            "close_session",
            "get_tool_adapter",
        ]
        for method_name in required_methods:
            assert hasattr(Engine, method_name), f"Engine missing {method_name}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_engine.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement engine protocol**

Create `src/django_ergo/conversation/engine.py`:
```python
"""Engine protocol ABC and shared types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from django_ergo.conversation.models import ConversationSession
from django_ergo.models import Workflow


@dataclass
class EngineResponse:
    """Yielded from Engine.send() — wraps engine-native events."""

    event_type: str  # "text", "tool_use", "thinking", "done", "error"
    raw: dict = field(default_factory=dict)
    text: str | None = None
    tool_use: dict | None = None  # {"id": ..., "name": ..., "input": ...}
    thinking: str | None = None


class TransportFailover(Exception):
    """Raised when a transport fails and should be swapped."""

    def __init__(self, original: str, fallback: str, reason: str):
        self.original = original
        self.fallback = fallback
        super().__init__(f"Transport failover {original} -> {fallback}: {reason}")


class Engine(ABC):
    """Abstract engine protocol. All engines implement this interface."""

    engine_type: str  # "claude" or "openai"

    @abstractmethod
    async def start_session(self, session: ConversationSession) -> str:
        """Start a new session. Returns engine-native session ID."""

    @abstractmethod
    async def resume_session(self, session: ConversationSession) -> None:
        """Resume an existing session from DB state."""

    @abstractmethod
    async def send(
        self, session: ConversationSession, message: str
    ) -> AsyncIterator[EngineResponse]:
        """Send a message, yield streaming responses. Persists to DB."""

    @abstractmethod
    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        """Submit a tool result, yield assistant continuation."""

    @abstractmethod
    def get_tools_schema(self, workflow: Workflow) -> list[dict]:
        """Convert ergo tools to engine-native tool format."""

    @abstractmethod
    def reconstruct_messages(self, session: ConversationSession) -> list[dict]:
        """Build engine-native message history from DB for resume/export."""

    @abstractmethod
    async def close_session(self, session: ConversationSession) -> None:
        """Clean up resources."""

    @abstractmethod
    def get_tool_adapter(self):
        """Return the ToolAdapter for this engine."""

    async def generate(
        self,
        prompt: str,
        workflow: Workflow | None = None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        """One-shot generation. No session required.

        If response_model is provided (a Pydantic BaseModel subclass),
        forces structured output via tool_use and returns parsed result
        in EngineResponse.raw["parsed"].
        """
        raise NotImplementedError("This engine does not support one-shot generation")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_engine.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/engine.py tests/test_conversation_engine.py
git commit -m "feat: add Engine ABC, EngineResponse, and TransportFailover"
```

---

### Task 3: Tool Adapters

**Files:**
- Create: `src/django_ergo/conversation/adapters.py`
- Test: `tests/test_conversation_adapters.py`

- [ ] **Step 1: Write failing tests for tool adapters**

```python
# tests/test_conversation_adapters.py
"""Tests for tool adapters that convert ergo tools to engine-native formats."""
import pytest
from django_ergo.conversation.adapters import (
    ToolAdapter,
    ClaudeToolAdapter,
    OpenAIToolAdapter,
)
from django_ergo.tools import ToolConfig


@pytest.fixture
def sample_tool():
    return ToolConfig(
        name="search_kb",
        description="Search the knowledge base",
        parameters={
            "query": {"type": "string", "required": True, "description": "Search query"},
            "top_k": {"type": "integer", "required": False, "default": 5},
        },
        requires_approval=False,
        readonly=True,
    )


@pytest.fixture
def approval_tool():
    return ToolConfig(
        name="delete_article",
        description="Delete an article from the knowledge base",
        parameters={
            "article_id": {"type": "string", "required": True, "description": "Article UUID"},
        },
        requires_approval=True,
        readonly=False,
    )


class TestClaudeToolAdapter:
    def setup_method(self):
        self.adapter = ClaudeToolAdapter()

    def test_to_engine_schema(self, sample_tool):
        schema = self.adapter.to_engine_schema(sample_tool)
        assert schema["name"] == "search_kb"
        assert schema["description"] == "Search the knowledge base"
        assert schema["input_schema"]["type"] == "object"
        assert "query" in schema["input_schema"]["properties"]
        assert "top_k" in schema["input_schema"]["properties"]
        assert "query" in schema["input_schema"]["required"]
        assert "top_k" not in schema["input_schema"]["required"]

    def test_parse_tool_call(self):
        raw = {"id": "toolu_01", "name": "search_kb", "input": {"query": "django"}}
        name, args = self.adapter.parse_tool_call(raw)
        assert name == "search_kb"
        assert args == {"query": "django"}

    def test_format_tool_result(self):
        result = self.adapter.format_tool_result("toolu_01", "Found 3 results", False)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "toolu_01"
        assert result["content"] == "Found 3 results"
        assert result["is_error"] is False

    def test_format_tool_result_error(self):
        result = self.adapter.format_tool_result("toolu_01", "Not found", True)
        assert result["is_error"] is True


class TestOpenAIToolAdapter:
    def setup_method(self):
        self.adapter = OpenAIToolAdapter()

    def test_to_engine_schema(self, sample_tool):
        schema = self.adapter.to_engine_schema(sample_tool)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search_kb"
        assert schema["function"]["description"] == "Search the knowledge base"
        assert schema["function"]["parameters"]["type"] == "object"
        assert "query" in schema["function"]["parameters"]["properties"]
        assert "query" in schema["function"]["parameters"]["required"]

    def test_parse_tool_call(self):
        raw = {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "search_kb", "arguments": '{"query": "django"}'},
        }
        name, args = self.adapter.parse_tool_call(raw)
        assert name == "search_kb"
        assert args == {"query": "django"}

    def test_format_tool_result(self):
        result = self.adapter.format_tool_result("call_abc", "Found 3 results", False)
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "call_abc"
        assert result["content"] == "Found 3 results"

    def test_format_tool_result_error(self):
        result = self.adapter.format_tool_result("call_abc", "Error occurred", True)
        assert result["content"] == "Error occurred"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_adapters.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement tool adapters**

Create `src/django_ergo/conversation/adapters.py`:
```python
"""Tool adapters for converting ergo tools to engine-native formats."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from django_ergo.tools import ToolConfig


class ToolAdapter(ABC):
    """Converts ergo tool definitions to/from engine-native formats."""

    @abstractmethod
    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        """Convert an ergo tool definition to engine-native format."""

    @abstractmethod
    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        """Extract (tool_name, arguments) from engine-native tool call."""

    @abstractmethod
    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        """Format tool result for engine-native submission."""


class ClaudeToolAdapter(ToolAdapter):
    """Adapter for Claude's tool format."""

    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        properties = {}
        required = []
        for param_name, param_info in tool_config.parameters.items():
            properties[param_name] = {
                "type": param_info.get("type", "string"),
            }
            if "description" in param_info:
                properties[param_name]["description"] = param_info["description"]
            if param_info.get("required"):
                required.append(param_name)

        return {
            "name": tool_config.name,
            "description": tool_config.description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        return raw["name"], raw["input"]

    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": str(result),
            "is_error": is_error,
        }


class OpenAIToolAdapter(ToolAdapter):
    """Adapter for OpenAI's tool format."""

    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        properties = {}
        required = []
        for param_name, param_info in tool_config.parameters.items():
            properties[param_name] = {
                "type": param_info.get("type", "string"),
            }
            if "description" in param_info:
                properties[param_name]["description"] = param_info["description"]
            if param_info.get("required"):
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": tool_config.name,
                "description": tool_config.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        return raw["function"]["name"], json.loads(raw["function"]["arguments"])

    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_use_id,
            "content": str(result),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_adapters.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/adapters.py tests/test_conversation_adapters.py
git commit -m "feat: add ClaudeToolAdapter and OpenAIToolAdapter"
```

---

### Task 4: Claude API Engine

**Files:**
- Create: `src/django_ergo/conversation/engines/__init__.py`
- Create: `src/django_ergo/conversation/engines/claude_api.py`
- Test: `tests/test_conversation_claude_api.py`

- [ ] **Step 1: Write failing tests for Claude API engine**

```python
# tests/test_conversation_claude_api.py
"""Tests for ClaudeAPIEngine — uses mocked Anthropic SDK."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import (
    ConversationSession,
    ClaudeMessage,
    ClaudeContentBlock,
)
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        metadata={"model": "claude-sonnet-4-6"},
    )


@pytest.fixture
def engine():
    return ClaudeAPIEngine(config={"model": "claude-sonnet-4-6", "api_key": "test-key"})


class TestClaudeAPIEngineReconstructMessages:
    def test_reconstruct_empty_session(self, engine, session):
        messages = engine.reconstruct_messages(session)
        assert messages == []

    def test_reconstruct_text_conversation(self, engine, session):
        m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
        ClaudeContentBlock.objects.create(
            message=m0, block_type="text", sequence=0, text="Hello"
        )

        m1 = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=1, stop_reason="end_turn"
        )
        ClaudeContentBlock.objects.create(
            message=m1, block_type="text", sequence=0, text="Hi there!"
        )

        messages = engine.reconstruct_messages(session)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == [{"type": "text", "text": "Hello"}]
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == [{"type": "text", "text": "Hi there!"}]

    def test_reconstruct_with_tool_use(self, engine, session):
        m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
        ClaudeContentBlock.objects.create(
            message=m0, block_type="text", sequence=0, text="List files"
        )

        m1 = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=1, stop_reason="tool_use"
        )
        ClaudeContentBlock.objects.create(
            message=m1,
            block_type="tool_use",
            sequence=0,
            tool_use_id="toolu_01",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )

        m2 = ClaudeMessage.objects.create(session=session, role="user", sequence=2)
        ClaudeContentBlock.objects.create(
            message=m2,
            block_type="tool_result",
            sequence=0,
            tool_result_for="toolu_01",
            tool_result_content="file1.py\nfile2.py",
            is_error=False,
        )

        messages = engine.reconstruct_messages(session)
        assert len(messages) == 3
        assert messages[1]["content"][0]["type"] == "tool_use"
        assert messages[1]["content"][0]["id"] == "toolu_01"
        assert messages[2]["content"][0]["type"] == "tool_result"
        assert messages[2]["content"][0]["tool_use_id"] == "toolu_01"

    def test_reconstruct_with_thinking(self, engine, session):
        m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
        ClaudeContentBlock.objects.create(
            message=m0, block_type="text", sequence=0, text="Complex question"
        )

        m1 = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=1, stop_reason="end_turn"
        )
        ClaudeContentBlock.objects.create(
            message=m1, block_type="thinking", sequence=0, thinking="Let me reason..."
        )
        ClaudeContentBlock.objects.create(
            message=m1, block_type="text", sequence=1, text="Here's my answer."
        )

        messages = engine.reconstruct_messages(session)
        assert len(messages) == 2
        assert messages[1]["content"][0]["type"] == "thinking"
        assert messages[1]["content"][1]["type"] == "text"


class TestClaudeAPIEngineToolsSchema:
    def test_get_tools_schema(self, engine):
        from django_ergo.models import Workflow
        from django_ergo.tools import tool_registry, ToolConfig

        workflow = Workflow(
            name="test",
            description="test",
            instructions="test",
            tools_config={"enabled_tools": ["search_kb"]},
        )

        # Register a test tool
        tool_registry._tools["search_kb"] = ToolConfig(
            name="search_kb",
            description="Search KB",
            parameters={"query": {"type": "string", "required": True}},
        )

        try:
            schema = engine.get_tools_schema(workflow)
            assert len(schema) == 1
            assert schema[0]["name"] == "search_kb"
            assert "input_schema" in schema[0]
        finally:
            del tool_registry._tools["search_kb"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_claude_api.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Claude API engine**

Create `src/django_ergo/conversation/engines/__init__.py`:
```python
"""Engine implementations."""
ENGINE_REGISTRY = {
    ("claude", "cli"): "django_ergo.conversation.engines.claude_cli.ClaudeCLIEngine",
    ("claude", "api"): "django_ergo.conversation.engines.claude_api.ClaudeAPIEngine",
    ("openai", "api"): "django_ergo.conversation.engines.openai_api.OpenAIAPIEngine",
}
```

Create `src/django_ergo/conversation/engines/claude_api.py`:
```python
"""Claude API engine — uses the anthropic SDK."""
from __future__ import annotations

from typing import Any, AsyncIterator

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.engine import Engine, EngineResponse
from django_ergo.conversation.models import (
    ClaudeContentBlock,
    ClaudeMessage,
    ConversationSession,
)
from django_ergo.models import Workflow
from django_ergo.tools import tool_registry


class ClaudeAPIEngine(Engine):
    engine_type = "claude"

    def __init__(self, config: dict):
        self.model = config.get("model", "claude-sonnet-4-6")
        self.api_key = config.get("api_key")
        self.max_tokens = config.get("max_tokens", 8192)
        self._adapter = ClaudeToolAdapter()
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def start_session(self, session: ConversationSession) -> str:
        # API is stateless — session ID is just our DB ID
        return str(session.id)

    async def resume_session(self, session: ConversationSession) -> None:
        # API is stateless — nothing to resume
        pass

    async def send(
        self, session: ConversationSession, message: str
    ) -> AsyncIterator[EngineResponse]:
        client = self._get_client()

        # Persist user message
        seq = session.claude_messages.count()
        user_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=user_msg, block_type="text", sequence=0, text=message
        )

        # Build full message history
        messages = self.reconstruct_messages(session)

        # Get tools if workflow configured
        tools = None
        if session.workflow:
            tools = self.get_tools_schema(session.workflow) or None

        # Stream response
        async with client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            tools=tools,
        ) as stream:
            assistant_msg = await ClaudeMessage.objects.acreate(
                session=session, role="assistant", sequence=seq + 1
            )
            block_seq = 0

            async for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "text":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="text",
                            sequence=block_seq,
                            text="",
                        )
                    elif block.type == "tool_use":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="tool_use",
                            sequence=block_seq,
                            tool_use_id=block.id,
                            tool_name=block.name,
                            tool_input={},
                        )
                    block_seq += 1

                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield EngineResponse(
                            event_type="text",
                            raw={"type": "text_delta"},
                            text=event.delta.text,
                        )
                    elif hasattr(event.delta, "thinking"):
                        yield EngineResponse(
                            event_type="thinking",
                            raw={"type": "thinking_delta"},
                            thinking=event.delta.thinking,
                        )

                elif event.type == "message_stop":
                    final = await stream.get_final_message()
                    assistant_msg.stop_reason = final.stop_reason
                    assistant_msg.input_tokens = final.usage.input_tokens
                    assistant_msg.output_tokens = final.usage.output_tokens
                    await assistant_msg.asave()

                    # Update content blocks with final content
                    for i, block in enumerate(final.content):
                        db_block = await assistant_msg.content_blocks.filter(
                            sequence=i
                        ).afirst()
                        if db_block and block.type == "text":
                            db_block.text = block.text
                            await db_block.asave()
                        elif db_block and block.type == "tool_use":
                            db_block.tool_input = block.input
                            await db_block.asave()
                            yield EngineResponse(
                                event_type="tool_use",
                                raw={"type": "tool_use"},
                                tool_use={
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                },
                            )
                        elif db_block and block.type == "thinking":
                            db_block.thinking = block.thinking
                            await db_block.asave()

                    yield EngineResponse(
                        event_type="done",
                        raw={"stop_reason": final.stop_reason},
                    )

    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        # Persist tool result as user message
        seq = session.claude_messages.count()
        result_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=result_msg,
            block_type="tool_result",
            sequence=0,
            tool_result_for=tool_use_id,
            tool_result_content=str(result),
            is_error=is_error,
        )

        # Rebuild and send
        messages = self.reconstruct_messages(session)
        client = self._get_client()

        tools = None
        if session.workflow:
            tools = self.get_tools_schema(session.workflow) or None

        async with client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            tools=tools,
        ) as stream:
            assistant_msg = await ClaudeMessage.objects.acreate(
                session=session, role="assistant", sequence=seq + 1
            )
            block_seq = 0

            async for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "text":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="text",
                            sequence=block_seq,
                            text="",
                        )
                    elif block.type == "tool_use":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="tool_use",
                            sequence=block_seq,
                            tool_use_id=block.id,
                            tool_name=block.name,
                            tool_input={},
                        )
                    block_seq += 1

                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield EngineResponse(
                            event_type="text", raw={"type": "text_delta"}, text=event.delta.text
                        )

                elif event.type == "message_stop":
                    final = await stream.get_final_message()
                    assistant_msg.stop_reason = final.stop_reason
                    assistant_msg.input_tokens = final.usage.input_tokens
                    assistant_msg.output_tokens = final.usage.output_tokens
                    await assistant_msg.asave()

                    for i, block in enumerate(final.content):
                        db_block = await assistant_msg.content_blocks.filter(
                            sequence=i
                        ).afirst()
                        if db_block and block.type == "text":
                            db_block.text = block.text
                            await db_block.asave()
                        elif db_block and block.type == "tool_use":
                            db_block.tool_input = block.input
                            await db_block.asave()
                            yield EngineResponse(
                                event_type="tool_use",
                                raw={"type": "tool_use"},
                                tool_use={
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                },
                            )

                    yield EngineResponse(
                        event_type="done", raw={"stop_reason": final.stop_reason}
                    )

    def get_tools_schema(self, workflow: Workflow) -> list[dict]:
        tools_config = workflow.get_tools_config()
        if not tools_config:
            return []
        schemas = []
        for tool_name in tools_config.get("enabled_tools", []):
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config:
                schemas.append(self._adapter.to_engine_schema(tool_config))
        return schemas

    def reconstruct_messages(self, session: ConversationSession) -> list[dict]:
        messages = []
        for msg in session.claude_messages.prefetch_related("content_blocks").all():
            content = []
            for block in msg.content_blocks.all():
                if block.block_type == "text":
                    content.append({"type": "text", "text": block.text})
                elif block.block_type == "thinking":
                    content.append({"type": "thinking", "thinking": block.thinking})
                elif block.block_type == "tool_use":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.tool_use_id,
                            "name": block.tool_name,
                            "input": block.tool_input or {},
                        }
                    )
                elif block.block_type == "tool_result":
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_result_for,
                            "content": block.tool_result_content or "",
                            "is_error": block.is_error,
                        }
                    )
            messages.append({"role": msg.role, "content": content})
        return messages

    async def close_session(self, session: ConversationSession) -> None:
        # API is stateless — nothing to close
        pass

    def get_tool_adapter(self):
        return self._adapter
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_claude_api.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/engines/ tests/test_conversation_claude_api.py
git commit -m "feat: add ClaudeAPIEngine with reconstruct_messages and streaming"
```

---

### Task 5: Claude CLI Engine

**Files:**
- Create: `src/django_ergo/conversation/engines/claude_cli.py`
- Test: `tests/test_conversation_claude_cli.py`

- [ ] **Step 1: Write failing tests for Claude CLI engine**

```python
# tests/test_conversation_claude_cli.py
"""Tests for ClaudeCLIEngine — subprocess management with mocked process."""
import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import (
    ConversationSession,
    ClaudeMessage,
    ClaudeContentBlock,
)
from django_ergo.conversation.engines.claude_cli import ClaudeCLIEngine
from django_ergo.conversation.engine import TransportFailover

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="cli",
        status="active",
        session_id="test-session-123",
    )


@pytest.fixture
def engine():
    return ClaudeCLIEngine(config={})


class TestClaudeCLIEngineReconstructMessages:
    """reconstruct_messages shares logic with ClaudeAPIEngine — verify it works."""

    def test_reconstruct_empty(self, engine, session):
        messages = engine.reconstruct_messages(session)
        assert messages == []

    def test_reconstruct_roundtrip(self, engine, session):
        m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
        ClaudeContentBlock.objects.create(
            message=m0, block_type="text", sequence=0, text="Hello"
        )
        m1 = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=1, stop_reason="end_turn"
        )
        ClaudeContentBlock.objects.create(
            message=m1, block_type="text", sequence=0, text="Hi!"
        )

        messages = engine.reconstruct_messages(session)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"


class TestClaudeCLIEngineHealthCheck:
    def test_healthy_process(self, engine):
        engine.process = MagicMock()
        engine.process.returncode = None
        assert engine._health_check() is True

    def test_dead_process(self, engine):
        engine.process = MagicMock()
        engine.process.returncode = 1
        assert engine._health_check() is False

    def test_no_process(self, engine):
        assert engine._health_check() is False


class TestClaudeCLIEngineResume:
    @pytest.mark.asyncio
    async def test_resume_raises_failover_on_error(self, engine, session):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("claude not found")):
            with pytest.raises(TransportFailover) as exc_info:
                await engine.resume_session(session)
            assert exc_info.value.original == "cli"
            assert exc_info.value.fallback == "api"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_claude_cli.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Claude CLI engine**

Create `src/django_ergo/conversation/engines/claude_cli.py`:
```python
"""Claude CLI engine — drives a claude subprocess with stream-json."""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.engine import Engine, EngineResponse, TransportFailover
from django_ergo.conversation.models import (
    ClaudeContentBlock,
    ClaudeMessage,
    ConversationSession,
)
from django_ergo.models import Workflow
from django_ergo.tools import tool_registry


class ClaudeCLIEngine(Engine):
    """Engine that drives Claude CLI as a subprocess with stream-json output."""

    engine_type = "claude"

    def __init__(self, config: dict):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._adapter = ClaudeToolAdapter()

    def _health_check(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start_session(self, session: ConversationSession) -> str:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
        ]

        if session.workflow:
            tools = self.get_tools_schema(session.workflow)
            if tools:
                tool_names = [t["name"] for t in tools]
                cmd.extend(["--allowedTools", ",".join(tool_names)])

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Read init event to get session ID
        init_line = await self.process.stdout.readline()
        if init_line:
            try:
                init_event = json.loads(init_line)
                return init_event.get("session_id", str(session.id))
            except json.JSONDecodeError:
                pass
        return str(session.id)

    async def resume_session(self, session: ConversationSession) -> None:
        try:
            cmd = [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--resume",
                session.session_id,
            ]
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            raise TransportFailover(
                original="cli",
                fallback="api",
                reason=f"CLI session not available: {e}",
            ) from e

    async def send(
        self, session: ConversationSession, message: str
    ) -> AsyncIterator[EngineResponse]:
        if not self._health_check():
            raise TransportFailover(
                original="cli",
                fallback="api",
                reason="CLI process is not running",
            )

        # Persist user message
        seq = session.claude_messages.count()
        user_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=user_msg, block_type="text", sequence=0, text=message
        )

        # Send to subprocess
        self.process.stdin.write((message + "\n").encode())
        await self.process.stdin.drain()

        # Read NDJSON stream
        assistant_msg = await ClaudeMessage.objects.acreate(
            session=session, role="assistant", sequence=seq + 1
        )
        block_seq = 0

        while True:
            line = await self.process.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if event_type == "assistant" and "content" in event.get("message", {}):
                # Final message with all content blocks
                msg_data = event["message"]
                for block_data in msg_data.get("content", []):
                    if block_data["type"] == "text":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="text",
                            sequence=block_seq,
                            text=block_data["text"],
                        )
                        yield EngineResponse(
                            event_type="text",
                            raw=block_data,
                            text=block_data["text"],
                        )
                    elif block_data["type"] == "tool_use":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="tool_use",
                            sequence=block_seq,
                            tool_use_id=block_data["id"],
                            tool_name=block_data["name"],
                            tool_input=block_data.get("input", {}),
                        )
                        yield EngineResponse(
                            event_type="tool_use",
                            raw=block_data,
                            tool_use={
                                "id": block_data["id"],
                                "name": block_data["name"],
                                "input": block_data.get("input", {}),
                            },
                        )
                    elif block_data["type"] == "thinking":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="thinking",
                            sequence=block_seq,
                            thinking=block_data.get("thinking", ""),
                        )
                        yield EngineResponse(
                            event_type="thinking",
                            raw=block_data,
                            thinking=block_data.get("thinking", ""),
                        )
                    block_seq += 1

                # Update message metadata
                assistant_msg.stop_reason = msg_data.get("stop_reason")
                usage = msg_data.get("usage", {})
                assistant_msg.input_tokens = usage.get("input_tokens")
                assistant_msg.output_tokens = usage.get("output_tokens")
                await assistant_msg.asave()

                yield EngineResponse(
                    event_type="done",
                    raw={"stop_reason": msg_data.get("stop_reason")},
                )
                break

            elif event_type == "result":
                # Final result event
                yield EngineResponse(event_type="done", raw=event)
                break

    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        # Persist tool result
        seq = session.claude_messages.count()
        result_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=result_msg,
            block_type="tool_result",
            sequence=0,
            tool_result_for=tool_use_id,
            tool_result_content=str(result),
            is_error=is_error,
        )

        # For CLI, tool results flow through the same subprocess stdin
        tool_result_json = json.dumps(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": str(result),
                "is_error": is_error,
            }
        )
        self.process.stdin.write((tool_result_json + "\n").encode())
        await self.process.stdin.drain()

        # Read continuation (reuse send's read logic)
        assistant_msg = await ClaudeMessage.objects.acreate(
            session=session, role="assistant", sequence=seq + 1
        )
        block_seq = 0

        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "assistant" and "content" in event.get("message", {}):
                msg_data = event["message"]
                for block_data in msg_data.get("content", []):
                    if block_data["type"] == "text":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="text",
                            sequence=block_seq,
                            text=block_data["text"],
                        )
                        yield EngineResponse(
                            event_type="text", raw=block_data, text=block_data["text"]
                        )
                    elif block_data["type"] == "tool_use":
                        await ClaudeContentBlock.objects.acreate(
                            message=assistant_msg,
                            block_type="tool_use",
                            sequence=block_seq,
                            tool_use_id=block_data["id"],
                            tool_name=block_data["name"],
                            tool_input=block_data.get("input", {}),
                        )
                        yield EngineResponse(
                            event_type="tool_use",
                            raw=block_data,
                            tool_use={
                                "id": block_data["id"],
                                "name": block_data["name"],
                                "input": block_data.get("input", {}),
                            },
                        )
                    block_seq += 1

                assistant_msg.stop_reason = msg_data.get("stop_reason")
                usage = msg_data.get("usage", {})
                assistant_msg.input_tokens = usage.get("input_tokens")
                assistant_msg.output_tokens = usage.get("output_tokens")
                await assistant_msg.asave()

                yield EngineResponse(
                    event_type="done", raw={"stop_reason": msg_data.get("stop_reason")}
                )
                break

    def get_tools_schema(self, workflow: Workflow) -> list[dict]:
        tools_config = workflow.get_tools_config()
        if not tools_config:
            return []
        schemas = []
        for tool_name in tools_config.get("enabled_tools", []):
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config:
                schemas.append(self._adapter.to_engine_schema(tool_config))
        return schemas

    def reconstruct_messages(self, session: ConversationSession) -> list[dict]:
        # Same logic as ClaudeAPIEngine — both use Claude message format
        messages = []
        for msg in session.claude_messages.prefetch_related("content_blocks").all():
            content = []
            for block in msg.content_blocks.all():
                if block.block_type == "text":
                    content.append({"type": "text", "text": block.text})
                elif block.block_type == "thinking":
                    content.append({"type": "thinking", "thinking": block.thinking})
                elif block.block_type == "tool_use":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.tool_use_id,
                            "name": block.tool_name,
                            "input": block.tool_input or {},
                        }
                    )
                elif block.block_type == "tool_result":
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_result_for,
                            "content": block.tool_result_content or "",
                            "is_error": block.is_error,
                        }
                    )
            messages.append({"role": msg.role, "content": content})
        return messages

    async def close_session(self, session: ConversationSession) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
        self.process = None

    def get_tool_adapter(self):
        return self._adapter
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_claude_cli.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/engines/claude_cli.py tests/test_conversation_claude_cli.py
git commit -m "feat: add ClaudeCLIEngine with subprocess management and failover"
```

---

### Task 6: OpenAI API Engine

**Files:**
- Create: `src/django_ergo/conversation/engines/openai_api.py`
- Test: `tests/test_conversation_openai_api.py`

- [ ] **Step 1: Write failing tests for OpenAI API engine**

```python
# tests/test_conversation_openai_api.py
"""Tests for OpenAIAPIEngine — reconstruct_messages and tools schema."""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationSession, OpenAIMessage
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="openai",
        transport_type="api",
        status="active",
        metadata={"model": "gpt-4o-mini"},
    )


@pytest.fixture
def engine():
    return OpenAIAPIEngine(config={"model": "gpt-4o-mini", "api_key": "test-key"})


class TestOpenAIAPIEngineReconstructMessages:
    def test_reconstruct_empty(self, engine, session):
        messages = engine.reconstruct_messages(session)
        assert messages == []

    def test_reconstruct_text_conversation(self, engine, session):
        OpenAIMessage.objects.create(
            session=session, role="user", content="Hello", sequence=0
        )
        OpenAIMessage.objects.create(
            session=session, role="assistant", content="Hi there!", sequence=1
        )
        messages = engine.reconstruct_messages(session)
        assert len(messages) == 2
        assert messages[0] == {"role": "user", "content": "Hello"}
        assert messages[1] == {"role": "assistant", "content": "Hi there!"}

    def test_reconstruct_with_tool_calls(self, engine, session):
        tool_calls = [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": "test"}'},
            }
        ]
        OpenAIMessage.objects.create(
            session=session, role="user", content="Search for test", sequence=0
        )
        OpenAIMessage.objects.create(
            session=session,
            role="assistant",
            content=None,
            tool_calls=tool_calls,
            sequence=1,
        )
        OpenAIMessage.objects.create(
            session=session,
            role="tool",
            content='{"results": []}',
            tool_call_id="call_abc",
            sequence=2,
        )

        messages = engine.reconstruct_messages(session)
        assert len(messages) == 3
        assert messages[1]["tool_calls"] == tool_calls
        assert messages[2]["role"] == "tool"
        assert messages[2]["tool_call_id"] == "call_abc"

    def test_reconstruct_with_system_message(self, engine, session):
        OpenAIMessage.objects.create(
            session=session, role="system", content="You are helpful.", sequence=0
        )
        OpenAIMessage.objects.create(
            session=session, role="user", content="Hi", sequence=1
        )
        messages = engine.reconstruct_messages(session)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_openai_api.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement OpenAI API engine**

Create `src/django_ergo/conversation/engines/openai_api.py`:
```python
"""OpenAI API engine — uses the openai SDK."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.conversation.engine import Engine, EngineResponse
from django_ergo.conversation.models import ConversationSession, OpenAIMessage
from django_ergo.models import Workflow
from django_ergo.tools import tool_registry


class OpenAIAPIEngine(Engine):
    engine_type = "openai"

    def __init__(self, config: dict):
        self.model = config.get("model", "gpt-4o-mini")
        self.api_key = config.get("api_key")
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens")
        self._adapter = OpenAIToolAdapter()
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai

            self._client = openai.AsyncOpenAI(api_key=self.api_key)
        return self._client

    async def start_session(self, session: ConversationSession) -> str:
        # Add system message from workflow if present
        if session.workflow:
            await OpenAIMessage.objects.acreate(
                session=session,
                role="system",
                content=session.workflow.instructions,
                sequence=0,
            )
        return str(session.id)

    async def resume_session(self, session: ConversationSession) -> None:
        # API is stateless
        pass

    async def send(
        self, session: ConversationSession, message: str
    ) -> AsyncIterator[EngineResponse]:
        client = self._get_client()

        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session, role="user", content=message, sequence=seq
        )

        messages = self.reconstruct_messages(session)
        tools = self.get_tools_schema(session.workflow) if session.workflow else None

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        assistant_msg = await OpenAIMessage.objects.acreate(
            session=session,
            role="assistant",
            content=msg.content,
            tool_calls=[tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
            sequence=seq + 1,
            input_tokens=response.usage.prompt_tokens if response.usage else None,
            output_tokens=response.usage.completion_tokens if response.usage else None,
        )

        if msg.content:
            yield EngineResponse(event_type="text", raw={}, text=msg.content)

        if msg.tool_calls:
            for tc in msg.tool_calls:
                yield EngineResponse(
                    event_type="tool_use",
                    raw=tc.model_dump(),
                    tool_use={
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments),
                    },
                )

        yield EngineResponse(
            event_type="done",
            raw={"finish_reason": choice.finish_reason},
        )

    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        seq = await session.openai_messages.acount()
        await OpenAIMessage.objects.acreate(
            session=session,
            role="tool",
            content=str(result),
            tool_call_id=tool_use_id,
            sequence=seq,
        )

        # Rebuild and re-send
        client = self._get_client()
        messages = self.reconstruct_messages(session)
        tools = self.get_tools_schema(session.workflow) if session.workflow else None

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        await OpenAIMessage.objects.acreate(
            session=session,
            role="assistant",
            content=msg.content,
            tool_calls=[tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
            sequence=seq + 1,
            input_tokens=response.usage.prompt_tokens if response.usage else None,
            output_tokens=response.usage.completion_tokens if response.usage else None,
        )

        if msg.content:
            yield EngineResponse(event_type="text", raw={}, text=msg.content)

        if msg.tool_calls:
            for tc in msg.tool_calls:
                yield EngineResponse(
                    event_type="tool_use",
                    raw=tc.model_dump(),
                    tool_use={
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments),
                    },
                )

        yield EngineResponse(
            event_type="done", raw={"finish_reason": choice.finish_reason}
        )

    def get_tools_schema(self, workflow: Workflow) -> list[dict]:
        tools_config = workflow.get_tools_config()
        if not tools_config:
            return []
        schemas = []
        for tool_name in tools_config.get("enabled_tools", []):
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config:
                schemas.append(self._adapter.to_engine_schema(tool_config))
        return schemas

    def reconstruct_messages(self, session: ConversationSession) -> list[dict]:
        messages = []
        for msg in session.openai_messages.all():
            entry = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            messages.append(entry)
        return messages

    async def close_session(self, session: ConversationSession) -> None:
        pass

    def get_tool_adapter(self):
        return self._adapter
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_openai_api.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/engines/openai_api.py tests/test_conversation_openai_api.py
git commit -m "feat: add OpenAIAPIEngine with reconstruct_messages"
```

---

### Task 7: Session Manager

**Files:**
- Create: `src/django_ergo/conversation/manager.py`
- Test: `tests/test_conversation_manager.py`

- [ ] **Step 1: Write failing tests for session manager**

```python
# tests/test_conversation_manager.py
"""Tests for SessionManager — lifecycle, routing, failover."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.manager import SessionManager
from django_ergo.conversation.engine import TransportFailover

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def manager():
    return SessionManager()


class TestSessionManagerCreate:
    @pytest.mark.asyncio
    async def test_create_session(self, manager, user):
        with patch.object(manager, "_build_engine") as mock_build:
            mock_engine = AsyncMock()
            mock_engine.start_session = AsyncMock(return_value="session-123")
            mock_build.return_value = mock_engine

            session = await manager.create_session(
                user=user,
                workflow=None,
                engine_type="claude",
                transport_type="api",
            )

            assert session.status == "active"
            assert session.engine_type == "claude"
            assert session.transport_type == "api"
            assert session.session_id == "session-123"
            mock_engine.start_session.assert_awaited_once()


class TestSessionManagerGetEngine:
    @pytest.mark.asyncio
    async def test_get_cached_engine(self, manager, user):
        session = await ConversationSession.objects.acreate(
            user=user, engine_type="claude", transport_type="api", status="active"
        )
        mock_engine = AsyncMock()
        manager._active_engines[session.id] = mock_engine

        engine = await manager.get_engine(session)
        assert engine is mock_engine

    @pytest.mark.asyncio
    async def test_get_engine_reconnects(self, manager, user):
        session = await ConversationSession.objects.acreate(
            user=user, engine_type="claude", transport_type="api", status="active"
        )

        with patch.object(manager, "_build_engine") as mock_build:
            mock_engine = AsyncMock()
            mock_build.return_value = mock_engine

            engine = await manager.get_engine(session)

            assert engine is mock_engine
            mock_engine.resume_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_engine_failover(self, manager, user):
        session = await ConversationSession.objects.acreate(
            user=user,
            engine_type="claude",
            transport_type="cli",
            status="active",
        )

        call_count = 0

        def build_side_effect(s):
            nonlocal call_count
            call_count += 1
            mock = AsyncMock()
            if call_count == 1:
                # CLI engine fails to resume
                mock.resume_session = AsyncMock(
                    side_effect=TransportFailover("cli", "api", "CLI not available")
                )
            else:
                # API engine succeeds
                mock.resume_session = AsyncMock()
            return mock

        with patch.object(manager, "_build_engine", side_effect=build_side_effect):
            engine = await manager.get_engine(session)

            # Should have swapped to API
            await session.arefresh_from_db()
            assert session.transport_type == "api"


class TestSessionManagerClose:
    @pytest.mark.asyncio
    async def test_close_session(self, manager, user):
        session = await ConversationSession.objects.acreate(
            user=user, engine_type="claude", transport_type="api", status="active"
        )
        mock_engine = AsyncMock()
        manager._active_engines[session.id] = mock_engine

        await manager.close_session(session)

        await session.arefresh_from_db()
        assert session.status == "completed"
        assert session.id not in manager._active_engines
        mock_engine.close_session.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_manager.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement session manager**

Create `src/django_ergo/conversation/manager.py`:
```python
"""Session manager — orchestrates engine lifecycle and failover."""
from __future__ import annotations

from uuid import UUID

from django.contrib.auth import get_user_model
from django.utils.module_loading import import_string

from django_ergo.conversation.engine import Engine, TransportFailover
from django_ergo.conversation.engines import ENGINE_REGISTRY
from django_ergo.conversation.models import ConversationSession
from django_ergo.models import Workflow

User = get_user_model()


class SessionManager:
    """Manages engine instances and their lifecycle."""

    def __init__(self):
        self._active_engines: dict[UUID, Engine] = {}

    async def create_session(
        self,
        user,
        workflow: Workflow | None,
        engine_type: str,
        transport_type: str,
        metadata: dict | None = None,
    ) -> ConversationSession:
        session = await ConversationSession.objects.acreate(
            user=user,
            workflow=workflow,
            engine_type=engine_type,
            transport_type=transport_type,
            status="active",
            metadata=metadata or {},
        )
        engine = self._build_engine(session)
        session.session_id = await engine.start_session(session)
        await session.asave()
        self._active_engines[session.id] = engine
        return session

    async def get_engine(self, session: ConversationSession) -> Engine:
        if session.id in self._active_engines:
            return self._active_engines[session.id]

        engine = self._build_engine(session)
        try:
            await engine.resume_session(session)
        except TransportFailover as f:
            session.transport_type = f.fallback
            await session.asave()
            engine = self._build_engine(session)
            await engine.resume_session(session)

        self._active_engines[session.id] = engine
        return engine

    async def close_session(self, session: ConversationSession) -> None:
        if engine := self._active_engines.pop(session.id, None):
            await engine.close_session(session)
        session.status = "completed"
        await session.asave()

    def _build_engine(self, session: ConversationSession) -> Engine:
        key = (session.engine_type, session.transport_type)
        engine_path = ENGINE_REGISTRY.get(key)
        if not engine_path:
            raise ValueError(f"No engine registered for {key}")
        engine_cls = import_string(engine_path)
        return engine_cls(config=session.metadata)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_manager.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/manager.py tests/test_conversation_manager.py
git commit -m "feat: add SessionManager with lifecycle management and failover"
```

---

### Task 8: Conversation Runner (Tool Execution Loop)

**Files:**
- Create: `src/django_ergo/conversation/runner.py`
- Test: `tests/test_conversation_runner.py`

- [ ] **Step 1: Write failing tests for conversation runner**

```python
# tests/test_conversation_runner.py
"""Tests for run_conversation_turn — tool execution loop with mock engine."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from django_ergo.conversation.runner import run_conversation_turn, PendingApproval
from django_ergo.conversation.engine import EngineResponse


async def _async_gen(*items):
    """Helper to create an async generator from items."""
    for item in items:
        yield item


class TestRunConversationTurn:
    @pytest.mark.asyncio
    async def test_text_only_response(self):
        engine = MagicMock()
        engine.send = MagicMock(
            return_value=_async_gen(
                EngineResponse(event_type="text", raw={}, text="Hello!"),
                EngineResponse(event_type="done", raw={}),
            )
        )

        responses = []
        async for r in run_conversation_turn(engine, MagicMock(), "Hi"):
            responses.append(r)

        assert len(responses) == 2
        assert responses[0].event_type == "text"
        assert responses[0].text == "Hello!"

    @pytest.mark.asyncio
    async def test_tool_use_auto_approved(self):
        engine = MagicMock()
        adapter = MagicMock()
        adapter.parse_tool_call.return_value = ("search_kb", {"query": "test"})
        engine.get_tool_adapter.return_value = adapter

        engine.send = MagicMock(
            return_value=_async_gen(
                EngineResponse(
                    event_type="tool_use",
                    raw={},
                    tool_use={"id": "toolu_01", "name": "search_kb", "input": {"query": "test"}},
                ),
            )
        )
        engine.submit_tool_result = MagicMock(
            return_value=_async_gen(
                EngineResponse(event_type="text", raw={}, text="Found results"),
                EngineResponse(event_type="done", raw={}),
            )
        )

        session = MagicMock()
        session.workflow = None  # No workflow = no approval needed

        with patch("django_ergo.conversation.runner.tool_registry") as mock_registry:
            mock_registry.execute_tool.return_value = {"results": ["item1"]}

            responses = []
            async for r in run_conversation_turn(engine, session, "Search"):
                responses.append(r)

        assert any(r.text == "Found results" for r in responses)
        mock_registry.execute_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_use_requires_approval(self):
        engine = MagicMock()
        adapter = MagicMock()
        adapter.parse_tool_call.return_value = ("delete_article", {"id": "123"})
        engine.get_tool_adapter.return_value = adapter

        engine.send = MagicMock(
            return_value=_async_gen(
                EngineResponse(
                    event_type="tool_use",
                    raw={},
                    tool_use={
                        "id": "toolu_02",
                        "name": "delete_article",
                        "input": {"id": "123"},
                    },
                ),
            )
        )

        session = MagicMock()
        session.workflow = MagicMock()
        session.workflow.get_tools_config.return_value = {
            "enabled_tools": ["delete_article"],
        }
        session.user = MagicMock()

        with patch("django_ergo.conversation.runner.tool_registry") as mock_registry:
            tool_config = MagicMock()
            tool_config.requires_approval = True
            mock_registry.get_tool.return_value = tool_config

            responses = []
            async for r in run_conversation_turn(engine, session, "Delete it"):
                responses.append(r)

        assert len(responses) == 1
        assert isinstance(responses[0], PendingApproval)
        assert responses[0].tool_name == "delete_article"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_runner.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement conversation runner**

Create `src/django_ergo/conversation/runner.py`:
```python
"""Conversation turn runner — tool execution loop above the engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from django_ergo.conversation.engine import Engine, EngineResponse
from django_ergo.conversation.models import ConversationSession
from django_ergo.tools import tool_registry


@dataclass
class PendingApproval:
    """Yielded when a tool requires user approval before execution."""

    tool_use_id: str
    tool_name: str
    arguments: dict


def _tool_requires_approval(tool_name: str, workflow) -> bool:
    """Check if a tool needs user approval."""
    tool_config = tool_registry.get_tool(tool_name)
    if not tool_config or not tool_config.requires_approval:
        return False

    # Check if whitelisted in workflow
    if workflow:
        tools_config = workflow.get_tools_config()
        approved = tools_config.get("approved_tools", [])
        if tool_name in approved:
            return False

    return True


async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    """Run a single conversation turn, handling tool calls automatically."""
    adapter = engine.get_tool_adapter()

    async for response in engine.send(session, message):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)

            if _tool_requires_approval(name, session.workflow):
                yield PendingApproval(
                    tool_use_id=response.tool_use["id"],
                    tool_name=name,
                    arguments=args,
                )
                return

            result = tool_registry.execute_tool(
                name=name,
                user=session.user,
                arguments=args,
                approved=True,
            )

            async for continuation in engine.submit_tool_result(
                session, response.tool_use["id"], result
            ):
                yield continuation
        else:
            yield response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_runner.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/runner.py tests/test_conversation_runner.py
git commit -m "feat: add run_conversation_turn with tool execution and approval loop"
```

---

### Task 9: Claude CLI Conversation Importer

**Files:**
- Create: `src/django_ergo/conversation/importers/__init__.py`
- Create: `src/django_ergo/conversation/importers/claude_cli.py`
- Test: `tests/test_conversation_import.py`

- [ ] **Step 1: Write failing tests for Claude CLI importer**

```python
# tests/test_conversation_import.py
"""Tests for conversation importers."""
import pytest
import json
from django.contrib.auth import get_user_model

from django_ergo.conversation.importers import ImportService
from django_ergo.conversation.importers.claude_cli import ClaudeCLIImporter
from django_ergo.conversation.models import (
    ConversationSession,
    ClaudeMessage,
    ClaudeContentBlock,
)

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


SAMPLE_CLAUDE_CLI_SESSION = [
    {
        "type": "user",
        "message": {"role": "user", "content": "Hello, what can you do?"},
        "uuid": "msg-001",
        "timestamp": "2026-03-28T20:00:00.000Z",
        "sessionId": "session-abc",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "I can help with many things!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 20},
        },
        "uuid": "msg-002",
        "timestamp": "2026-03-28T20:00:05.000Z",
    },
    {
        "type": "user",
        "message": {"role": "user", "content": "List files in current directory"},
        "uuid": "msg-003",
        "timestamp": "2026-03-28T20:00:10.000Z",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01ABC",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 30},
        },
        "uuid": "msg-004",
        "timestamp": "2026-03-28T20:00:12.000Z",
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01ABC",
                    "content": "file1.py\nfile2.py",
                    "is_error": False,
                }
            ],
        },
        "uuid": "msg-005",
        "timestamp": "2026-03-28T20:00:13.000Z",
        "toolUseResult": "file1.py\nfile2.py",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Here are the files in the current directory."}
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 150, "output_tokens": 25},
        },
        "uuid": "msg-006",
        "timestamp": "2026-03-28T20:00:15.000Z",
    },
]


class TestClaudeCLIImporter:
    def setup_method(self):
        self.importer = ClaudeCLIImporter()

    def test_detect_format_valid(self):
        assert self.importer.detect_format(SAMPLE_CLAUDE_CLI_SESSION) is True

    def test_detect_format_invalid(self):
        assert self.importer.detect_format([{"role": "user", "content": "Hi"}]) is False
        assert self.importer.detect_format({"type": "not_a_list"}) is False
        assert self.importer.detect_format([]) is False

    @pytest.mark.asyncio
    async def test_import_conversation(self, user):
        session = await self.importer.import_conversation(
            SAMPLE_CLAUDE_CLI_SESSION, user
        )

        assert session.engine_type == "claude"
        assert session.status == "paused"
        assert session.metadata.get("imported_from") == "cli_session"

        messages = list(ClaudeMessage.objects.filter(session=session).order_by("sequence"))
        assert len(messages) == 6

        # First message: user text
        blocks_0 = list(messages[0].content_blocks.all())
        assert messages[0].role == "user"
        assert len(blocks_0) == 1
        assert blocks_0[0].block_type == "text"
        assert blocks_0[0].text == "Hello, what can you do?"

        # Second message: assistant text
        blocks_1 = list(messages[1].content_blocks.all())
        assert messages[1].role == "assistant"
        assert messages[1].stop_reason == "end_turn"
        assert messages[1].input_tokens == 50
        assert blocks_1[0].text == "I can help with many things!"

        # Fourth message: assistant tool_use
        blocks_3 = list(messages[3].content_blocks.all())
        assert blocks_3[0].block_type == "tool_use"
        assert blocks_3[0].tool_name == "Bash"
        assert blocks_3[0].tool_use_id == "toolu_01ABC"
        assert blocks_3[0].tool_input == {"command": "ls -la"}

        # Fifth message: user tool_result
        blocks_4 = list(messages[4].content_blocks.all())
        assert blocks_4[0].block_type == "tool_result"
        assert blocks_4[0].tool_result_for == "toolu_01ABC"
        assert blocks_4[0].is_error is False


class TestImportService:
    @pytest.mark.asyncio
    async def test_auto_detect_claude_cli(self, user):
        service = ImportService()
        session = await service.import_auto(SAMPLE_CLAUDE_CLI_SESSION, user)
        assert session.engine_type == "claude"

    @pytest.mark.asyncio
    async def test_auto_detect_unknown_format(self, user):
        service = ImportService()
        with pytest.raises(ValueError, match="Unrecognized"):
            await service.import_auto({"unknown": "format"}, user)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_import.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement importers**

Create `src/django_ergo/conversation/importers/__init__.py`:
```python
"""Conversation importers for external formats."""
from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model

from django_ergo.conversation.importers.claude_cli import ClaudeCLIImporter
from django_ergo.conversation.models import ConversationSession

User = get_user_model()


class ImportService:
    """Auto-detect and import conversations from external formats."""

    def __init__(self):
        self.importers = [ClaudeCLIImporter()]

    async def import_auto(
        self, data: Any, user, **kwargs
    ) -> ConversationSession:
        for importer in self.importers:
            if importer.detect_format(data):
                return await importer.import_conversation(data, user, **kwargs)
        raise ValueError("Unrecognized conversation format")
```

Create `src/django_ergo/conversation/importers/claude_cli.py`:
```python
"""Import conversations from Claude CLI session files (JSONL)."""
from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model

from django_ergo.conversation.models import (
    ClaudeContentBlock,
    ClaudeMessage,
    ConversationSession,
)

User = get_user_model()


class ClaudeCLIImporter:
    """Imports from Claude CLI JSONL session files."""

    def detect_format(self, data: Any) -> bool:
        if not isinstance(data, list) or len(data) == 0:
            return False
        return any(
            isinstance(msg, dict) and msg.get("type") in ("user", "assistant")
            for msg in data
        )

    async def import_conversation(
        self,
        data: list[dict],
        user,
        workflow=None,
    ) -> ConversationSession:
        # Extract session ID from first message if available
        session_id = ""
        for msg in data:
            if sid := msg.get("sessionId"):
                session_id = sid
                break

        session = await ConversationSession.objects.acreate(
            user=user,
            workflow=workflow,
            engine_type="claude",
            transport_type="cli",
            status="paused",
            session_id=session_id,
            metadata={"imported_from": "cli_session"},
        )

        for seq, msg_data in enumerate(data):
            msg_type = msg_data.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            message_obj = msg_data.get("message", {})
            role = "user" if msg_type == "user" else "assistant"

            # Extract usage
            usage = message_obj.get("usage", {})

            claude_msg = await ClaudeMessage.objects.acreate(
                session=session,
                role=role,
                sequence=seq,
                stop_reason=message_obj.get("stop_reason"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )

            # Parse content — can be a string or list of blocks
            content = message_obj.get("content", "")

            if isinstance(content, str):
                # Simple text message
                await ClaudeContentBlock.objects.acreate(
                    message=claude_msg,
                    block_type="text",
                    sequence=0,
                    text=content,
                )
            elif isinstance(content, list):
                for block_seq, block in enumerate(content):
                    block_type = block.get("type", "text")

                    if block_type == "text":
                        await ClaudeContentBlock.objects.acreate(
                            message=claude_msg,
                            block_type="text",
                            sequence=block_seq,
                            text=block.get("text", ""),
                        )
                    elif block_type == "thinking":
                        await ClaudeContentBlock.objects.acreate(
                            message=claude_msg,
                            block_type="thinking",
                            sequence=block_seq,
                            thinking=block.get("thinking", ""),
                        )
                    elif block_type == "tool_use":
                        await ClaudeContentBlock.objects.acreate(
                            message=claude_msg,
                            block_type="tool_use",
                            sequence=block_seq,
                            tool_use_id=block.get("id", ""),
                            tool_name=block.get("name", ""),
                            tool_input=block.get("input"),
                        )
                    elif block_type == "tool_result":
                        await ClaudeContentBlock.objects.acreate(
                            message=claude_msg,
                            block_type="tool_result",
                            sequence=block_seq,
                            tool_result_for=block.get("tool_use_id", ""),
                            tool_result_content=block.get("content", ""),
                            is_error=block.get("is_error", False),
                        )

        return session
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_import.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/importers/ tests/test_conversation_import.py
git commit -m "feat: add Claude CLI conversation importer with auto-detection"
```

---

### Task 10: Management Command and Public API

**Files:**
- Create: `src/django_ergo/management/commands/import_conversations.py`
- Modify: `src/django_ergo/conversation/__init__.py`
- Test: `tests/test_import_command.py`

- [ ] **Step 1: Write failing test for management command**

```python
# tests/test_import_command.py
"""Tests for import_conversations management command."""
import pytest
import json
import tempfile
from pathlib import Path
from django.core.management import call_command
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationSession

User = get_user_model()
pytestmark = pytest.mark.django_db


SAMPLE_SESSION = [
    {
        "type": "user",
        "message": {"role": "user", "content": "Hello"},
        "uuid": "msg-001",
        "sessionId": "session-abc",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi there!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        "uuid": "msg-002",
    },
]


class TestImportConversationsCommand:
    def test_import_jsonl_file(self):
        user = User.objects.create_user(username="testuser", password="testpass")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for msg in SAMPLE_SESSION:
                f.write(json.dumps(msg) + "\n")
            f.flush()

            call_command("import_conversations", f.name, "--user", "testuser")

        sessions = ConversationSession.objects.filter(user=user)
        assert sessions.count() == 1
        assert sessions.first().engine_type == "claude"

    def test_import_json_file(self):
        user = User.objects.create_user(username="testuser2", password="testpass")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(SAMPLE_SESSION, f)
            f.flush()

            call_command("import_conversations", f.name, "--user", "testuser2")

        sessions = ConversationSession.objects.filter(user=user)
        assert sessions.count() == 1

    def test_import_directory(self):
        user = User.objects.create_user(username="testuser3", password="testpass")

        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(2):
                path = Path(tmpdir) / f"session_{i}.jsonl"
                with open(path, "w") as f:
                    for msg in SAMPLE_SESSION:
                        f.write(json.dumps(msg) + "\n")

            call_command("import_conversations", tmpdir, "--user", "testuser3")

        sessions = ConversationSession.objects.filter(user=user)
        assert sessions.count() == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_import_command.py -v`
Expected: FAIL — command not found

- [ ] **Step 3: Implement management command**

Create `src/django_ergo/management/commands/import_conversations.py`:
```python
"""Management command to import conversations from external sources."""
import asyncio
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from django_ergo.conversation.importers import ImportService

User = get_user_model()


class Command(BaseCommand):
    help = "Import conversations from Claude CLI session files or directories"

    def add_arguments(self, parser):
        parser.add_argument(
            "source",
            help="Path to a .jsonl/.json file or directory containing session files",
        )
        parser.add_argument("--user", required=True, help="Username to assign sessions to")
        parser.add_argument(
            "--format",
            choices=["auto", "claude-cli"],
            default="auto",
            help="Source format (default: auto-detect)",
        )

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"User '{options['user']}' not found")

        source = Path(options["source"])
        if not source.exists():
            raise CommandError(f"Source '{source}' does not exist")

        asyncio.run(self._import(source, user))

    async def _import(self, source: Path, user):
        service = ImportService()
        count = 0

        if source.is_file():
            data = self._read_file(source)
            await service.import_auto(data, user)
            count = 1
        elif source.is_dir():
            for path in sorted(source.rglob("*.jsonl")) + sorted(source.rglob("*.json")):
                # Skip non-session json files
                if path.suffix == ".json" and path.name.endswith("metadata.json"):
                    continue
                try:
                    data = self._read_file(path)
                    session = await service.import_auto(data, user)
                    session.metadata["source_file"] = str(path)
                    await session.asave()
                    count += 1
                    self.stdout.write(f"  Imported: {path.name}")
                except (ValueError, json.JSONDecodeError) as e:
                    self.stderr.write(f"  Skipped {path.name}: {e}")

        self.stdout.write(self.style.SUCCESS(f"Imported {count} conversation(s)"))

    def _read_file(self, path: Path) -> list[dict]:
        text = path.read_text()
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in text.strip().splitlines() if line.strip()]
        return json.loads(text)
```

- [ ] **Step 4: Update conversation __init__.py with full public API**

Update `src/django_ergo/conversation/__init__.py`:
```python
"""
Multi-engine conversation framework for django-ergo.

Provides lossless, engine-native conversation storage and management
for Claude (CLI + API) and OpenAI engines.
"""
from django_ergo.conversation.models import (
    ConversationSession,
    ClaudeMessage,
    ClaudeContentBlock,
    OpenAIMessage,
)
from django_ergo.conversation.engine import Engine, EngineResponse, TransportFailover
from django_ergo.conversation.adapters import (
    ToolAdapter,
    ClaudeToolAdapter,
    OpenAIToolAdapter,
)
from django_ergo.conversation.manager import SessionManager
from django_ergo.conversation.runner import run_conversation_turn, PendingApproval
from django_ergo.conversation.importers import ImportService

__all__ = [
    # Models
    "ConversationSession",
    "ClaudeMessage",
    "ClaudeContentBlock",
    "OpenAIMessage",
    # Engine
    "Engine",
    "EngineResponse",
    "TransportFailover",
    # Adapters
    "ToolAdapter",
    "ClaudeToolAdapter",
    "OpenAIToolAdapter",
    # Manager
    "SessionManager",
    # Runner
    "run_conversation_turn",
    "PendingApproval",
    # Import
    "ImportService",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_import_command.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_*.py tests/test_import_command.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/django_ergo/management/commands/import_conversations.py src/django_ergo/conversation/__init__.py tests/test_import_command.py
git commit -m "feat: add import_conversations management command and public API exports"
```

---

### Task 11: Add pytest-asyncio dependency and verify full suite

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add pytest-asyncio to dev dependencies**

In `pyproject.toml`, add `pytest-asyncio` to the test dependencies group alongside `pytest` and `pytest-django`.

- [ ] **Step 2: Run the entire existing test suite to verify no regressions**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/ -v --ignore=tests/test_openai_fields.py --ignore=tests/test_openai_workflow.py`
Expected: All existing tests still pass, all new conversation tests pass

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add pytest-asyncio for async engine tests"
```

---

### Task 12: One-Shot Typed Generation (Engine.generate)

Adds `generate()` to both Claude and OpenAI engines for one-shot typed output — e.g., "given this prompt and a Pydantic model, return structured data." No session lifecycle needed.

**Files:**
- Modify: `src/django_ergo/conversation/engines/claude_api.py`
- Modify: `src/django_ergo/conversation/engines/openai_api.py`
- Test: `tests/test_conversation_generate.py`

- [ ] **Step 1: Write failing tests for generate()**

```python
# tests/test_conversation_generate.py
"""Tests for Engine.generate() — one-shot typed output."""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import BaseModel

from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.engine import EngineResponse


class MovieReview(BaseModel):
    title: str
    rating: float
    summary: str


class TestClaudeAPIGenerate:
    @pytest.mark.asyncio
    async def test_generate_text(self):
        engine = ClaudeAPIEngine(config={"api_key": "test-key"})

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello!")]
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)

            result = await engine.generate("Say hello")

        assert result.event_type == "done"
        assert result.text == "Hello!"

    @pytest.mark.asyncio
    async def test_generate_with_response_model(self):
        engine = ClaudeAPIEngine(config={"api_key": "test-key"})

        # Claude returns structured output via tool_use
        tool_input = {"title": "Inception", "rating": 9.5, "summary": "Mind-bending thriller"}
        mock_tool_block = MagicMock(
            type="tool_use", id="toolu_01", name="structured_output", input=tool_input
        )
        mock_response = MagicMock()
        mock_response.content = [mock_tool_block]
        mock_response.stop_reason = "tool_use"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=50)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)

            result = await engine.generate(
                "Review Inception", response_model=MovieReview
            )

        assert result.event_type == "done"
        parsed = result.raw["parsed"]
        assert isinstance(parsed, MovieReview)
        assert parsed.title == "Inception"
        assert parsed.rating == 9.5

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        engine = ClaudeAPIEngine(config={"api_key": "test-key"})

        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="I am a pirate assistant!")]
        mock_response.stop_reason = "end_turn"
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.messages.create = AsyncMock(return_value=mock_response)

            result = await engine.generate("Who are you?", system="You are a pirate.")

        # Verify system was passed
        call_kwargs = mock_client.return_value.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "You are a pirate."


class TestOpenAIAPIGenerate:
    @pytest.mark.asyncio
    async def test_generate_text(self):
        engine = OpenAIAPIEngine(config={"api_key": "test-key"})

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello!"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(
                return_value=mock_response
            )

            result = await engine.generate("Say hello")

        assert result.event_type == "done"
        assert result.text == "Hello!"

    @pytest.mark.asyncio
    async def test_generate_with_response_model(self):
        engine = OpenAIAPIEngine(config={"api_key": "test-key"})

        # OpenAI returns structured output via function call
        tool_call = MagicMock()
        tool_call.id = "call_abc"
        tool_call.function.name = "structured_output"
        tool_call.function.arguments = json.dumps(
            {"title": "Inception", "rating": 9.5, "summary": "Mind-bending thriller"}
        )

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [tool_call]
        mock_choice.finish_reason = "tool_calls"
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=50)

        with patch.object(engine, "_get_client") as mock_client:
            mock_client.return_value.chat.completions.create = AsyncMock(
                return_value=mock_response
            )

            result = await engine.generate(
                "Review Inception", response_model=MovieReview
            )

        parsed = result.raw["parsed"]
        assert isinstance(parsed, MovieReview)
        assert parsed.title == "Inception"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_generate.py -v`
Expected: FAIL — `generate()` raises `NotImplementedError`

- [ ] **Step 3: Implement generate() on ClaudeAPIEngine**

Add to `src/django_ergo/conversation/engines/claude_api.py`:
```python
    async def generate(
        self,
        prompt: str,
        workflow: Workflow | None = None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        client = self._get_client()

        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        # System prompt — from arg or workflow
        sys_prompt = system or (workflow.instructions if workflow else None)
        if sys_prompt:
            kwargs["system"] = sys_prompt

        # Structured output via tool_use forcing
        if response_model is not None:
            schema = response_model.model_json_schema()
            kwargs["tools"] = [
                {
                    "name": "structured_output",
                    "description": f"Return a {response_model.__name__} object",
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "structured_output"}

        # Add workflow tools if present and no response_model
        elif workflow:
            tools = self.get_tools_schema(workflow)
            if tools:
                kwargs["tools"] = tools

        response = await client.messages.create(**kwargs)

        # Parse response
        if response_model is not None:
            # Find tool_use block and parse
            for block in response.content:
                if block.type == "tool_use" and block.name == "structured_output":
                    parsed = response_model.model_validate(block.input)
                    return EngineResponse(
                        event_type="done",
                        raw={
                            "parsed": parsed,
                            "usage": {
                                "input_tokens": response.usage.input_tokens,
                                "output_tokens": response.usage.output_tokens,
                            },
                        },
                        text=None,
                    )

        # Plain text response
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        return EngineResponse(
            event_type="done",
            raw={
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            },
            text=text,
        )
```

- [ ] **Step 4: Implement generate() on OpenAIAPIEngine**

Add to `src/django_ergo/conversation/engines/openai_api.py`:
```python
    async def generate(
        self,
        prompt: str,
        workflow: Workflow | None = None,
        system: str | None = None,
        response_model: type | None = None,
    ) -> EngineResponse:
        client = self._get_client()

        messages = []
        sys_prompt = system or (workflow.instructions if workflow else None)
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.append({"role": "user", "content": prompt})

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        # Structured output via function calling
        if response_model is not None:
            schema = response_model.model_json_schema()
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "structured_output",
                        "description": f"Return a {response_model.__name__} object",
                        "parameters": schema,
                    },
                }
            ]
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": "structured_output"},
            }
        elif workflow:
            tools = self.get_tools_schema(workflow)
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        if response_model is not None and msg.tool_calls:
            tc = msg.tool_calls[0]
            raw_args = json.loads(tc.function.arguments)
            parsed = response_model.model_validate(raw_args)
            return EngineResponse(
                event_type="done",
                raw={
                    "parsed": parsed,
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                    },
                },
                text=None,
            )

        return EngineResponse(
            event_type="done",
            raw={
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                },
            },
            text=msg.content,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/linked/p/boundcorp/django-ergo && python -m pytest tests/test_conversation_generate.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/conversation/engines/claude_api.py src/django_ergo/conversation/engines/openai_api.py tests/test_conversation_generate.py
git commit -m "feat: add Engine.generate() for one-shot typed outputs via tool_use forcing"
```
