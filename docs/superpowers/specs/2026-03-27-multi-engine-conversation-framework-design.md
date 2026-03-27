# Multi-Engine Conversation Framework Design

**Date:** 2026-03-27
**Status:** Draft
**Author:** Lee / Claude

## Overview

Extend django-ergo from a one-off workflow/generation toolkit into a full conversation platform supporting multiple LLM engines (Claude, OpenAI), multiple transports (CLI subscription, API key), lossless conversation storage, conversation import, and session resume.

## Goals

1. **Lossless engine-native storage** — faithfully represent Claude and OpenAI conversation structures without flattening to a lowest-common-denominator schema
2. **Drive Claude CLI from Django** — use subscription billing via long-running CLI sessions with stream-json output
3. **Multi-engine, multi-transport** — unified engine protocol with pluggable transports (CLI, API) per engine
4. **Conversation import** — bulk ingest Claude CLI history (~/.claude/) and OpenAI exports
5. **Session resume** — any stored conversation can be continued from DB state alone, on any transport
6. **Full platform** — interactive real-time chat, background automation, and conversation archive/search

## Non-Goals (v1)

- Codex/OpenAI CLI transport
- Daemon pool for CLI processes (start with spawn/resume lifecycle)
- Refactoring existing OpenAI workflow_engine.py (build alongside, migrate later)

## Architecture

Five layers, each with a clear responsibility:

```
┌─────────────────────────────────────────────────┐
│  Views / Consumers / Management Commands        │
├─────────────────────────────────────────────────┤
│  Session Manager (lifecycle, failover, routing) │
├─────────────────────────────────────────────────┤
│  Engine Protocol (send, stream, resume)         │
│  ┌──────────────┐  ┌────────────────────────┐   │
│  │ Tool Adapter  │  │ Conversation Importer  │   │
│  └──────────────┘  └────────────────────────┘   │
├─────────────────────────────────────────────────┤
│  Storage (engine-native Django models)          │
└─────────────────────────────────────────────────┘
```

---

## Layer 1: Storage

### ConversationSession (shared base)

Engine-agnostic session container. All engine-specific messages FK to this.

```python
class ConversationSession(TimeStampedMixin):
    id = UUIDField(primary_key=True)
    user = ForeignKey(User)
    workflow = ForeignKey(Workflow, null=True, blank=True)
    engine_type = CharField(max_length=20, choices=[("claude", "Claude"), ("openai", "OpenAI")])
    transport_type = CharField(max_length=20, choices=[("cli", "CLI"), ("api", "API")])
    session_id = CharField(max_length=255, blank=True)  # engine-native session ID
    status = CharField(max_length=20, choices=[
        ("active", "Active"),
        ("paused", "Paused"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ])
    metadata = JSONField(default=dict)  # engine config, model name, import source, etc.
```

### Claude-native messages

Two models to faithfully represent Claude's content-block-array message structure.

```python
class ClaudeMessage(TimeStampedMixin):
    id = UUIDField(primary_key=True)
    session = ForeignKey(ConversationSession, related_name="claude_messages")
    role = CharField(max_length=20, choices=[("user", "User"), ("assistant", "Assistant")])
    stop_reason = CharField(max_length=30, null=True, blank=True)  # end_turn, tool_use, max_tokens
    input_tokens = IntegerField(null=True, blank=True)
    output_tokens = IntegerField(null=True, blank=True)
    sequence = IntegerField()  # ordering within session

    class Meta:
        ordering = ["sequence"]


class ClaudeContentBlock(TimeStampedMixin):
    message = ForeignKey(ClaudeMessage, related_name="content_blocks")
    block_type = CharField(max_length=20, choices=[
        ("text", "Text"),
        ("tool_use", "Tool Use"),
        ("tool_result", "Tool Result"),
        ("thinking", "Thinking"),
    ])
    sequence = IntegerField()  # ordering within message

    # Text blocks
    text = TextField(null=True, blank=True)

    # Thinking blocks
    thinking = TextField(null=True, blank=True)

    # Tool use blocks
    tool_use_id = CharField(max_length=255, null=True, blank=True)
    tool_name = CharField(max_length=255, null=True, blank=True)
    tool_input = JSONField(null=True, blank=True)

    # Tool result blocks
    tool_result_for = CharField(max_length=255, null=True, blank=True)  # references tool_use_id
    tool_result_content = JSONField(null=True, blank=True)
    is_error = BooleanField(default=False)

    class Meta:
        ordering = ["sequence"]
```

### OpenAI-native messages

Flat structure matching OpenAI's single-content + tool_calls format.

```python
class OpenAIMessage(TimeStampedMixin):
    id = UUIDField(primary_key=True)
    session = ForeignKey(ConversationSession, related_name="openai_messages")
    role = CharField(max_length=20, choices=[
        ("user", "User"), ("assistant", "Assistant"),
        ("system", "System"), ("tool", "Tool"),
    ])
    content = TextField(null=True, blank=True)
    tool_calls = JSONField(null=True, blank=True)  # OpenAI's native tool_calls array
    tool_call_id = CharField(max_length=255, null=True, blank=True)  # for tool response messages
    function_name = CharField(max_length=255, null=True, blank=True)
    input_tokens = IntegerField(null=True, blank=True)
    output_tokens = IntegerField(null=True, blank=True)
    sequence = IntegerField()

    class Meta:
        ordering = ["sequence"]
```

### Design decisions

- `ClaudeContentBlock` is a separate model (not JSON) for queryability — filter by block type, search tool names, find thinking blocks across sessions
- OpenAI messages stay flat because that matches OpenAI's structure
- Both link to shared `ConversationSession` for cross-engine queries (all sessions for a user, all sessions using a workflow)
- Existing `UserChat`/`ChatMessage` models remain untouched — they're the v1 one-off system

---

## Layer 2: Engine Protocol

Abstract async interface that all engines implement.

```python
@dataclass
class EngineResponse:
    """Yielded from stream() — wraps engine-native events into a common envelope."""
    event_type: str  # "text", "tool_use", "thinking", "done", "error"
    raw: dict        # engine-native payload, stored as-is
    text: str | None = None
    tool_use: dict | None = None  # {"id": ..., "name": ..., "input": ...}
    thinking: str | None = None


class Engine(ABC):
    engine_type: str  # "claude" or "openai"

    @abstractmethod
    async def start_session(self, session: ConversationSession) -> str:
        """Start a new session. Returns engine-native session ID."""

    @abstractmethod
    async def resume_session(self, session: ConversationSession) -> None:
        """Resume an existing session from DB state."""

    @abstractmethod
    async def send(self, session: ConversationSession, message: str) -> AsyncIterator[EngineResponse]:
        """Send a message, yield streaming responses. Persists messages to DB."""

    @abstractmethod
    async def submit_tool_result(
        self, session: ConversationSession, tool_use_id: str, result: Any, is_error: bool = False
    ) -> AsyncIterator[EngineResponse]:
        """Submit a tool result and yield the assistant's continuation."""

    @abstractmethod
    def get_tools_schema(self, workflow: Workflow) -> list[dict]:
        """Convert ergo tools into engine-native tool format."""

    @abstractmethod
    def reconstruct_messages(self, session: ConversationSession) -> list[dict]:
        """Build engine-native message history from stored models.
        Enables resume-from-DB, transport failover, and conversation export."""

    @abstractmethod
    async def close_session(self, session: ConversationSession) -> None:
        """Clean up resources (kill subprocess, close connection)."""
```

### Engine implementations

**ClaudeCLIEngine:**
- Manages a `claude -p --output-format stream-json` subprocess per session
- Reads NDJSON from stdout, writes to stdin
- Persists `ClaudeMessage` + `ClaudeContentBlock` rows as streaming events arrive
- Resume via `claude --resume {session_id}`, with failover to API if CLI session unavailable

**ClaudeAPIEngine:**
- Uses `anthropic` Python SDK with streaming
- Same storage models as CLI engine (`ClaudeMessage` + `ClaudeContentBlock`)
- Stateless — sends full message history (via `reconstruct_messages()`) on each turn
- Used for: direct API access, failover from CLI, resuming imported conversations

**OpenAIAPIEngine:**
- Refactored from existing `workflow_engine.py` logic
- Uses `openai` Python SDK
- Persists to `OpenAIMessage`

### Session resume flow

Any session can be resumed from DB state alone:

1. `Engine.reconstruct_messages(session)` builds full message history from stored models
2. For API transport: send reconstructed history as context with the next message
3. For CLI transport: try `claude --resume {session_id}` first, failover to API if unavailable
4. Transport type on session updates to reflect what's actually being used

---

## Layer 3: Tool Adapter

Translates ergo's `@tool()` definitions between engine-native formats. Does not modify the existing `ToolRegistry`.

```python
class ToolAdapter(ABC):
    @abstractmethod
    def to_engine_schema(self, tool_config: ToolConfig) -> dict:
        """Convert an ergo tool definition to engine-native format."""

    @abstractmethod
    def parse_tool_call(self, raw: dict) -> tuple[str, dict]:
        """Extract (tool_name, arguments) from engine-native tool call."""

    @abstractmethod
    def format_tool_result(self, tool_use_id: str, result: Any, is_error: bool) -> dict:
        """Format tool result for engine-native submission."""
```

**ClaudeToolAdapter** converts to/from Claude's `{"name", "description", "input_schema"}` format.

**OpenAIToolAdapter** converts to/from OpenAI's `{"type": "function", "function": {...}}` format.

### Tool execution loop

The tool execution loop sits above the engine, not inside it:

```python
async def run_conversation_turn(engine, session, message):
    adapter = engine.get_tool_adapter()
    async for response in engine.send(session, message):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)
            if tool_requires_approval(name, session.workflow):
                yield PendingApproval(...)
                return  # pause until user approves
            result = tool_registry.execute_tool(name, session.user, args)
            async for continuation in engine.submit_tool_result(session, response.tool_use["id"], result):
                yield continuation
        else:
            yield response
```

This keeps tools decoupled from engines. The existing `@tool()` decorator and `ToolRegistry` are untouched.

---

## Layer 4: Session Manager

Orchestrates engine lifecycle, handles failover.

```python
class SessionManager:
    _active_engines: dict[UUID, Engine] = {}

    async def create_session(self, user, workflow, engine_type, transport_type) -> ConversationSession:
        """Create DB session, spin up engine, return ready-to-use session."""

    async def get_engine(self, session) -> Engine:
        """Get or reconnect engine for an existing session.
        Handles failover: if CLI resume fails, transparently switches to API."""

    async def close_session(self, session) -> None:
        """Clean shutdown — kill subprocess, update DB status."""
```

### Key behaviors

- `_active_engines` is process-local. If Django restarts, engines reconnect lazily on next `get_engine()` call.
- **Automatic failover**: CLI subprocess dies → catch `TransportFailover` → swap to API transport → update session's `transport_type` → continue seamlessly.
- Views/consumers only interact with `SessionManager`, never instantiate engines directly.
- Works the same from sync views, async consumers, or Celery tasks.

---

## Layer 5: Conversation Import

Ingest conversations from external sources into ergo's native models.

```python
class ConversationImporter(ABC):
    def detect_format(self, data) -> bool:
        """Can this importer handle this data?"""

    async def import_conversation(self, data, user, workflow=None) -> ConversationSession:
        """Parse external format, create session + native messages."""


class ImportService:
    importers = [ClaudeCLIImporter(), ClaudeAPIImporter(), OpenAIImporter()]

    async def import_auto(self, data, user, **kwargs) -> ConversationSession:
        """Auto-detect format and import."""

    async def import_claude_cli_sessions(self, directory: Path, user: User):
        """Bulk import from a Claude CLI projects directory."""
```

### Import details

- **ClaudeCLIImporter**: Reads JSONL session files from `~/.claude/projects/*/sessions/`. Maps `human`/`assistant` message types, creates `ClaudeMessage` + `ClaudeContentBlock` for each content block.
- **OpenAIImporter**: Reads OpenAI chat completion format. Creates `OpenAIMessage` rows.
- Imported sessions have `status="paused"` — ready to resume on any transport.
- Import source stored in `metadata` (e.g., `{"imported_from": "cli_session", "source_file": "..."}`).
- Management command: `manage.py import_conversations <source> --user <username> --format auto`

### Resuming imported conversations

Imported sessions are first-class. To continue an imported Claude CLI conversation:

1. User opens the session in the UI
2. `SessionManager.get_engine(session)` builds a `ClaudeCLIEngine` (or `ClaudeAPIEngine`)
3. Engine calls `reconstruct_messages()` to build full context from stored `ClaudeMessage`/`ClaudeContentBlock` rows
4. New CLI subprocess (or API call) starts with that context
5. Session status flips to `"active"`, conversation continues

---

## Relationship to Existing Models

The existing `UserChat`, `ChatMessage`, and `Workflow` models remain untouched:

- `Workflow` is reused as a configuration template (system prompt, tool config) for the new `ConversationSession`
- `UserChat`/`ChatMessage` continue to work for the v1 one-off workflow system
- Migration path: eventually `UserChat` could be replaced by `ConversationSession` + `OpenAIMessage`, but this is not required for v1

---

## Engine/Transport Matrix

| Engine | Transport | Billing | Implementation |
|--------|-----------|---------|----------------|
| Claude | CLI | Subscription | `ClaudeCLIEngine` — subprocess with stream-json |
| Claude | API | Per-token | `ClaudeAPIEngine` — anthropic SDK |
| OpenAI | API | Per-token | `OpenAIAPIEngine` — openai SDK |
| OpenAI | CLI | (future) | Not in v1 |

---

## File Organization

```
src/django_ergo/
├── models.py                    # Existing models (untouched)
├── conversation/
│   ├── __init__.py
│   ├── models.py                # ConversationSession, ClaudeMessage, ClaudeContentBlock, OpenAIMessage
│   ├── engine.py                # Engine ABC, EngineResponse
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── claude_cli.py        # ClaudeCLIEngine
│   │   ├── claude_api.py        # ClaudeAPIEngine
│   │   └── openai_api.py        # OpenAIAPIEngine
│   ├── adapters.py              # ToolAdapter, ClaudeToolAdapter, OpenAIToolAdapter
│   ├── manager.py               # SessionManager
│   ├── runner.py                # run_conversation_turn() — tool execution loop
│   └── importers/
│       ├── __init__.py          # ImportService
│       ├── claude_cli.py        # ClaudeCLIImporter
│       ├── claude_api.py        # ClaudeAPIImporter
│       └── openai.py            # OpenAIImporter
├── management/commands/
│   ├── import_conversations.py  # Bulk import command
│   └── ... (existing commands)
└── ... (existing files)
```
