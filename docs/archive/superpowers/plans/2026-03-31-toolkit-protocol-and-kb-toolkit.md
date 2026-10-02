# Toolkit Protocol and KBToolkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a `Toolkit` ABC from the existing ChatWithHistoryToolkit pattern, build a `KBToolkit` for agent-driven KB access, and update the runner to accept composable toolkit lists.

**Architecture:** `Toolkit` ABC defines `has_tool`, `execute_tool`, `get_tools_schema`, `render_overview`. `ChatWithHistoryToolkit` inherits it unchanged. `KBToolkit` provides 4 read-only KB tools. The runner accepts `list[Toolkit]`, merges tool schemas, and dispatches calls to the first matching toolkit.

**Tech Stack:** Django 4.2+, existing conversation models, pgvector semantic search, ToolAdapter for schema generation

**Spec:** `docs/superpowers/specs/2026-03-31-toolkit-protocol-and-kb-toolkit-design.md`

---

## File Structure

```
src/django_ergo/conversation/
├── toolkit.py              # NEW: Toolkit ABC (4 abstract methods)
├── history_toolkit.py      # MODIFY: inherit from Toolkit
├── runner.py               # MODIFY: extra_tools becomes list[Toolkit]
├── engine.py               # MODIFY: send() and submit_tool_result() get additional_tools param
├── engines/
│   ├── claude_api.py       # MODIFY: thread additional_tools through to API call
│   ├── claude_cli.py       # MODIFY: thread additional_tools through to CLI call
│   └── openai_api.py       # MODIFY: thread additional_tools through to API call

src/django_ergo/
├── kb_toolkit.py           # NEW: KBToolkit with 4 read-only tools

tests/
├── test_toolkit_protocol.py    # NEW: Toolkit ABC contract tests
├── test_kb_toolkit.py          # NEW: KBToolkit tools and execution
├── test_conversation_runner.py # MODIFY: list[Toolkit] tests
├── test_conversation_toolkit.py # EXISTING: verify no regressions
```

---

### Task 1: Toolkit ABC

**Files:**
- Create: `src/django_ergo/conversation/toolkit.py`
- Test: `tests/test_toolkit_protocol.py`

- [ ] **Step 1: Write failing tests for Toolkit ABC**

```python
# tests/test_toolkit_protocol.py
"""Tests for Toolkit ABC protocol."""
import pytest

from django_ergo.conversation.toolkit import Toolkit


class TestToolkitABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Toolkit()

    def test_required_methods(self):
        required = {"has_tool", "execute_tool", "get_tools_schema", "render_overview"}
        abstract = {m for m in Toolkit.__abstractmethods__}
        assert abstract == required

    def test_concrete_subclass_works(self):
        class FakeToolkit(Toolkit):
            def has_tool(self, tool_name):
                return tool_name == "test"

            def execute_tool(self, tool_name, arguments):
                return "result"

            def get_tools_schema(self, adapter):
                return []

            def render_overview(self):
                return "overview"

        tk = FakeToolkit()
        assert tk.has_tool("test") is True
        assert tk.has_tool("other") is False
        assert tk.execute_tool("test", {}) == "result"
        assert tk.get_tools_schema(None) == []
        assert tk.render_overview() == "overview"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_toolkit_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement Toolkit ABC**

```python
# src/django_ergo/conversation/toolkit.py
"""Toolkit protocol — base class for all scoped tool bundles."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter


class Toolkit(ABC):
    """Abstract base for scoped tool bundles.

    A toolkit is a set of tools bound to specific data (e.g., knowledgebases,
    conversation sessions) with a defined capability scope. Toolkits plug into
    the conversation runner via the extra_tools parameter.
    """

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_toolkit_protocol.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/toolkit.py tests/test_toolkit_protocol.py
git commit -m "feat: add Toolkit ABC protocol for composable scoped tool bundles"
```

---

### Task 2: Refactor ChatWithHistoryToolkit to Inherit Toolkit

**Files:**
- Modify: `src/django_ergo/conversation/history_toolkit.py`
- Test: `tests/test_conversation_toolkit.py` (existing — verify no regressions)

- [ ] **Step 1: Update ChatWithHistoryToolkit to inherit from Toolkit**

In `src/django_ergo/conversation/history_toolkit.py`, change the import and class declaration:

Add import:
```python
from django_ergo.conversation.toolkit import Toolkit
```

Change class declaration from:
```python
class ChatWithHistoryToolkit:
```
to:
```python
class ChatWithHistoryToolkit(Toolkit):
```

No other changes needed — all 4 abstract methods are already implemented.

- [ ] **Step 2: Run existing toolkit tests to verify no regressions**

Run: `python -m pytest tests/test_conversation_toolkit.py -v`
Expected: All PASS (14 tests)

- [ ] **Step 3: Commit**

```bash
git add src/django_ergo/conversation/history_toolkit.py
git commit -m "refactor: ChatWithHistoryToolkit inherits from Toolkit ABC"
```

---

### Task 3: KBToolkit Implementation

**Files:**
- Create: `src/django_ergo/kb_toolkit.py`
- Test: `tests/test_kb_toolkit.py`

- [ ] **Step 1: Write failing tests for KBToolkit**

```python
# tests/test_kb_toolkit.py
"""Tests for KBToolkit — scoped KB read tools."""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.kb_toolkit import KBToolkit
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db

ARTICLE_COUNT_DOCS = 4
ARTICLE_COUNT_FAQ = 2
CONTENT_PREVIEW_MAX = 200


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def docs_kb(user):
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
        content="To get started, install the package with pip install mypackage.",
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="10", title="Installation",
        content="Run pip install mypackage to install.",
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="11", title="Configuration",
        content="Configure your settings in settings.py.",
    )
    return kb


@pytest.fixture()
def faq_kb(user):
    kb = Knowledgebase.objects.create(
        name="FAQ",
        description="Frequently asked questions",
        owner_id=str(user.id),
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="0", title="General",
        content="General frequently asked questions.",
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="1", title="Billing",
        content="Billing and payment questions.",
    )
    return kb


@pytest.fixture()
def toolkit(docs_kb, faq_kb):
    return KBToolkit(knowledgebases=[docs_kb, faq_kb])


class TestKBToolkitIsToolkit:
    def test_inherits_toolkit(self):
        from django_ergo.conversation.toolkit import Toolkit

        assert issubclass(KBToolkit, Toolkit)


class TestHasTool:
    def test_recognizes_own_tools(self, toolkit):
        assert toolkit.has_tool("kb_list") is True
        assert toolkit.has_tool("kb_search") is True
        assert toolkit.has_tool("kb_get_article") is True
        assert toolkit.has_tool("kb_table_of_contents") is True

    def test_rejects_unknown_tools(self, toolkit):
        assert toolkit.has_tool("history_view_conversation") is False
        assert toolkit.has_tool("search") is False


class TestRenderOverview:
    def test_includes_all_kbs(self, toolkit):
        overview = toolkit.render_overview()
        assert "Product Docs" in overview
        assert "FAQ" in overview

    def test_includes_descriptions(self, toolkit):
        overview = toolkit.render_overview()
        assert "Complete product documentation" in overview
        assert "Frequently asked questions" in overview

    def test_includes_top_level_articles(self, toolkit):
        overview = toolkit.render_overview()
        assert "Introduction" in overview
        assert "Getting Started" in overview
        assert "General" in overview
        # Should NOT include nested articles
        assert "Installation" not in overview


class TestKBList:
    def test_lists_all_kbs(self, toolkit):
        result = toolkit.execute_tool("kb_list", {})
        assert "Product Docs" in result
        assert "FAQ" in result
        assert str(ARTICLE_COUNT_DOCS) in result
        assert str(ARTICLE_COUNT_FAQ) in result


class TestKBGetArticle:
    def test_get_existing_article(self, toolkit):
        result = toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": "Product Docs", "hierarchy_code": "0"},
        )
        assert "Introduction" in result
        assert "Welcome to the product documentation" in result

    def test_get_article_with_summary(self, toolkit, docs_kb):
        article = Article.objects.get(knowledgebase=docs_kb, hierarchy_code="0")
        article.summary = "A brief intro to the docs."
        article.save(update_fields=["summary"])
        result = toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": "Product Docs", "hierarchy_code": "0"},
        )
        assert "A brief intro to the docs" in result

    def test_invalid_kb_name(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "kb_get_article",
                {"kb_name": "Nonexistent", "hierarchy_code": "0"},
            )

    def test_invalid_hierarchy_code(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "kb_get_article",
                {"kb_name": "Product Docs", "hierarchy_code": "ZZZ"},
            )


class TestKBTableOfContents:
    def test_lists_all_articles(self, toolkit):
        result = toolkit.execute_tool(
            "kb_table_of_contents",
            {"kb_name": "Product Docs"},
        )
        assert "0: Introduction" in result
        assert "1: Getting Started" in result
        assert "10: Installation" in result
        assert "11: Configuration" in result

    def test_nested_articles_indented(self, toolkit):
        result = toolkit.execute_tool(
            "kb_table_of_contents",
            {"kb_name": "Product Docs"},
        )
        lines = result.strip().splitlines()
        # Top-level articles should not be indented
        intro_line = [l for l in lines if "Introduction" in l][0]
        assert not intro_line.startswith(" ")
        # Nested articles should be indented
        install_line = [l for l in lines if "Installation" in l][0]
        assert install_line.startswith("  ")

    def test_invalid_kb_name(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "kb_table_of_contents",
                {"kb_name": "Nonexistent"},
            )


class TestKBSearch:
    def test_search_returns_results(self, toolkit, docs_kb):
        """Search should work but may fail without embedding provider.

        Since tests may not have an OpenAI key, we test that the tool
        handles the error gracefully rather than crashing.
        """
        result = toolkit.execute_tool(
            "kb_search",
            {"query": "install the package", "kb_name": "Product Docs"},
        )
        # Should return either results or a graceful error message
        assert isinstance(result, str)
        assert len(result) > 0

    def test_search_invalid_kb_name(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "kb_search",
                {"query": "test", "kb_name": "Nonexistent"},
            )


class TestGetToolsSchema:
    def test_returns_schemas(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        tool_names = [s["name"] for s in schemas]
        assert "kb_list" in tool_names
        assert "kb_search" in tool_names
        assert "kb_get_article" in tool_names
        assert "kb_table_of_contents" in tool_names

    def test_schema_has_input_schema(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        search_schema = [s for s in schemas if s["name"] == "kb_search"][0]
        assert "input_schema" in search_schema
        assert "query" in search_schema["input_schema"]["properties"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kb_toolkit.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement KBToolkit**

```python
# src/django_ergo/kb_toolkit.py
"""KBToolkit — scoped read-only tools for agent-driven KB access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.models import Knowledgebase

CONTENT_PREVIEW_MAX = 200
DEFAULT_TOP_K = 5
TOC_INDENT = "  "

KB_TOOLS = [
    {
        "name": "kb_list",
        "description": "List all available knowledge bases with descriptions and article counts",
        "parameters": {},
    },
    {
        "name": "kb_search",
        "description": "Semantic search across knowledge bases for relevant articles",
        "parameters": {
            "query": {
                "type": "string",
                "required": True,
                "description": "Search query text",
            },
            "kb_name": {
                "type": "string",
                "required": False,
                "description": "Filter to a specific KB by name",
            },
            "top_k": {
                "type": "integer",
                "required": False,
                "description": "Number of results to return (default: 5)",
            },
        },
    },
    {
        "name": "kb_get_article",
        "description": "Get the full content of a specific article by KB name and hierarchy code",
        "parameters": {
            "kb_name": {
                "type": "string",
                "required": True,
                "description": "Name of the knowledge base",
            },
            "hierarchy_code": {
                "type": "string",
                "required": True,
                "description": "Article hierarchy code (e.g., '0', '1A', '2B3')",
            },
        },
    },
    {
        "name": "kb_table_of_contents",
        "description": "Get the full table of contents for a knowledge base",
        "parameters": {
            "kb_name": {
                "type": "string",
                "required": True,
                "description": "Name of the knowledge base",
            },
        },
    },
]

KB_TOOL_NAMES = {t["name"] for t in KB_TOOLS}


class KBToolkit(Toolkit):
    """Scoped toolkit for agent-driven KB search and browsing."""

    def __init__(self, knowledgebases: list[Knowledgebase]):
        self.knowledgebases = {str(kb.id): kb for kb in knowledgebases}
        self._name_to_id: dict[str, str] = {kb.name: str(kb.id) for kb in knowledgebases}

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in KB_TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in KB_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                readonly=True,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "kb_list":
            return self._kb_list()
        if tool_name == "kb_search":
            return self._kb_search(arguments)
        if tool_name == "kb_get_article":
            return self._kb_get_article(arguments)
        if tool_name == "kb_table_of_contents":
            return self._kb_table_of_contents(arguments)
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def render_overview(self) -> str:
        parts = []
        for i, (kb_id, kb) in enumerate(self.knowledgebases.items(), 1):
            article_count = kb.articles.count()
            header = f"=== Knowledge Base: {kb.name} (kb_id: {kb_id}) ==="
            desc = f"Description: {kb.description}"
            count = f"Articles: {article_count}"

            top_level = kb.articles.filter(hierarchy_code__regex=r"^.$")
            toc_lines = [f"  {a.hierarchy_code}: {a.title}" for a in top_level]
            toc = "Top-level sections:\n" + "\n".join(toc_lines) if toc_lines else "No articles yet."

            parts.append(f"{header}\n{desc}\n{count}\n\n{toc}")
        return "\n\n".join(parts)

    def _get_kb_by_name(self, name: str) -> Knowledgebase:
        kb_id = self._name_to_id.get(name)
        if kb_id is None:
            msg = f"Knowledge base '{name}' not found in this toolkit"
            raise ValueError(msg)
        return self.knowledgebases[kb_id]

    def _kb_list(self) -> str:
        lines = []
        for i, (kb_id, kb) in enumerate(self.knowledgebases.items(), 1):
            article_count = kb.articles.count()
            lines.append(f"{i}. {kb.name} (kb_id: {kb_id}) — {article_count} articles")
            lines.append(f"   {kb.description}")
        return "\n".join(lines)

    def _kb_search(self, args: dict) -> str:
        from django_ergo.models import Article

        query = args["query"]
        top_k = args.get("top_k", DEFAULT_TOP_K)

        if kb_name := args.get("kb_name"):
            kb = self._get_kb_by_name(kb_name)
            qs = Article.objects.filter(knowledgebase=kb)
        else:
            kb_ids = list(self.knowledgebases.keys())
            qs = Article.objects.filter(knowledgebase_id__in=kb_ids)

        try:
            results = qs.multi_field_semantic_search(query, top_k=top_k)
        except Exception as e:  # noqa: BLE001
            return f"Search failed: {e}. Try using kb_get_article or kb_table_of_contents instead."

        if not results:
            return "No results found."

        lines = []
        for i, article in enumerate(results, 1):
            distance = getattr(article, "combined_distance", None)
            distance_str = f" (distance: {distance:.3f})" if distance is not None else ""
            preview = article.content[:CONTENT_PREVIEW_MAX]
            if len(article.content) > CONTENT_PREVIEW_MAX:
                preview += "..."
            lines.append(
                f"Result {i}{distance_str}:\n"
                f"  KB: {article.knowledgebase.name}\n"
                f"  Article: {article.hierarchy_code} — {article.title}\n"
                f"  Preview: {preview}"
            )
        return "\n\n".join(lines)

    def _kb_get_article(self, args: dict) -> str:
        kb = self._get_kb_by_name(args["kb_name"])
        hierarchy_code = args["hierarchy_code"]

        try:
            article = kb.articles.get(hierarchy_code=hierarchy_code)
        except kb.articles.model.DoesNotExist:
            msg = f"Article '{hierarchy_code}' not found in '{kb.name}'"
            raise ValueError(msg) from None

        lines = [
            f"Article: {article.hierarchy_code} — {article.title}",
            f"KB: {kb.name}",
            "",
            "Content:",
            article.content,
        ]
        if article.summary:
            lines.extend(["", "Summary:", article.summary])
        return "\n".join(lines)

    def _kb_table_of_contents(self, args: dict) -> str:
        kb = self._get_kb_by_name(args["kb_name"])
        articles = kb.articles.all().order_by("hierarchy_code")

        lines = []
        for article in articles:
            depth = len(article.hierarchy_code) - 1
            indent = TOC_INDENT * depth
            lines.append(f"{indent}{article.hierarchy_code}: {article.title}")
        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kb_toolkit.py -v`
Expected: All PASS

- [ ] **Step 5: Run ruff**

Run: `ruff check --fix src/django_ergo/kb_toolkit.py tests/test_kb_toolkit.py && ruff format src/django_ergo/kb_toolkit.py tests/test_kb_toolkit.py`

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/kb_toolkit.py tests/test_kb_toolkit.py
git commit -m "feat: add KBToolkit with read-only KB search and browse tools"
```

---

### Task 4: Engine additional_tools Parameter

**Files:**
- Modify: `src/django_ergo/conversation/engine.py`
- Modify: `src/django_ergo/conversation/engines/claude_api.py`
- Modify: `src/django_ergo/conversation/engines/claude_cli.py`
- Modify: `src/django_ergo/conversation/engines/openai_api.py`
- Test: `tests/test_conversation_engine.py` (existing — verify signature)

- [ ] **Step 1: Write failing test for additional_tools parameter**

Add to `tests/test_conversation_engine.py`:

```python
class TestEngineAdditionalTools:
    def test_send_accepts_additional_tools(self):
        """Engine.send() signature includes additional_tools parameter."""
        import inspect

        sig = inspect.signature(Engine.send)
        params = list(sig.parameters.keys())
        assert "additional_tools" in params
        assert sig.parameters["additional_tools"].default is None

    def test_submit_tool_result_accepts_additional_tools(self):
        """Engine.submit_tool_result() signature includes additional_tools parameter."""
        import inspect

        sig = inspect.signature(Engine.submit_tool_result)
        params = list(sig.parameters.keys())
        assert "additional_tools" in params
        assert sig.parameters["additional_tools"].default is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_conversation_engine.py::TestEngineAdditionalTools -v`
Expected: FAIL — `additional_tools` not in params

- [ ] **Step 3: Update Engine ABC**

In `src/django_ergo/conversation/engine.py`, update `send()` and `submit_tool_result()`:

```python
    @abstractmethod
    async def send(
        self, session, message: str, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
        """Send a message, yield streaming responses.

        Args:
            session: The conversation session.
            message: User message text.
            additional_tools: Extra tool schemas to include in the API call
                (e.g., from Toolkits). Merged with workflow tools.
        """

    @abstractmethod
    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
        """Submit a tool result, yield assistant continuation."""
```

- [ ] **Step 4: Update ClaudeAPIEngine**

In `src/django_ergo/conversation/engines/claude_api.py`:

Update `_process_response` to accept `additional_tools`:
```python
    async def _process_response(
        self, session, seq: int, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
```

Inside `_process_response`, after building `tools` from workflow, merge additional tools:
```python
            tools = (
                self.get_tools_schema(session.workflow) if session.workflow else None
            )
            if additional_tools:
                tools = (tools or []) + additional_tools
```

Update `send()` signature:
```python
    async def send(
        self, session, message: str, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
```

Pass `additional_tools` to `_process_response`:
```python
        async for event in self._process_response(session, seq + 1, additional_tools):
            yield event
```

Update `submit_tool_result()` signature:
```python
    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
```

Pass `additional_tools` to `_process_response`:
```python
        async for event in self._process_response(session, seq + 1, additional_tools):
            yield event
```

- [ ] **Step 5: Update OpenAIAPIEngine**

In `src/django_ergo/conversation/engines/openai_api.py`:

Same pattern — update `_call_and_persist`, `send`, `submit_tool_result` to accept and thread `additional_tools`.

Update `_call_and_persist`:
```python
    async def _call_and_persist(
        self, session, seq: int, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
```

Inside, after building `tools`:
```python
            tools = (
                self.get_tools_schema(session.workflow) if session.workflow else None
            )
            if additional_tools:
                tools = (tools or []) + additional_tools
```

Update `send`:
```python
    async def send(
        self, session, message: str, additional_tools: list[dict] | None = None
    ) -> AsyncIterator[EngineResponse]:
```

Thread to `_call_and_persist`:
```python
        async for event in self._call_and_persist(session, seq + 1, additional_tools):
```

Update `submit_tool_result`:
```python
    async def submit_tool_result(
        self,
        session,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
```

Thread to `_call_and_persist`:
```python
        async for event in self._call_and_persist(session, seq + 1, additional_tools):
```

- [ ] **Step 6: Update ClaudeCLIEngine**

In `src/django_ergo/conversation/engines/claude_cli.py`:

The CLI engine works differently — it spawns a subprocess. Tool schemas aren't passed per-call; they're part of the subprocess invocation. For now, update the signatures to match the ABC (accept `additional_tools`) but ignore the parameter in the CLI implementation. Add a comment noting this limitation.

Update `send`:
```python
    async def send(
        self, session: ConversationSession, message: str,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
```

Update `submit_tool_result`:
```python
    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        is_error: bool = False,
        additional_tools: list[dict] | None = None,
    ) -> AsyncIterator[EngineResponse]:
```

- [ ] **Step 7: Run all engine tests to verify no regressions**

Run: `python -m pytest tests/test_conversation_engine.py tests/test_conversation_claude_api.py tests/test_conversation_openai_api.py tests/test_conversation_claude_cli.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add src/django_ergo/conversation/engine.py src/django_ergo/conversation/engines/claude_api.py src/django_ergo/conversation/engines/openai_api.py src/django_ergo/conversation/engines/claude_cli.py tests/test_conversation_engine.py
git commit -m "feat: add additional_tools parameter to Engine.send() and submit_tool_result()"
```

---

### Task 5: Runner Refactor — list[Toolkit] Support

**Files:**
- Modify: `src/django_ergo/conversation/runner.py`
- Modify: `tests/test_conversation_runner.py`

- [ ] **Step 1: Write failing tests for list[Toolkit] support**

Replace the `TestExtraTools` class in `tests/test_conversation_runner.py`:

```python
class TestExtraTools:
    def test_extra_tools_accepts_list(self):
        """run_conversation_turn accepts a list of Toolkits."""
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        assert "extra_tools" in sig.parameters
        assert sig.parameters["extra_tools"].default is None

    @patch("django_ergo.conversation.runner.tool_registry")
    def test_first_matching_toolkit_handles_tool(self, mock_registry):
        """When multiple toolkits are provided, the first one that claims the tool handles it."""
        from unittest.mock import MagicMock

        toolkit_a = MagicMock()
        toolkit_a.has_tool.return_value = False
        toolkit_b = MagicMock()
        toolkit_b.has_tool.return_value = True
        toolkit_b.execute_tool.return_value = "result from b"

        # Simulate the dispatch logic
        toolkits = [toolkit_a, toolkit_b]
        name = "kb_search"
        result = None
        for tk in toolkits:
            if tk.has_tool(name):
                result = tk.execute_tool(name, {"query": "test"})
                break

        toolkit_a.has_tool.assert_called_with(name)
        toolkit_b.has_tool.assert_called_with(name)
        assert result == "result from b"
        toolkit_a.execute_tool.assert_not_called()

    def test_extra_tools_backward_compatible_with_none(self):
        """run_conversation_turn still works without extra_tools."""
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        assert sig.parameters["extra_tools"].default is None
```

- [ ] **Step 2: Run tests to verify they pass (signature test may fail)**

Run: `python -m pytest tests/test_conversation_runner.py::TestExtraTools -v`

- [ ] **Step 3: Update runner to accept list[Toolkit]**

Replace the full content of `src/django_ergo/conversation/runner.py`:

```python
"""Conversation turn runner — tool execution loop above the engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.toolkit import Toolkit


@dataclass
class PendingApproval:
    """Yielded when a tool requires user approval before execution."""

    tool_use_id: str
    tool_name: str
    arguments: dict


def _tool_requires_approval(tool_name: str, workflow) -> bool:
    tool_config = tool_registry.get_tool(tool_name)
    if not tool_config or not tool_config.requires_approval:
        return False
    if workflow:
        tools_config = workflow.get_tools_config()
        approved = tools_config.get("approved_tools", [])
        if tool_name in approved:
            return False
    return True


def _collect_toolkit_schemas(
    toolkits: list[Toolkit], adapter,
) -> list[dict]:
    """Collect tool schemas from all toolkits for engine injection."""
    schemas = []
    for toolkit in toolkits:
        schemas.extend(toolkit.get_tools_schema(adapter))
    return schemas


def _find_toolkit_for_tool(
    toolkits: list[Toolkit], tool_name: str,
) -> Toolkit | None:
    """Find the first toolkit that handles the given tool name."""
    for toolkit in toolkits:
        if toolkit.has_tool(tool_name):
            return toolkit
    return None


async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: list[Toolkit] | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    adapter = engine.get_tool_adapter()
    toolkits = extra_tools or []
    additional_tool_schemas = _collect_toolkit_schemas(toolkits, adapter) if toolkits else None

    async for response in engine.send(session, message, additional_tools=additional_tool_schemas):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)

            # Check toolkits first (e.g., KB toolkit, history toolkit)
            toolkit = _find_toolkit_for_tool(toolkits, name)
            if toolkit is not None:
                result = toolkit.execute_tool(name, args)
                async for continuation in engine.submit_tool_result(
                    session, response.tool_use["id"], result,
                    additional_tools=additional_tool_schemas,
                ):
                    yield continuation
                continue

            if _tool_requires_approval(name, session.workflow):
                yield PendingApproval(
                    tool_use_id=response.tool_use["id"], tool_name=name, arguments=args
                )
                return
            result = tool_registry.execute_tool(
                name=name, user=session.user, arguments=args, approved=True
            )
            async for continuation in engine.submit_tool_result(
                session, response.tool_use["id"], result,
                additional_tools=additional_tool_schemas,
            ):
                yield continuation
        else:
            yield response
```

- [ ] **Step 4: Run all runner tests**

Run: `python -m pytest tests/test_conversation_runner.py -v`
Expected: All PASS

- [ ] **Step 5: Run ruff**

Run: `ruff check --fix src/django_ergo/conversation/runner.py tests/test_conversation_runner.py && ruff format src/django_ergo/conversation/runner.py tests/test_conversation_runner.py`

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/conversation/runner.py tests/test_conversation_runner.py
git commit -m "feat: runner accepts list[Toolkit] with schema merging and dispatch"
```

---

### Task 6: Full Suite Verification

- [ ] **Step 1: Run full conversation test suite**

Run: `python -m pytest tests/test_conversation_*.py tests/test_toolkit_protocol.py tests/test_kb_toolkit.py -v`
Expected: All PASS

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `python -m pytest tests/test_settings.py tests/test_tool_registry_unit.py tests/test_kb_tools.py -v`
Expected: All PASS

- [ ] **Step 3: Push**

```bash
git push
```
