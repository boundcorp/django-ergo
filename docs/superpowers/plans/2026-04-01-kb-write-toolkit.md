# KBWriteToolkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `KBWriteToolkit` that provides create/update/delete tools for KB articles, bound to a single knowledgebase with approval-required write operations.

**Architecture:** `KBWriteToolkit` inherits from `Toolkit` ABC, binds to one KB, provides 3 tools with `requires_approval=True`. Hierarchy code auto-generation supports top-level and child placement. Embedding generation happens automatically via Django's `SemanticTextField` `pre_save` hook.

**Tech Stack:** Django 4.2+, existing Article/Knowledgebase models, Toolkit ABC, ToolConfig

**Spec:** `docs/superpowers/specs/2026-04-01-kb-write-toolkit-design.md`

---

## File Structure

```
src/django_ergo/
├── kb_write_toolkit.py         # NEW: KBWriteToolkit with 3 write tools

tests/
├── test_kb_write_toolkit.py    # NEW: write toolkit tests
```

---

### Task 1: KBWriteToolkit — Tests and Implementation

**Files:**
- Create: `src/django_ergo/kb_write_toolkit.py`
- Create: `tests/test_kb_write_toolkit.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_kb_write_toolkit.py
"""Tests for KBWriteToolkit — scoped KB write tools."""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.kb_write_toolkit import KBWriteToolkit
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
        content="Welcome to the product documentation.",
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="1", title="Getting Started",
        content="To get started, install the package.",
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="10", title="Installation",
        content="Run pip install mypackage.",
    )
    return kb


@pytest.fixture()
def toolkit(kb):
    return KBWriteToolkit(knowledgebase=kb)


class TestKBWriteToolkitIsToolkit:
    def test_inherits_toolkit(self):
        from django_ergo.conversation.toolkit import Toolkit

        assert issubclass(KBWriteToolkit, Toolkit)


class TestHasTool:
    def test_recognizes_own_tools(self, toolkit):
        assert toolkit.has_tool("kb_create_article") is True
        assert toolkit.has_tool("kb_update_article") is True
        assert toolkit.has_tool("kb_delete_article") is True

    def test_rejects_unknown_tools(self, toolkit):
        assert toolkit.has_tool("kb_search") is False
        assert toolkit.has_tool("kb_list") is False


class TestRenderOverview:
    def test_includes_kb_name(self, toolkit):
        overview = toolkit.render_overview()
        assert "Product Docs" in overview

    def test_includes_article_count(self, toolkit):
        overview = toolkit.render_overview()
        assert "3" in overview

    def test_mentions_approval(self, toolkit):
        overview = toolkit.render_overview()
        assert "approval" in overview.lower()


class TestCreateArticle:
    def test_create_with_explicit_code(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "Deployment", "content": "How to deploy.", "hierarchy_code": "2"},
        )
        assert "2" in result
        assert "Deployment" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="2").exists()

    def test_create_auto_top_level(self, toolkit, kb):
        """Auto-generates next top-level code when no code or parent given."""
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "FAQ", "content": "Frequently asked questions."},
        )
        # Existing top-level codes are "0" and "1", so next should be "2"
        assert "2" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="2").exists()

    def test_create_under_parent(self, toolkit, kb):
        """Creates child article under a parent code."""
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "Config File", "content": "Config details.", "parent_code": "1"},
        )
        # Parent "1" already has child "10", so next child should be "11"
        assert "11" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="11").exists()

    def test_create_under_parent_no_children(self, toolkit, kb):
        """Creates first child under a parent that has no children yet."""
        # Add a new top-level article with no children
        Article.objects.create(
            knowledgebase=kb, hierarchy_code="2", title="API",
            content="API reference.",
        )
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "Endpoints", "content": "API endpoints.", "parent_code": "2"},
        )
        assert "20" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="20").exists()

    def test_create_with_summary(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_create_article",
            {
                "title": "Summary Test",
                "content": "Full content here.",
                "summary": "Brief summary.",
                "hierarchy_code": "3",
            },
        )
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="3")
        assert article.summary == "Brief summary."

    def test_create_conflict_raises(self, toolkit):
        with pytest.raises(ValueError, match="already exists"):
            toolkit.execute_tool(
                "kb_create_article",
                {"title": "Conflict", "content": "x", "hierarchy_code": "0"},
            )

    def test_create_both_code_and_parent_raises(self, toolkit):
        with pytest.raises(ValueError, match="not both"):
            toolkit.execute_tool(
                "kb_create_article",
                {
                    "title": "Bad",
                    "content": "x",
                    "hierarchy_code": "5",
                    "parent_code": "1",
                },
            )


class TestUpdateArticle:
    def test_update_content(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_update_article",
            {"hierarchy_code": "0", "content": "Updated introduction content."},
        )
        assert "0" in result
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.content == "Updated introduction content."

    def test_update_title(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_update_article",
            {"hierarchy_code": "0", "title": "New Title"},
        )
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.title == "New Title"

    def test_update_summary(self, toolkit, kb):
        toolkit.execute_tool(
            "kb_update_article",
            {"hierarchy_code": "0", "summary": "A new summary."},
        )
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.summary == "A new summary."

    def test_update_multiple_fields(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_update_article",
            {
                "hierarchy_code": "0",
                "title": "Intro v2",
                "content": "New content.",
                "summary": "New summary.",
            },
        )
        assert "title" in result.lower()
        assert "content" in result.lower()
        assert "summary" in result.lower()
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.title == "Intro v2"
        assert article.content == "New content."
        assert article.summary == "New summary."

    def test_update_nonexistent_raises(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "kb_update_article",
                {"hierarchy_code": "ZZZ", "content": "x"},
            )

    def test_update_no_fields_raises(self, toolkit):
        with pytest.raises(ValueError, match="No fields"):
            toolkit.execute_tool(
                "kb_update_article",
                {"hierarchy_code": "0"},
            )


class TestDeleteArticle:
    def test_delete_existing(self, toolkit, kb):
        result = toolkit.execute_tool(
            "kb_delete_article",
            {"hierarchy_code": "10"},
        )
        assert "10" in result
        assert "Installation" in result
        assert not Article.objects.filter(knowledgebase=kb, hierarchy_code="10").exists()

    def test_delete_nonexistent_raises(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "kb_delete_article",
                {"hierarchy_code": "ZZZ"},
            )


class TestGetToolsSchema:
    def test_returns_schemas(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        tool_names = [s["name"] for s in schemas]
        assert "kb_create_article" in tool_names
        assert "kb_update_article" in tool_names
        assert "kb_delete_article" in tool_names

    def test_tools_require_approval(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        # All tools should be in the schema (approval is handled at ToolConfig level)
        assert len(schemas) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kb_write_toolkit.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement KBWriteToolkit**

```python
# src/django_ergo/kb_write_toolkit.py
"""KBWriteToolkit — scoped write tools for a single knowledgebase."""

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
            return self._create_article(arguments)
        if tool_name == "kb_update_article":
            return self._update_article(arguments)
        if tool_name == "kb_delete_article":
            return self._delete_article(arguments)
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

    def _create_article(self, args: dict) -> str:
        from django_ergo.models import Article

        title = args["title"]
        content = args["content"]
        summary = args.get("summary")
        hierarchy_code = args.get("hierarchy_code")
        parent_code = args.get("parent_code")
        kb = self.knowledgebase

        if hierarchy_code and parent_code:
            msg = "Provide hierarchy_code or parent_code, not both"
            raise ValueError(msg)

        existing_codes = set(
            kb.articles.values_list("hierarchy_code", flat=True)
        )

        if hierarchy_code:
            if hierarchy_code in existing_codes:
                msg = f"Article with code '{hierarchy_code}' already exists in '{kb.name}'"
                raise ValueError(msg)
        elif parent_code:
            child_codes = {c for c in existing_codes if c.startswith(parent_code) and len(c) == len(parent_code) + 1}
            hierarchy_code = _next_child_code(parent_code, child_codes)
        else:
            top_level_codes = {c for c in existing_codes if len(c) == 1}
            hierarchy_code = _next_hex_code(top_level_codes)

        create_kwargs = {
            "knowledgebase": kb,
            "title": title,
            "content": content,
            "hierarchy_code": hierarchy_code,
        }
        if summary:
            create_kwargs["summary"] = summary

        Article.objects.create(**create_kwargs)
        return f"Created article {hierarchy_code}: \"{title}\" in {kb.name}"

    def _update_article(self, args: dict) -> str:
        hierarchy_code = args["hierarchy_code"]
        kb = self.knowledgebase

        updatable = {k: args[k] for k in ("title", "content", "summary") if k in args}
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

    def _delete_article(self, args: dict) -> str:
        hierarchy_code = args["hierarchy_code"]
        kb = self.knowledgebase

        try:
            article = kb.articles.get(hierarchy_code=hierarchy_code)
        except kb.articles.model.DoesNotExist:
            msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
            raise ValueError(msg) from None

        title = article.title
        article.delete()
        return f"Deleted article {hierarchy_code}: \"{title}\" from {kb.name}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kb_write_toolkit.py -v`
Expected: All PASS

- [ ] **Step 5: Run ruff**

Run: `ruff check --fix src/django_ergo/kb_write_toolkit.py tests/test_kb_write_toolkit.py && ruff format src/django_ergo/kb_write_toolkit.py tests/test_kb_write_toolkit.py`

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/kb_write_toolkit.py tests/test_kb_write_toolkit.py
git commit -m "feat: add KBWriteToolkit with create/update/delete article tools"
```

---

### Task 2: Full Suite Verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/test_kb_toolkit.py tests/test_kb_write_toolkit.py tests/test_toolkit_protocol.py tests/test_conversation_runner.py -v`
Expected: All PASS

- [ ] **Step 2: Run broader regression check**

Run: `python -m pytest tests/test_settings.py tests/test_tool_registry_unit.py -v`
Expected: All PASS

- [ ] **Step 3: Push**

```bash
git push
```
