# KBSuggestToolkit Design

**Date:** 2026-04-02
**Status:** Draft
**Author:** Lee / Claude

## Overview

Add a `KBSuggestToolkit` that lets agents propose KB changes (create/update/delete articles) without immediately executing them. Suggestions accumulate in memory for human review. Also refactor `KBWriteToolkit` to extract shared write logic into reusable module-level functions.

## Problem

The `KBWriteToolkit` executes changes immediately (with per-operation approval). For absorption pipelines and AI-driven curation, a "suggest then review" pattern is more appropriate — the agent proposes a batch of changes, a human reviews them, then selectively applies. This is the pull request model for KB changes.

## Goals

1. **KBSuggestToolkit** — 3 tools that accumulate suggestions in memory without modifying the DB
2. **Batch review API** — `get_suggestions()`, `apply_suggestions()`, `clear()`
3. **Shared write logic** — extract CRUD functions from `KBWriteToolkit` so both toolkits use the same code
4. **No approval needed** — suggest tools are inert, the review step IS the approval

## Non-Goals

- Persisting suggestions to DB (consuming app can do this if needed)
- UI for reviewing suggestions (app concern)
- Absorption pipeline (future sub-project, will use this toolkit)

---

## Component 1: Shared Write Functions

Extract from `KBWriteToolkit` into module-level functions in `kb_write_toolkit.py`:

```python
def create_article(kb, title, content, hierarchy_code=None, parent_code=None, summary=None) -> str:
    """Create an article in the KB. Returns confirmation string."""

def update_article(kb, hierarchy_code, title=None, content=None, summary=None) -> str:
    """Update an existing article. Returns confirmation string."""

def delete_article(kb, hierarchy_code) -> str:
    """Delete an article. Returns confirmation string."""
```

`KBWriteToolkit._create_article()`, `_update_article()`, `_delete_article()` become thin wrappers calling these functions. The hierarchy code helpers (`_next_hex_code`, `_next_child_code`) are already module-level.

This is a pure refactor — no behavioral changes, existing tests continue to pass.

---

## Component 2: KBSuggestToolkit

### Construction

```python
suggest = KBSuggestToolkit(knowledgebase=docs_kb)
```

Bound to a single KB, same as `KBWriteToolkit`.

### Tools

| Tool | Args | Behavior |
|------|------|----------|
| `kb_suggest_create` | `title` (required), `content` (required), optional `hierarchy_code`/`parent_code`/`summary` | Appends a create suggestion to internal list. Returns confirmation. |
| `kb_suggest_update` | `hierarchy_code` (required), optional `title`/`content`/`summary` | Appends an update suggestion. Returns confirmation. |
| `kb_suggest_delete` | `hierarchy_code` (required) | Appends a delete suggestion. Returns confirmation. |

No `requires_approval` — suggestions don't modify the DB. Tool responses confirm the suggestion was recorded:

```
Suggestion recorded: CREATE article "Deployment Guide" (will be placed at top level)
```

### Review API

```python
# Get all accumulated suggestions
suggestions = suggest.get_suggestions()
# Returns: [
#   {"action": "create", "title": "Deployment Guide", "content": "...", "parent_code": None, "hierarchy_code": None, "summary": None},
#   {"action": "update", "hierarchy_code": "2A", "content": "...improved...", "title": None, "summary": None},
#   {"action": "delete", "hierarchy_code": "0F"},
# ]

# Apply all suggestions (executes via shared write functions)
results = suggest.apply_suggestions()
# Returns: ["Created article 4: ...", "Updated article 2A: ...", "Deleted article 0F: ..."]

# Or apply specific ones by index
results = suggest.apply_suggestions(indices=[0, 1])

# Discard all suggestions
suggest.clear()
```

`apply_suggestions()` calls the shared `create_article()`, `update_article()`, `delete_article()` functions from `kb_write_toolkit.py`. If a suggestion fails (e.g., article doesn't exist for update), it includes the error in results and continues with the remaining suggestions.

### render_overview()

```
=== KB Suggestions: Product Docs (kb_id: abc-123) ===
Articles: 47
Available tools: kb_suggest_create, kb_suggest_update, kb_suggest_delete
Suggestions are recorded for later review — no changes are made immediately.
```

---

## File Organization

```
src/django_ergo/
├── kb_write_toolkit.py         # MODIFY: extract shared functions, KBWriteToolkit delegates
├── kb_suggest_toolkit.py       # NEW: KBSuggestToolkit

tests/
├── test_kb_write_toolkit.py    # EXISTING: verify no regressions after refactor
├── test_kb_suggest_toolkit.py  # NEW: suggest tools, accumulation, apply, clear
```

---

## Relationship to Existing Code

- `KBWriteToolkit` in `kb_write_toolkit.py` → refactored to use module-level functions, no behavioral changes
- `Toolkit` ABC → KBSuggestToolkit inherits from it
- `ToolConfig` → used with `requires_approval=False`, `readonly=False` for suggest tools
- `Article` model → written to only when suggestions are applied
- Runner / engines → no changes needed
