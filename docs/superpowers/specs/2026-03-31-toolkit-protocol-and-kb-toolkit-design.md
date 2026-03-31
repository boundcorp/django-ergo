# Toolkit Protocol and KBToolkit Design

**Date:** 2026-03-31
**Status:** Draft
**Author:** Lee / Claude

## Overview

Extract a `Toolkit` protocol from the existing `ChatWithHistoryToolkit` pattern, then build `KBToolkit` as a second implementation that gives agents scoped, read-only access to knowledgebases. Update the conversation runner to accept a list of toolkits.

## Problem

The conversation framework has a working toolkit pattern (`ChatWithHistoryToolkit`) but it's a one-off. There's no formal protocol, no way to compose multiple toolkits, and no way for agents to search or browse knowledgebases through the conversation framework. The existing `kb_tools.py` registers tools globally and scopes by user at runtime — fine for the old Chat/Workflow system, but doesn't fit the scoped-toolkit model.

## Goals

1. **Toolkit protocol** — formal ABC that all toolkits implement, enabling composition and type safety
2. **KBToolkit** — scoped read-only tools for agent-driven KB search and browsing
3. **Runner composition** — `extra_tools` accepts a list of toolkits, merging schemas and dispatching calls
4. **Backward compatible** — existing `ChatWithHistoryToolkit` usage continues to work

## Non-Goals

- KB write tools (create/update/delete articles) — next sub-project
- KB population from source materials
- KB visualization or optimization tooling
- Changes to the existing global `kb_tools.py` or `tool_registry`

---

## Component 1: Toolkit Protocol

An abstract base class defining the contract all toolkits implement.

### Interface

```python
class Toolkit(ABC):
    @abstractmethod
    def has_tool(self, tool_name: str) -> bool:
        """Check if this toolkit handles a given tool name."""

    @abstractmethod
    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a toolkit tool and return the result string."""

    @abstractmethod
    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        """Return tool schemas in engine-native format."""

    @abstractmethod
    def render_overview(self) -> str:
        """Render initial context for the agent (e.g., TOC, summaries)."""
```

### File Location

`src/django_ergo/conversation/toolkit.py` — lives in the conversation module since it's part of the conversation framework's tool dispatch system.

### Refactoring ChatWithHistoryToolkit

`ChatWithHistoryToolkit` already implements all four methods. Change it to inherit from `Toolkit`:

```python
from django_ergo.conversation.toolkit import Toolkit

class ChatWithHistoryToolkit(Toolkit):
    # ... existing code unchanged
```

No behavioral changes. Just adds the ABC parent.

---

## Component 2: KBToolkit

A toolkit scoped to specific knowledgebases, providing read-only search and browse tools.

### Construction

```python
toolkit = KBToolkit(knowledgebases=[docs_kb, faq_kb])
```

Stores KBs as `{str(kb.id): kb}` dict, same pattern as `ChatWithHistoryToolkit` with sessions.

### Tools

| Tool | Args | Returns |
|------|------|---------|
| `kb_list` | (none) | Names, descriptions, article counts for all bound KBs |
| `kb_search` | `query` (required), `kb_name` (optional), `top_k` (optional, default 5) | Top-k articles with titles, hierarchy codes, content preview (200 chars), cosine distances |
| `kb_get_article` | `kb_name` (required), `hierarchy_code` (required) | Full article title, content, and summary |
| `kb_table_of_contents` | `kb_name` (required) | All articles with hierarchy codes and titles, indented by depth |

All tools are read-only and never require approval. Tool names are prefixed with `kb_` to avoid collisions with other toolkits.

### Tool Behavior Details

**`kb_list`**: Returns a formatted listing of all bound KBs.
```
1. Product Docs (kb_id: abc-123) — 47 articles
   Complete product documentation for FooBar v3.2

2. FAQ (kb_id: def-456) — 12 articles
   Frequently asked questions
```

**`kb_search`**: Uses `ArticleQuerySet.multi_field_semantic_search()` for weighted content+summary search. Filters to the bound KBs (or a specific KB if `kb_name` is provided). Returns results formatted as:
```
Result 1 (distance: 0.234):
  KB: Product Docs
  Article: 2A — Authentication Setup
  Preview: To set up authentication, first configure your OAuth provider...

Result 2 (distance: 0.412):
  ...
```

If `kb_name` is provided but doesn't match any bound KB, raises ValueError. If the embedding provider fails (e.g., no API key configured), returns an error message string rather than raising — the agent can still use non-search tools.

**`kb_get_article`**: Looks up a specific article by KB name and hierarchy code. Returns:
```
Article: 2A — Authentication Setup
KB: Product Docs

Content:
[full article content]

Summary:
[article summary, if available]
```

Raises ValueError if KB name not found or article doesn't exist.

**`kb_table_of_contents`**: Returns all articles for a KB, indented by hierarchy depth:
```
0: Introduction
1: Getting Started
  10: Installation
  11: Configuration
  12: First Steps
2: API Reference
  20: Authentication
    200: OAuth
    201: API Keys
  21: Endpoints
3: Troubleshooting
```

Depth is inferred from hierarchy code length (1 char = top level, 2 chars = second level, etc.).

### render_overview()

Returns KB info + top-level articles for each bound KB:
```
=== Knowledge Base: Product Docs (kb_id: abc-123) ===
Description: Complete product documentation for FooBar v3.2
Articles: 47

Top-level sections:
  0: Introduction
  1: Getting Started
  2: API Reference
  3: Troubleshooting
  4: FAQ

=== Knowledge Base: FAQ (kb_id: def-456) ===
Description: Frequently asked questions
Articles: 12

Top-level sections:
  0: General
  1: Billing
  2: Technical
```

### File Location

`src/django_ergo/kb_toolkit.py` — lives at the top level next to `kb_tools.py`, since it's a KB concern, not a conversation concern.

---

## Component 3: Runner Integration

### extra_tools becomes list[Toolkit]

Update `run_conversation_turn()`:

```python
async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: list[Toolkit] | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
```

### Tool dispatch

On `tool_use` events, the runner iterates toolkits in order:

```python
for toolkit in extra_tools:
    if toolkit.has_tool(name):
        result = toolkit.execute_tool(name, args)
        # submit result to engine
        break
else:
    # fall through to global tool_registry + approval logic
```

First toolkit to claim the tool handles it. No approval required for toolkit tools.

### Schema merging

Tool schemas from all toolkits are merged and passed to the engine alongside any workflow tools. This requires extending `engine.send()` with an optional `additional_tools` parameter:

```python
# Engine ABC
async def send(
    self,
    session: ConversationSession,
    message: str,
    additional_tools: list[dict] | None = None,
) -> AsyncIterator[EngineResponse]:
```

Each engine implementation appends `additional_tools` to its own tool list for that API call. This is backward-compatible (defaults to None).

The runner collects all toolkit schemas:

```python
adapter = engine.get_tool_adapter()
additional_tools = []
for toolkit in extra_tools:
    additional_tools.extend(toolkit.get_tools_schema(adapter))
async for response in engine.send(session, message, additional_tools=additional_tools):
    ...
```

---

## File Organization

```
src/django_ergo/
├── conversation/
│   ├── toolkit.py              # Toolkit ABC
│   ├── history_toolkit.py      # ChatWithHistoryToolkit (updated: inherits Toolkit)
│   ├── runner.py               # Updated: extra_tools is list[Toolkit]
│   ├── engine.py               # Updated: send() gets additional_tools param
│   ├── engines/
│   │   ├── claude_api.py       # Updated: pass additional_tools in API call
│   │   ├── claude_cli.py       # Updated: pass additional_tools in CLI call
│   │   └── openai_api.py       # Updated: pass additional_tools in API call
├── kb_toolkit.py               # KBToolkit with read-only KB tools

tests/
├── test_toolkit_protocol.py    # Toolkit ABC tests
├── test_kb_toolkit.py          # KBToolkit tools and execution
├── test_conversation_runner.py # Updated: list[Toolkit] tests
├── test_conversation_toolkit.py # Existing: verify no regressions
```

---

## Usage Pattern

```python
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit

# Create toolkits scoped to specific data
kb_tools = KBToolkit(knowledgebases=[docs_kb, faq_kb])
history_tools = ChatWithHistoryToolkit(sessions=[prev_session])

# Build initial context
initial_message = (
    "You have access to the following knowledge bases and conversation history.\n\n"
    + kb_tools.render_overview()
    + "\n\n"
    + history_tools.render_overview()
)

# Run conversation with both toolkits
async for response in run_conversation_turn(
    engine, session, initial_message,
    extra_tools=[kb_tools, history_tools],
):
    yield response
```

---

## Relationship to Existing Code

- `ChatWithHistoryToolkit` in `history_toolkit.py` → inherits from `Toolkit`, no behavioral changes
- `run_conversation_turn` in `runner.py` → `extra_tools` changes from `ChatWithHistoryToolkit | None` to `list[Toolkit] | None`
- `Engine.send()` in `engine.py` → extended with `additional_tools` parameter
- `ClaudeAPIEngine`, `ClaudeCLIEngine`, `OpenAIAPIEngine` → updated to pass additional tools in API/CLI calls
- `kb_tools.py` → unchanged, continues to work via global registry
- `ArticleQuerySet` methods → used by KBToolkit for search, unchanged
- `Knowledgebase.get_table_of_contents()` → used by KBToolkit for TOC rendering
