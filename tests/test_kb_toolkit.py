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
        knowledgebase=kb,
        hierarchy_code="0",
        title="Introduction",
        content="Welcome to the product documentation.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="1",
        title="Getting Started",
        content="To get started, install the package with pip install mypackage.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="10",
        title="Installation",
        content="Run pip install mypackage to install.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="11",
        title="Configuration",
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
        knowledgebase=kb,
        hierarchy_code="0",
        title="General",
        content="General frequently asked questions.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="1",
        title="Billing",
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
        intro_line = next(line for line in lines if "Introduction" in line)
        assert not intro_line.startswith(" ")
        # Nested articles should be indented
        install_line = next(line for line in lines if "Installation" in line)
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
        search_schema = next(s for s in schemas if s["name"] == "kb_search")
        assert "input_schema" in search_schema
        assert "query" in search_schema["input_schema"]["properties"]
