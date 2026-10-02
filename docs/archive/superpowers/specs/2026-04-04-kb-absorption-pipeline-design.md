# KB Absorption Pipeline Design

**Date:** 2026-04-04
**Status:** Draft
**Author:** Lee / Claude

## Overview

Add an `absorb_conversation` function that reviews a completed conversation and proposes KB changes via `KBSuggestToolkit`. The function composes existing primitives (ConversationRenderer, KBToolkit, KBSuggestToolkit, engine.generate-style flow) into a single pipeline call.

## Problem

We have all the pieces for KB absorption — read tools, suggest tools, conversation rendering, engines — but no composed pipeline that ties them together. A developer who wants "learn from this conversation" has to wire up the renderer, build a prompt, create toolkits, run a conversation turn, and collect suggestions manually. The pipeline function makes this a one-liner.

## Goals

1. **`absorb_conversation()` function** — reviews a session, proposes KB changes via KBSuggestToolkit
2. **Default system prompt** — includes KB name, description, and TOC for scoping
3. **Customizable** — override system prompt, renderer detail level
4. **Returns suggestions** — caller decides when/how to apply

## Non-Goals

- Automatic application of suggestions (caller reviews and applies)
- Post-conversation hooks or scheduling (app concern)
- Batch multi-session absorption (developer loops with single-session function)
- Management command (can be built on top by the consuming app)

---

## The Function

```python
async def absorb_conversation(
    session: ConversationSession,
    target_kb: Knowledgebase,
    engine: Engine,
    system: str | None = None,
    renderer: ConversationRenderer | None = None,
) -> KBSuggestToolkit:
```

### Flow

1. Render the source conversation at skeleton level (or custom renderer)
2. Build the system prompt — use provided `system` or fill in the default template with KB name, description, and current TOC
3. Create a `KBSuggestToolkit` bound to `target_kb`
4. Create a `KBToolkit` bound to `target_kb` (read-only, so the agent can check existing content)
5. Run a conversation turn with both toolkits as `extra_tools`
6. The agent analyzes the conversation and calls `kb_suggest_create` / `kb_suggest_update` / `kb_suggest_delete`
7. Return the `KBSuggestToolkit` with accumulated suggestions

The function creates a temporary `ConversationSession` for the absorption agent's own conversation (separate from the source session being analyzed). This session is marked with `metadata={"absorption_source": str(session.id)}` for traceability.

### Default System Prompt

```python
ABSORB_SYSTEM = """\
You are a knowledge base curator. You are reviewing a conversation to extract \
knowledge worth preserving into the knowledge base described below.

Knowledge Base: {kb_name}
Description: {kb_description}

Current table of contents:
{kb_toc}

Your job:
- Identify facts, decisions, preferences, and context from the conversation \
that belong in this knowledge base
- Use kb_suggest_create to propose new articles for new topics
- Use kb_suggest_update to propose improvements to existing articles
- Use kb_suggest_delete if conversation reveals an article is outdated or wrong
- Only propose changes relevant to this KB's description
- Avoid duplicating information already in the KB
- Use the KB read tools to check existing content before suggesting updates

Be selective. Not everything in a conversation belongs in a knowledge base."""
```

`{kb_name}`, `{kb_description}`, and `{kb_toc}` are formatted in before the call. If `system` is provided, it's used as-is (no template formatting).

### Usage Pattern

```python
from django_ergo.kb_pipelines import absorb_conversation

# After a support conversation finishes:
suggestions = await absorb_conversation(
    session=support_session,
    target_kb=docs_kb,
    engine=engine,
)

# Review what the agent proposed:
for s in suggestions.get_suggestions():
    print(f"{s['action']}: {s.get('title', s.get('hierarchy_code'))}")

# Apply all (or selective):
results = suggestions.apply_suggestions()
```

Custom prompt example:

```python
suggestions = await absorb_conversation(
    session=chat_session,
    target_kb=personal_kb,
    engine=engine,
    system="Extract facts about the user's preferences, habits, and ongoing projects. Create one article per topic.",
)
```

---

## File Organization

```
src/django_ergo/
├── kb_pipelines.py              # NEW: absorb_conversation() + ABSORB_SYSTEM prompt

tests/
├── test_kb_pipelines.py         # NEW: tests with mocked engine
```

---

## Relationship to Existing Code

- `ConversationRenderer` from `conversation/renderer.py` — renders source conversation
- `KBSuggestToolkit` from `kb_suggest_toolkit.py` — accumulates suggestions
- `KBToolkit` from `kb_toolkit.py` — read access for checking existing KB content
- `run_conversation_turn` from `conversation/runner.py` — executes the absorption agent turn
- `ConversationSession` from `conversation/models.py` — temporary session for absorption agent
- `Knowledgebase.description` — included in default system prompt for scoping
- `Knowledgebase.get_table_of_contents()` — included in prompt for context
- `Engine` ABC — any engine can power absorption (Claude API, OpenAI, etc.)
