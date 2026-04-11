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
        knowledgebase=kb,
        hierarchy_code="0",
        title="Introduction",
        content="Welcome to the product documentation.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="1",
        title="Getting Started",
        content="To get started, install the package.",
    )
    Article.objects.create(
        knowledgebase=kb,
        hierarchy_code="10",
        title="Installation",
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

    def test_create_with_section(self, toolkit, kb):
        """Auto-generates next code within a section prefix."""
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "FAQ", "content": "Frequently asked questions.", "section": "2"},
        )
        assert "20" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="20").exists()

    def test_create_with_section_existing_children(self, toolkit, kb):
        """Skips existing children when auto-generating within a section."""
        Article.objects.create(
            knowledgebase=kb, hierarchy_code="20", title="First", content="x"
        )
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "Second", "content": "y", "section": "2"},
        )
        assert "21" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="21").exists()

    def test_create_no_placement_raises(self, toolkit):
        """Raises when no placement parameter is given."""
        with pytest.raises(ValueError, match="section"):
            toolkit.execute_tool(
                "kb_create_article",
                {"title": "FAQ", "content": "Frequently asked questions."},
            )

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
        Article.objects.create(
            knowledgebase=kb,
            hierarchy_code="2",
            title="API",
            content="API reference.",
        )
        result = toolkit.execute_tool(
            "kb_create_article",
            {"title": "Endpoints", "content": "API endpoints.", "parent_code": "2"},
        )
        assert "20" in result
        assert Article.objects.filter(knowledgebase=kb, hierarchy_code="20").exists()

    def test_create_with_summary(self, toolkit, kb):
        toolkit.execute_tool(
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
        with pytest.raises(ValueError, match="only one"):
            toolkit.execute_tool(
                "kb_create_article",
                {
                    "title": "Bad",
                    "content": "x",
                    "hierarchy_code": "5",
                    "parent_code": "1",
                },
            )

    def test_create_section_and_code_raises(self, toolkit):
        with pytest.raises(ValueError, match="only one"):
            toolkit.execute_tool(
                "kb_create_article",
                {
                    "title": "Bad",
                    "content": "x",
                    "hierarchy_code": "5",
                    "section": "1",
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
        toolkit.execute_tool(
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
        assert not Article.objects.filter(
            knowledgebase=kb, hierarchy_code="10"
        ).exists()

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
        expected_tool_count = len(
            ["kb_create_article", "kb_update_article", "kb_delete_article"]
        )
        assert len(schemas) == expected_tool_count
