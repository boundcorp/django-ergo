# KBSuggestToolkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `KBSuggestToolkit` for agents to propose KB changes without executing them, plus refactor `KBWriteToolkit` to extract shared write logic into reusable functions.

**Architecture:** Extract `create_article()`, `update_article()`, `delete_article()` as module-level functions in `kb_write_toolkit.py`. `KBWriteToolkit` delegates to them. New `KBSuggestToolkit` accumulates suggestions in memory, applies them via the same shared functions.

**Tech Stack:** Django 4.2+, existing Article/Knowledgebase models, Toolkit ABC

**Spec:** `docs/superpowers/specs/2026-04-02-kb-suggest-toolkit-design.md`

---

## File Structure

```
src/django_ergo/
├── kb_write_toolkit.py         # MODIFY: extract shared functions, KBWriteToolkit delegates
├── kb_suggest_toolkit.py       # NEW: KBSuggestToolkit

tests/
├── test_kb_write_toolkit.py    # EXISTING: verify no regressions after refactor
├── test_kb_suggest_toolkit.py  # NEW: suggest tools, accumulation, apply, clear
```

---

### Task 1: Refactor KBWriteToolkit — Extract Shared Functions

**Files:**
- Modify: `src/django_ergo/kb_write_toolkit.py`
- Test: `tests/test_kb_write_toolkit.py` (existing — verify no regressions)

- [ ] **Step 1: Extract module-level functions and update KBWriteToolkit**

Replace the full content of `src/django_ergo/kb_write_toolkit.py`:

```python
"""KBWriteToolkit — scoped write tools for a single knowledgebase.

Also provides module-level create_article(), update_article(), delete_article()
functions used by both KBWriteToolkit and KBSuggestToolkit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase


def _next_hex_code(existing_codes: set[str]) -> str:
    """Find the next available single-char hex code (0-9, A-F, then 10+)."""
    for i in range(256):
        code = format(i, "X")
        if code not in existing_codes:
            return code
    msg = "No available hierarchy codes"
    raise ValueError(msg)


def _next_child_code(parent_code: str, existing_codes: set[str]) -> str:
    """Find the next available child code under a parent."""
    for i in range(256):
        suffix = format(i, "X")
        code = f"{parent_code}{suffix}"
        if code not in existing_codes:
            return code
    msg = f"No available child codes under '{parent_code}'"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Shared write functions — used by KBWriteToolkit and KBSuggestToolkit
# ---------------------------------------------------------------------------


def create_article(
    kb: Knowledgebase,
    title: str,
    content: str,
    hierarchy_code: str | None = None,
    parent_code: str | None = None,
    summary: str | None = None,
) -> str:
    """Create an article in the KB. Returns confirmation string."""
    from django_ergo.models import Article

    if hierarchy_code and parent_code:
        msg = "Provide hierarchy_code or parent_code, not both"
        raise ValueError(msg)

    existing_codes = set(kb.articles.values_list("hierarchy_code", flat=True))

    if hierarchy_code:
        if hierarchy_code in existing_codes:
            msg = f"Article with code '{hierarchy_code}' already exists in '{kb.name}'"
            raise ValueError(msg)
    elif parent_code:
        child_codes = {
            c
            for c in existing_codes
            if c.startswith(parent_code) and len(c) == len(parent_code) + 1
        }
        hierarchy_code = _next_child_code(parent_code, child_codes)
    else:
        top_level_codes = {c for c in existing_codes if len(c) == 1}
        hierarchy_code = _next_hex_code(top_level_codes)

    create_kwargs: dict = {
        "knowledgebase": kb,
        "title": title,
        "content": content,
        "hierarchy_code": hierarchy_code,
    }
    if summary:
        create_kwargs["summary"] = summary

    Article.objects.create(**create_kwargs)
    return f'Created article {hierarchy_code}: "{title}" in {kb.name}'


def update_article(
    kb: Knowledgebase,
    hierarchy_code: str,
    title: str | None = None,
    content: str | None = None,
    summary: str | None = None,
) -> str:
    """Update an existing article. Returns confirmation string."""
    updatable = {}
    if title is not None:
        updatable["title"] = title
    if content is not None:
        updatable["content"] = content
    if summary is not None:
        updatable["summary"] = summary

    if not updatable:
        msg = "No fields to update. Provide at least one of: title, content, summary"
        raise ValueError(msg)

    try:
        article = kb.articles.get(hierarchy_code=hierarchy_code)
    except kb.articles.model.DoesNotExist:
        msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
        raise ValueError(msg) from None

    for field, value in updatable.items():
        setattr(article, field, value)
    article.save(update_fields=list(updatable.keys()))

    fields_str = ", ".join(updatable.keys())
    return f"Updated article {hierarchy_code} in {kb.name}: {fields_str}"


def delete_article(kb: Knowledgebase, hierarchy_code: str) -> str:
    """Delete an article. Returns confirmation string."""
    try:
        article = kb.articles.get(hierarchy_code=hierarchy_code)
    except kb.articles.model.DoesNotExist:
        msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
        raise ValueError(msg) from None

    title = article.title
    article.delete()
    return f'Deleted article {hierarchy_code}: "{title}" from {kb.name}'


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

KB_WRITE_TOOLS = [
    {
        "name": "kb_create_article",
        "description": "Create a new article in the knowledge base",
        "parameters": {
            "title": {
                "type": "string",
                "required": True,
                "description": "Article title",
            },
            "content": {
                "type": "string",
                "required": True,
                "description": "Article content",
            },
            "hierarchy_code": {
                "type": "string",
                "required": False,
                "description": "Explicit hierarchy code for placement. Conflicts if already taken.",
            },
            "parent_code": {
                "type": "string",
                "required": False,
                "description": "Place as child of this article. Auto-generates sub-code.",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "Article summary",
            },
        },
    },
    {
        "name": "kb_update_article",
        "description": "Update an existing article's title, content, or summary",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to update",
            },
            "title": {
                "type": "string",
                "required": False,
                "description": "New title",
            },
            "content": {
                "type": "string",
                "required": False,
                "description": "New content",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "New summary",
            },
        },
    },
    {
        "name": "kb_delete_article",
        "description": "Delete an article from the knowledge base",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to delete",
            },
        },
    },
]

KB_WRITE_TOOL_NAMES = {t["name"] for t in KB_WRITE_TOOLS}


class KBWriteToolkit(Toolkit):
    """Scoped toolkit for writing articles to a single knowledgebase."""

    def __init__(self, knowledgebase: Knowledgebase):
        self.knowledgebase = knowledgebase

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_WRITE_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_WRITE_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                requires_approval=True,
                readonly=False,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "kb_create_article":
            return create_article(
                self.knowledgebase,
                title=arguments["title"],
                content=arguments["content"],
                hierarchy_code=arguments.get("hierarchy_code"),
                parent_code=arguments.get("parent_code"),
                summary=arguments.get("summary"),
            )
        if tool_name == "kb_update_article":
            return update_article(
                self.knowledgebase,
                hierarchy_code=arguments["hierarchy_code"],
                title=arguments.get("title"),
                content=arguments.get("content"),
                summary=arguments.get("summary"),
            )
        if tool_name == "kb_delete_article":
            return delete_article(
                self.knowledgebase,
                hierarchy_code=arguments["hierarchy_code"],
            )
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def render_overview(self) -> str:
        kb = self.knowledgebase
        article_count = kb.articles.count()
        return (
            f"=== KB Write Access: {kb.name} (kb_id: {kb.id}) ===\n"
            f"Articles: {article_count}\n"
            f"Available tools: kb_create_article, kb_update_article, kb_delete_article\n"
            f"Note: All write operations require approval."
        )
```

- [ ] **Step 2: Run existing write toolkit tests to verify no regressions**

Run: `python -m pytest tests/test_kb_write_toolkit.py -v`
Expected: All 23 PASS — behavior unchanged

- [ ] **Step 3: Commit**

```bash
git add src/django_ergo/kb_write_toolkit.py
git commit -m "refactor: extract shared create/update/delete functions from KBWriteToolkit"
```

---

### Task 2: KBSuggestToolkit Implementation

**Files:**
- Create: `src/django_ergo/kb_suggest_toolkit.py`
- Create: `tests/test_kb_suggest_toolkit.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_kb_suggest_toolkit.py
"""Tests for KBSuggestToolkit — propose KB changes for later review."""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.kb_suggest_toolkit import KBSuggestToolkit
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def kb(user):
    kb = Knowledgebase.objects.create(
        name="Product Docs",
        description="Complete product documentation",
        owner_id=str(user.id),
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="0", title="Introduction",
        content="Welcome to the docs.",
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="1", title="Getting Started",
        content="Install the package.",
    )
    return kb


@pytest.fixture()
def toolkit(kb):
    return KBSuggestToolkit(knowledgebase=kb)


class TestKBSuggestToolkitIsToolkit:
    def test_inherits_toolkit(self):
        from django_ergo.conversation.toolkit import Toolkit

        assert issubclass(KBSuggestToolkit, Toolkit)


class TestHasTool:
    def test_recognizes_own_tools(self, toolkit):
        assert toolkit.has_tool("kb_suggest_create") is True
        assert toolkit.has_tool("kb_suggest_update") is True
        assert toolkit.has_tool("kb_suggest_delete") is True

    def test_rejects_unknown_tools(self, toolkit):
        assert toolkit.has_tool("kb_create_article") is False
        assert toolkit.has_tool("kb_search") is False


class TestRenderOverview:
    def test_includes_kb_name(self, toolkit):
        overview = toolkit.render_overview()
        assert "Product Docs" in overview

    def test_mentions_suggestions(self, toolkit):
        overview = toolkit.render_overview()
        assert "suggest" in overview.lower()


class TestSuggestCreate:
    def test_records_suggestion(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "Deployment", "content": "How to deploy."},
        )
        assert "suggestion" in result.lower() or "recorded" in result.lower()
        # DB should NOT have the article yet
        assert not Article.objects.filter(knowledgebase=kb, hierarchy_code="2").exists()
        suggestions = toolkit.get_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["action"] == "create"
        assert suggestions[0]["title"] == "Deployment"

    def test_records_with_hierarchy_code(self, toolkit):
        toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "FAQ", "content": "Questions.", "hierarchy_code": "5"},
        )
        suggestions = toolkit.get_suggestions()
        assert suggestions[0]["hierarchy_code"] == "5"

    def test_records_with_parent_code(self, toolkit):
        toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "Config", "content": "Config.", "parent_code": "1"},
        )
        suggestions = toolkit.get_suggestions()
        assert suggestions[0]["parent_code"] == "1"

    def test_records_with_summary(self, toolkit):
        toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "T", "content": "C", "summary": "Brief."},
        )
        suggestions = toolkit.get_suggestions()
        assert suggestions[0]["summary"] == "Brief."


class TestSuggestUpdate:
    def test_records_suggestion(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_suggest_update",
            {"hierarchy_code": "0", "content": "Updated intro."},
        )
        assert "suggestion" in result.lower() or "recorded" in result.lower()
        # DB should NOT be changed yet
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.content == "Welcome to the docs."
        suggestions = toolkit.get_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["action"] == "update"
        assert suggestions[0]["hierarchy_code"] == "0"

    def test_records_multiple_fields(self, toolkit):
        toolkit.execute_tool(
            "kb_suggest_update",
            {"hierarchy_code": "0", "title": "New Title", "content": "New content."},
        )
        suggestions = toolkit.get_suggestions()
        assert suggestions[0]["title"] == "New Title"
        assert suggestions[0]["content"] == "New content."


class TestSuggestDelete:
    def test_records_suggestion(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_suggest_delete",
            {"hierarchy_code": "1"},
        )
        assert "suggestion" in result.lower() or "recorded" in result.lower()
        # DB should NOT have deleted the article
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="1").exists()
        suggestions = toolkit.get_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["action"] == "delete"
        assert suggestions[0]["hierarchy_code"] == "1"


class TestMultipleSuggestions:
    def test_accumulates_in_order(self, toolkit):
        toolkit.execute_tool("kb_suggest_create", {"title": "A", "content": "a"})
        toolkit.execute_tool("kb_suggest_update", {"hierarchy_code": "0", "title": "B"})
        toolkit.execute_tool("kb_suggest_delete", {"hierarchy_code": "1"})
        suggestions = toolkit.get_suggestions()
        assert len(suggestions) == 3
        assert suggestions[0]["action"] == "create"
        assert suggestions[1]["action"] == "update"
        assert suggestions[2]["action"] == "delete"


class TestClear:
    def test_clears_all_suggestions(self, toolkit):
        toolkit.execute_tool("kb_suggest_create", {"title": "A", "content": "a"})
        toolkit.execute_tool("kb_suggest_delete", {"hierarchy_code": "1"})
        assert len(toolkit.get_suggestions()) == 2
        toolkit.clear()
        assert len(toolkit.get_suggestions()) == 0


class TestApplySuggestions:
    def test_apply_all(self, toolkit, kb):
        toolkit.execute_tool("kb_suggest_create", {"title": "New", "content": "New article.", "hierarchy_code": "3"})
        toolkit.execute_tool("kb_suggest_update", {"hierarchy_code": "0", "title": "Updated Intro"})

        results = toolkit.apply_suggestions()
        assert len(results) == 2
        assert "Created" in results[0]
        assert "Updated" in results[1]

        # DB should reflect changes
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="3").exists()
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.title == "Updated Intro"

        # Suggestions should be cleared after apply
        assert len(toolkit.get_suggestions()) == 0

    def test_apply_specific_indices(self, toolkit, kb):
        toolkit.execute_tool("kb_suggest_create", {"title": "A", "content": "a", "hierarchy_code": "3"})
        toolkit.execute_tool("kb_suggest_create", {"title": "B", "content": "b", "hierarchy_code": "4"})
        toolkit.execute_tool("kb_suggest_delete", {"hierarchy_code": "1"})

        results = toolkit.apply_suggestions(indices=[0, 2])
        assert len(results) == 2
        assert "Created" in results[0]  # index 0: create A
        assert "Deleted" in results[1]  # index 2: delete "1"

        # A should exist, B should NOT, "1" should be deleted
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="3").exists()
        assert not Article.objects.filter(knowledgebase=kb, hierarchy_code="4").exists()
        assert not Article.objects.filter(knowledgebase=kb, hierarchy_code="1").exists()

        # Only applied suggestions cleared, unapplied remain
        remaining = toolkit.get_suggestions()
        assert len(remaining) == 1
        assert remaining[0]["title"] == "B"

    def test_apply_continues_on_error(self, toolkit, kb):
        toolkit.execute_tool("kb_suggest_delete", {"hierarchy_code": "NONEXISTENT"})
        toolkit.execute_tool("kb_suggest_create", {"title": "Good", "content": "ok", "hierarchy_code": "5"})

        results = toolkit.apply_suggestions()
        assert len(results) == 2
        assert "not found" in results[0].lower() or "error" in results[0].lower()
        assert "Created" in results[1]
        # Good article should still be created despite first failure
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="5").exists()

    def test_apply_empty_is_noop(self, toolkit):
        results = toolkit.apply_suggestions()
        assert results == []


class TestGetToolsSchema:
    def test_returns_schemas(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        tool_names = [s["name"] for s in schemas]
        assert "kb_suggest_create" in tool_names
        assert "kb_suggest_update" in tool_names
        assert "kb_suggest_delete" in tool_names

    def test_tools_do_not_require_approval(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        assert len(schemas) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kb_suggest_toolkit.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement KBSuggestToolkit**

```python
# src/django_ergo/kb_suggest_toolkit.py
"""KBSuggestToolkit — propose KB changes for later review."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit
from django_ergo.kb_write_toolkit import create_article
from django_ergo.kb_write_toolkit import delete_article
from django_ergo.kb_write_toolkit import update_article

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase

KB_SUGGEST_TOOLS = [
    {
        "name": "kb_suggest_create",
        "description": "Suggest creating a new article in the knowledge base (recorded for later review)",
        "parameters": {
            "title": {
                "type": "string",
                "required": True,
                "description": "Article title",
            },
            "content": {
                "type": "string",
                "required": True,
                "description": "Article content",
            },
            "hierarchy_code": {
                "type": "string",
                "required": False,
                "description": "Explicit hierarchy code for placement",
            },
            "parent_code": {
                "type": "string",
                "required": False,
                "description": "Place as child of this article",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "Article summary",
            },
        },
    },
    {
        "name": "kb_suggest_update",
        "description": "Suggest updating an existing article (recorded for later review)",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to update",
            },
            "title": {
                "type": "string",
                "required": False,
                "description": "New title",
            },
            "content": {
                "type": "string",
                "required": False,
                "description": "New content",
            },
            "summary": {
                "type": "string",
                "required": False,
                "description": "New summary",
            },
        },
    },
    {
        "name": "kb_suggest_delete",
        "description": "Suggest deleting an article (recorded for later review)",
        "parameters": {
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Hierarchy code of the article to delete",
            },
        },
    },
]

KB_SUGGEST_TOOL_NAMES = {t["name"] for t in KB_SUGGEST_TOOLS}


class KBSuggestToolkit(Toolkit):
    """Propose KB changes without executing them. Suggestions accumulate for review."""

    def __init__(self, knowledgebase: Knowledgebase):
        self.knowledgebase = knowledgebase
        self._suggestions: list[dict] = []

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_SUGGEST_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_SUGGEST_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                requires_approval=False,
                readonly=False,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "kb_suggest_create":
            return self._suggest_create(arguments)
        if tool_name == "kb_suggest_update":
            return self._suggest_update(arguments)
        if tool_name == "kb_suggest_delete":
            return self._suggest_delete(arguments)
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def render_overview(self) -> str:
        kb = self.knowledgebase
        article_count = kb.articles.count()
        return (
            f"=== KB Suggestions: {kb.name} (kb_id: {kb.id}) ===\n"
            f"Articles: {article_count}\n"
            f"Available tools: kb_suggest_create, kb_suggest_update, kb_suggest_delete\n"
            f"Suggestions are recorded for later review — no changes are made immediately."
        )

    def get_suggestions(self) -> list[dict]:
        """Return all accumulated suggestions."""
        return list(self._suggestions)

    def clear(self) -> None:
        """Discard all suggestions."""
        self._suggestions.clear()

    def apply_suggestions(self, indices: list[int] | None = None) -> list[str]:
        """Apply suggestions, executing them against the DB.

        Args:
            indices: Specific suggestion indices to apply. If None, apply all.

        Returns:
            List of result strings (one per applied suggestion).
            Failed suggestions include the error message instead of raising.
        """
        if not self._suggestions:
            return []

        if indices is None:
            to_apply = list(enumerate(self._suggestions))
        else:
            to_apply = [(i, self._suggestions[i]) for i in indices]

        results = []
        applied_indices = set()
        for idx, suggestion in to_apply:
            try:
                result = self._execute_suggestion(suggestion)
                results.append(result)
                applied_indices.add(idx)
            except ValueError as e:
                results.append(f"Error: {e}")
                applied_indices.add(idx)

        # Remove applied suggestions in reverse order to preserve indices
        for idx in sorted(applied_indices, reverse=True):
            self._suggestions.pop(idx)

        return results

    def _execute_suggestion(self, suggestion: dict) -> str:
        action = suggestion["action"]
        kb = self.knowledgebase

        if action == "create":
            return create_article(
                kb,
                title=suggestion["title"],
                content=suggestion["content"],
                hierarchy_code=suggestion.get("hierarchy_code"),
                parent_code=suggestion.get("parent_code"),
                summary=suggestion.get("summary"),
            )
        if action == "update":
            return update_article(
                kb,
                hierarchy_code=suggestion["hierarchy_code"],
                title=suggestion.get("title"),
                content=suggestion.get("content"),
                summary=suggestion.get("summary"),
            )
        if action == "delete":
            return delete_article(kb, hierarchy_code=suggestion["hierarchy_code"])

        msg = f"Unknown action: {action}"
        raise ValueError(msg)

    def _suggest_create(self, args: dict) -> str:
        suggestion = {
            "action": "create",
            "title": args["title"],
            "content": args["content"],
            "hierarchy_code": args.get("hierarchy_code"),
            "parent_code": args.get("parent_code"),
            "summary": args.get("summary"),
        }
        self._suggestions.append(suggestion)
        placement = ""
        if args.get("hierarchy_code"):
            placement = f" at code {args['hierarchy_code']}"
        elif args.get("parent_code"):
            placement = f" under parent {args['parent_code']}"
        else:
            placement = " (will be placed at top level)"
        return f'Suggestion recorded: CREATE article "{args["title"]}"{placement}'

    def _suggest_update(self, args: dict) -> str:
        suggestion = {
            "action": "update",
            "hierarchy_code": args["hierarchy_code"],
            "title": args.get("title"),
            "content": args.get("content"),
            "summary": args.get("summary"),
        }
        self._suggestions.append(suggestion)
        fields = [k for k in ("title", "content", "summary") if args.get(k) is not None]
        fields_str = ", ".join(fields) if fields else "no fields"
        return f"Suggestion recorded: UPDATE article {args['hierarchy_code']} ({fields_str})"

    def _suggest_delete(self, args: dict) -> str:
        suggestion = {
            "action": "delete",
            "hierarchy_code": args["hierarchy_code"],
        }
        self._suggestions.append(suggestion)
        return f"Suggestion recorded: DELETE article {args['hierarchy_code']}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kb_suggest_toolkit.py -v`
Expected: All PASS

- [ ] **Step 5: Run ruff on all changed/new files**

Run: `ruff check --fix src/django_ergo/kb_write_toolkit.py src/django_ergo/kb_suggest_toolkit.py tests/test_kb_suggest_toolkit.py && ruff format src/django_ergo/kb_write_toolkit.py src/django_ergo/kb_suggest_toolkit.py tests/test_kb_suggest_toolkit.py`

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/kb_suggest_toolkit.py tests/test_kb_suggest_toolkit.py
git commit -m "feat: add KBSuggestToolkit for proposing KB changes with batch review"
```

---

### Task 3: Full Suite Verification

- [ ] **Step 1: Run all KB toolkit tests**

Run: `python -m pytest tests/test_kb_toolkit.py tests/test_kb_write_toolkit.py tests/test_kb_suggest_toolkit.py tests/test_toolkit_protocol.py -v`
Expected: All PASS

- [ ] **Step 2: Run broader regression check**

Run: `python -m pytest tests/test_settings.py tests/test_tool_registry_unit.py tests/test_conversation_runner.py -v`
Expected: All PASS

- [ ] **Step 3: Push**

```bash
git push
```
