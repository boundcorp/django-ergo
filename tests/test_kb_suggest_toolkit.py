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
        knowledgebase=kb,
        hierarchy_code="0",
        title="Introduction",
        content="Welcome to the docs.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="1",
        title="Getting Started",
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

    def test_records_with_section(self, toolkit):
        result = toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "Darrow", "content": "Character.", "section": "1"},
        )
        assert "section 1" in result
        suggestions = toolkit.get_suggestions()
        assert suggestions[0]["section"] == "1"

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
        toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "New", "content": "New article.", "hierarchy_code": "3"},
        )
        toolkit.execute_tool(
            "kb_suggest_update", {"hierarchy_code": "0", "title": "Updated Intro"}
        )

        results = toolkit.apply_suggestions()
        assert len(results) == 2
        assert "Created" in results[0]
        assert "Updated" in results[1]

        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="3").exists()
        article = Article.objects.get(knowledgebase=kb, hierarchy_code="0")
        assert article.title == "Updated Intro"

        assert len(toolkit.get_suggestions()) == 0

    def test_apply_specific_indices(self, toolkit, kb):
        toolkit.execute_tool(
            "kb_suggest_create", {"title": "A", "content": "a", "hierarchy_code": "3"}
        )
        toolkit.execute_tool(
            "kb_suggest_create", {"title": "B", "content": "b", "hierarchy_code": "4"}
        )
        toolkit.execute_tool("kb_suggest_delete", {"hierarchy_code": "1"})

        results = toolkit.apply_suggestions(indices=[0, 2])
        assert len(results) == 2
        assert "Created" in results[0]
        assert "Deleted" in results[1]

        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="3").exists()
        assert not Article.objects.filter(knowledgebase=kb, hierarchy_code="4").exists()
        assert not Article.objects.filter(knowledgebase=kb, hierarchy_code="1").exists()

        remaining = toolkit.get_suggestions()
        assert len(remaining) == 1
        assert remaining[0]["title"] == "B"

    def test_apply_continues_on_error(self, toolkit, kb):
        toolkit.execute_tool("kb_suggest_delete", {"hierarchy_code": "NONEXISTENT"})
        toolkit.execute_tool(
            "kb_suggest_create",
            {"title": "Good", "content": "ok", "hierarchy_code": "5"},
        )

        results = toolkit.apply_suggestions()
        assert len(results) == 2
        assert "not found" in results[0].lower() or "error" in results[0].lower()
        assert "Created" in results[1]
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
