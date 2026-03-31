# Conversation Renderer and History Toolkit Design

**Date:** 2026-03-30
**Status:** Draft
**Author:** Lee / Claude

## Overview

Replace the naive full-transcript `_format_conversation_as_text` with a tiered rendering system that controls token cost, plus a scoped toolkit that lets agents efficiently browse past conversations with tool-based drill-down.

## Problem

The current `run_conversation_pipeline` dumps entire conversations as flat text. Five 100k-token conversations = 500k tokens in context. Most of that is tool call internals and thinking blocks that the consuming LLM doesn't need upfront.

## Goals

1. **Token-efficient conversation rendering** — tiered detail levels so skeleton views are ~10-15% of full cost
2. **Agent drill-down tools** — scoped tools to retrieve specific tool calls, message ranges, and thinking blocks on demand
3. **Pluggable rendering strategies** — built-in (headline, skeleton, full) plus custom callables (e.g., LLM-summarized)
4. **Clean integration** — works with existing pipelines, engines, and runner without breaking changes

---

## Component 1: ConversationRenderer

A class with configurable detail levels that renders conversations for LLM consumption.

### Detail Levels

| Level | Name | What's included | Token ratio |
|-------|------|----------------|-------------|
| 0 | `headline` | Session slug + last prompt from metadata (if available from CLI import), else first user message truncated to ~100 chars | ~1% |
| 1 | `skeleton` | User messages + assistant text. Tool calls as `[tool_call #N: Name(key_args)]`. Tool results as `[tool_result #N: (X lines)]`. Thinking omitted. All messages numbered. | ~10-15% |
| 2 | `full` | Everything verbatim — text, tool_use with inputs, tool_result with outputs, thinking blocks | 100% |

### Skeleton Format Example

```
[msg #0 USER]: What files are in /tmp?
[msg #1 ASSISTANT]: [tool_call #1: Bash(command="ls /tmp")]
[msg #2 TOOL_RESULT #1]: (2 lines)
[msg #3 ASSISTANT]: There are 2 files in /tmp: file1.txt and file2.txt.
```

Messages get sequential `msg #N` numbers. Tool calls get their own sequential `#N` counter. Both are referenced by the drill-down tools.

### API

```python
class ConversationRenderer:
    def __init__(self, detail: str = "skeleton", custom_fn=None):
        self.detail = detail  # "headline", "skeleton", "full", "custom"
        self.custom_fn = custom_fn  # async callable(session) -> str

    def render(self, session) -> str:
        """Render a conversation from DB models at the configured detail level."""

    def render_messages(self, messages: list[dict]) -> str:
        """Render from already-reconstructed message dicts (engine-agnostic)."""

    async def render_async(self, session, **kwargs) -> str:
        """Async render — required for custom strategies that call LLMs."""
```

The renderer works on either raw model querysets (for DB-backed sessions) or reconstructed message dicts (for engine-agnostic use). Both paths produce the same output.

### Custom Strategies

Pass an async callable for LLM-powered rendering:

```python
async def haiku_summary(session, engine):
    full = ConversationRenderer(detail="full").render(session)
    response = await engine.generate(
        prompt=full,
        system="Summarize this conversation in 2-3 sentences.",
    )
    return response.text

renderer = ConversationRenderer(detail="custom", custom_fn=haiku_summary)
text = await renderer.render_async(session, engine=engine)
```

### Caching

Custom strategies (especially LLM-based) can be expensive. The renderer supports opt-in caching via `session.metadata`:

- `renderer.render_and_cache(session)` stores the result in `session.metadata["rendered_cache"][strategy_name]`
- Subsequent `render()` calls check the cache first
- Cache is invalidated when `session.updated_at` changes

---

## Component 2: ChatWithHistoryToolkit

A scoped toolkit instantiated with specific conversations. Provides tools bound to those conversations for agent drill-down.

### Instantiation

```python
toolkit = ChatWithHistoryToolkit(
    sessions=[session_a, session_b, session_c],
    renderer=ConversationRenderer(detail="skeleton"),  # optional, defaults to skeleton
)
```

### Initial Context

`toolkit.render_overview()` returns a skeleton rendering of all sessions, suitable for the initial message to the agent:

```
=== Conversation 1 (session_id: abc-123) ===
[msg #0 USER]: What files are in /tmp?
[msg #1 ASSISTANT]: [tool_call #1: Bash(command="ls /tmp")]
[msg #2 TOOL_RESULT #1]: (2 lines)
[msg #3 ASSISTANT]: There are 2 files in /tmp.

=== Conversation 2 (session_id: def-456) ===
[msg #0 USER]: Explain the auth system
[msg #1 ASSISTANT]: The auth system uses JWT tokens...
```

### Scoped Tools

| Tool | Args | Returns |
|------|------|---------|
| `view_conversation` | `session_id`, `detail` (optional, default skeleton) | Rendered conversation at requested detail level |
| `get_tool_call` | `session_id`, `tool_call_number` | Full tool_use input + tool_result output for that call |
| `get_message_range` | `session_id`, `start`, `end` | Full-detail rendering of messages #start through #end |
| `get_thinking` | `session_id`, `message_number` | Thinking block content for that assistant message |

All tools are read-only and never require approval. They execute locally via DB queries + rendering — no external API calls.

### Tool Execution

The toolkit owns execution of its tools:

```python
class ChatWithHistoryToolkit:
    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        """Return tool schemas in engine-native format."""

    def has_tool(self, tool_name: str) -> bool:
        """Check if this toolkit handles a given tool name."""

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a toolkit tool and return the result string."""
```

Tools are prefixed internally (e.g., `history_view_conversation`) to avoid collisions with workflow tools.

---

## Component 3: Runner Integration

The `run_conversation_turn` function gets a new optional `extra_tools` parameter.

### Changes to runner

```python
async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: ChatWithHistoryToolkit | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
```

When `extra_tools` is provided:
1. The engine's `get_tools_schema()` output is merged with `extra_tools.get_tools_schema(adapter)`
2. On `tool_use` events, the runner checks `extra_tools.has_tool(name)` first
3. If the toolkit handles it, call `extra_tools.execute_tool(name, args)` directly (no approval needed)
4. Otherwise fall through to the global `tool_registry` with normal approval logic

### Usage Pattern

```python
toolkit = ChatWithHistoryToolkit(sessions=[session_a, session_b])
analysis_session = await session_manager.create_session(
    user=user, workflow=analysis_workflow,
    engine_type="claude", transport_type="api",
)

initial_message = (
    "Analyze these conversations:\n\n" + toolkit.render_overview()
)

async for response in run_conversation_turn(
    engine, analysis_session, initial_message, extra_tools=toolkit,
):
    yield response
```

---

## Component 4: Pipeline Integration

The existing pipeline functions use the renderer instead of raw `_format_conversation_as_text`.

### Changes

- `run_conversation_pipeline` gets an optional `renderer` parameter (defaults to `ConversationRenderer(detail="skeleton")`)
- `summarize_conversation` and `compact_conversation` use skeleton by default
- `_format_conversation_as_text` is deprecated in favor of `ConversationRenderer(detail="full").render_messages()`

---

## File Organization

```
src/django_ergo/conversation/
├── renderer.py          # ConversationRenderer + detail strategies
├── history_toolkit.py   # ChatWithHistoryToolkit + scoped tools
├── pipelines.py         # Updated to use renderer
├── runner.py            # Updated with extra_tools parameter
```

---

## CLI Import Metadata Extraction

The Claude CLI importer should extract additional metadata from session files:

- **`slug`** — human-readable session identifier (e.g., `"sorted-brewing-mitten"`), present on most messages. Stored in `session.metadata["slug"]`.
- **`last-prompt`** — the last user message text, stored as a separate JSONL event with `type: "last-prompt"`. Stored in `session.metadata["last_prompt"]`.
- **`turn_duration`** events — `system` type messages with `subtype: "turn_duration"` containing `durationMs` and `messageCount`. Could be aggregated into `session.metadata["total_duration_ms"]`.

The `headline` renderer uses these when available:
1. If `session.metadata["last_prompt"]` exists → use it (truncated)
2. Else if `session.metadata["slug"]` exists → use it as a fallback label
3. Else → first user message text truncated to ~100 chars

Note: Claude CLI does NOT store pre-written summaries. The session picker in the CLI likely generates descriptions on the fly. If a summary is needed, use the `custom` renderer strategy with an LLM call and cache the result.

---

## Relationship to Existing Code

- `_format_conversation_as_text` in `pipelines.py` → replaced by `ConversationRenderer(detail="full")`
- `run_conversation_turn` in `runner.py` → extended with `extra_tools` param, backward compatible
- `ToolAdapter` in `adapters.py` → used by toolkit to generate engine-native schemas
- `ConversationSession` models → unchanged, renderer queries them directly
