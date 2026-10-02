# KBWriteToolkit Design

**Date:** 2026-04-01
**Status:** Draft
**Author:** Lee / Claude

## Overview

Add a `KBWriteToolkit` — a `Toolkit` subclass bound to a single knowledgebase, providing create/update/delete tools for articles. Complements the existing read-only `KBToolkit` which binds to multiple KBs.

## Problem

The `KBToolkit` we just built is read-only. Agents that need to populate or curate a KB (absorption pipelines, content agents, admin tools) have no way to write articles through the toolkit/conversation framework. The existing `kb_tools.py` global tools don't fit the scoped-toolkit model.

## Goals

1. **Write tools for KB articles** — create, update, delete via a scoped toolkit
2. **Single-KB binding** — write access is always scoped to one KB (read can be multi-KB)
3. **Approval by default** — all write tools require approval, whitelistable via workflow config
4. **Hierarchy code auto-generation** — sensible defaults when the caller doesn't specify placement

## Non-Goals

- KB creation/deletion (use Django ORM directly)
- Absorption pipelines or self-organization (future sub-projects, will use these write tools)
- Bulk operations or batch embedding (YAGNI for now)

---

## KBWriteToolkit

### Construction

```python
write_tools = KBWriteToolkit(knowledgebase=docs_kb)
```

Takes a single `Knowledgebase` instance. Stores it as `self.knowledgebase`.

### Tools

| Tool | Args | Approval | Description |
|------|------|----------|-------------|
| `kb_create_article` | `title` (required), `content` (required), `hierarchy_code` (optional), `parent_code` (optional), `summary` (optional) | Yes | Create a new article |
| `kb_update_article` | `hierarchy_code` (required), `title` (optional), `content` (optional), `summary` (optional) | Yes | Update fields on an existing article |
| `kb_delete_article` | `hierarchy_code` (required) | Yes | Delete an article |

All tools have `requires_approval=True` in their `ToolConfig`.

### Hierarchy Code Auto-Generation

When creating an article:

- **`hierarchy_code` provided**: use it directly. Raise `ValueError` if it conflicts with an existing article.
- **`parent_code` provided**: append under that parent. Find the highest existing child code and increment. E.g., parent `"2"` with children `"20"`, `"21"`, `"22"` → new child gets `"23"`.
- **Neither provided**: create a new top-level article. Find the highest existing single-char code and increment. E.g., if `"3"` is the highest → new article gets `"4"`. Hex increments: after `"9"` comes `"A"`, after `"F"` comes `"10"` (two-char, still top-level-ish but fine for large KBs).

### Tool Behavior Details

**`kb_create_article`**: Creates the article via `Article.objects.create()`. The `SemanticTextField` `pre_save` hook handles embedding generation automatically. Returns a confirmation with the hierarchy code and title.

```
Created article 4: "Deployment Guide" in Product Docs
```

If embedding generation fails (no API key, etc.), the article is still created — just without embeddings. This is existing `SemanticTextField` behavior.

**`kb_update_article`**: Looks up the article by hierarchy code. Updates only the fields that were provided (title, content, summary). Calls `article.save()` which triggers re-embedding if content changed. Returns confirmation listing what was updated.

```
Updated article 2A in Product Docs: content, title
```

Raises `ValueError` if the article doesn't exist.

**`kb_delete_article`**: Looks up and deletes the article. Returns confirmation.

```
Deleted article 2A: "Authentication Setup" from Product Docs
```

Raises `ValueError` if the article doesn't exist.

### render_overview()

Returns a brief description of write capabilities:

```
=== KB Write Access: Product Docs (kb_id: abc-123) ===
Articles: 47
Available tools: kb_create_article, kb_update_article, kb_delete_article
Note: All write operations require approval.
```

### Usage Pattern

```python
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.kb_write_toolkit import KBWriteToolkit

# Read from multiple KBs, write to one
read_tools = KBToolkit(knowledgebases=[docs_kb, faq_kb])
write_tools = KBWriteToolkit(knowledgebase=docs_kb)

async for response in run_conversation_turn(
    engine, session, message,
    extra_tools=[read_tools, write_tools],
):
    yield response
```

---

## File Organization

```
src/django_ergo/
├── kb_toolkit.py               # Existing: KBToolkit (read-only, multi-KB)
├── kb_write_toolkit.py         # NEW: KBWriteToolkit (write, single-KB)

tests/
├── test_kb_toolkit.py          # Existing: read toolkit tests
├── test_kb_write_toolkit.py    # NEW: write toolkit tests
```

---

## Relationship to Existing Code

- `KBToolkit` in `kb_toolkit.py` → unchanged, continues to be read-only
- `Toolkit` ABC in `conversation/toolkit.py` → KBWriteToolkit inherits from it
- `ToolConfig` in `tools.py` → used with `requires_approval=True` for write tools
- `Article` model in `models.py` → written to via standard Django ORM
- `SemanticTextField` `pre_save` hook → handles embedding generation on create/update
- `run_conversation_turn` in `runner.py` → already supports `list[Toolkit]`, no changes needed
- `kb_tools.py` → unchanged, legacy global tools remain separate
