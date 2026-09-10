from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import RequestFactory

from django_ergo.fields import vector_search
from django_ergo.kb_strategy_toolkit import KBStrategyToolkit
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.kb_tools import get_article_by_hierarchy
from django_ergo.kb_tools import get_kb_table_of_contents
from django_ergo.kb_tools import list_user_knowledgebases
from django_ergo.kb_tools import search_garden_kb
from django_ergo.kb_tools import search_user_kb
from django_ergo.models import Article
from django_ergo.models import Knowledgebase
from tests.example_app.api import get_article
from tests.example_app.api import list_articles

pytestmark = pytest.mark.django_db


def test_virtual_strategy_write_and_gap_analysis_hide_archived(legacy_kbs):
    _owner, owned, visible, archived, _foreign = legacy_kbs
    owned.articles.filter(pk=visible.pk).update(hierarchy_code="AA01")
    owned.articles.filter(pk=archived.pk).update(hierarchy_code="AA02")
    toolkit = KBStrategyToolkit(owned)
    toolkit.execute_tool("kb_update_strategy", {"strategy": "Reviewed organization"})
    toolkit.execute_tool(
        "kb_propose_tree",
        {
            "prefix": "AA",
            "title": "Pumps",
            "description": "Pump instructions",
            "entries": ["Reset"],
        },
    )
    assert "Reviewed organization" in toolkit.execute_tool("kb_get_strategy", {})
    assert "Tree #AA: 1 articles" in toolkit.execute_tool("kb_get_tree_status", {})


@pytest.fixture
def legacy_kbs():
    owner = get_user_model().objects.create_user(username="owner")
    other = get_user_model().objects.create_user(username="other")
    owned = Knowledgebase.objects.create(name="Garden", owner_id=str(owner.pk))
    private = Knowledgebase.objects.create(
        name="Private Garden", owner_id=str(other.pk)
    )
    with patch("django_ergo.fields.generate_embedding", return_value=[1.0] * 1536):
        visible = Article.objects.create(
            knowledgebase=owned,
            hierarchy_code="A",
            title="Visible",
            content="garden pump",
            summary="garden",
        )
        archived = Article.objects.create(
            knowledgebase=owned,
            hierarchy_code="B",
            title="Hidden archived",
            content="garden pump",
            summary="garden",
            status="archived",
        )
        foreign = Article.objects.create(
            knowledgebase=private,
            hierarchy_code="A",
            title="Hidden other owner",
            content="garden pump",
            summary="garden",
        )
    return owner, owned, visible, archived, foreign


@pytest.mark.parametrize(
    "method",
    [
        "semantic_search_content",
        "semantic_search_summary",
        "multi_field_semantic_search",
        "hybrid_search",
    ],
)
def test_semantic_queries_preserve_scope_and_lifecycle(legacy_kbs, method):
    _owner, owned, visible, _archived, _foreign = legacy_kbs
    with patch("django_ergo.models.generate_embedding", return_value=[1.0] * 1536):
        results = getattr(Article.objects.filter(knowledgebase=owned), method)("garden")
        assert [article.pk for article in results] == [visible.pk]


@pytest.mark.parametrize(
    "method",
    ["vector_search_content", "vector_search_summary", "multi_field_vector_search"],
)
def test_vector_queries_preserve_scope_and_lifecycle(legacy_kbs, method):
    _owner, owned, visible, _archived, _foreign = legacy_kbs
    results = getattr(Article.objects.filter(knowledgebase=owned), method)([1.0] * 1536)
    assert [article.pk for article in results] == [visible.pk]


def test_garden_search_cannot_select_global_kbs(legacy_kbs):
    owner, _owned, visible, _archived, _foreign = legacy_kbs
    with patch("django_ergo.models.generate_embedding", return_value=[1.0] * 1536):
        assert [item["id"] for item in search_garden_kb(owner, "garden")] == [
            str(visible.pk)
        ]
        assert [item["id"] for item in search_user_kb(owner, "garden")] == [
            str(visible.pk)
        ]


def test_archived_pages_are_excluded_from_legacy_read_tools(legacy_kbs):
    owner, owned, visible, archived, _foreign = legacy_kbs
    toolkit = KBToolkit([owned])
    assert "Hidden" not in toolkit.render_overview()
    assert "Articles: 1" in toolkit.render_overview()
    assert "Hidden" not in toolkit.execute_tool(
        "kb_table_of_contents", {"kb_name": owned.name}
    )
    assert "Hidden" not in owned.get_table_of_contents()
    assert toolkit.execute_tool("kb_list", {}).count("1 articles") == 1
    with pytest.raises(ValueError, match="not found"):
        toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": owned.name, "hierarchy_code": archived.hierarchy_code},
        )
    assert "error" in get_article_by_hierarchy(
        owner, owned.name, archived.hierarchy_code
    )
    assert list_user_knowledgebases(owner)[0]["article_count"] == 1
    assert [
        item["id"]
        for item in get_kb_table_of_contents(owner, owned.name)["table_of_contents"]
    ] == [str(visible.pk)]
    assert [item["id"] for item in owned.articles.to_prefetch_results()] == [
        str(visible.pk)
    ]


def test_low_level_model_search_does_not_return_archived_content(legacy_kbs):
    _owner, _owned, _visible, archived, _foreign = legacy_kbs
    assert archived.pk not in [
        article.pk
        for article in vector_search(Article, "content_embedding", [1.0] * 1536)
    ]


def test_prefetch_results_accepts_an_already_limited_search(legacy_kbs):
    _owner, owned, visible, _archived, _foreign = legacy_kbs
    results = owned.articles.vector_search_content([1.0] * 1536)
    assert [item["id"] for item in results.to_prefetch_results()] == [str(visible.pk)]


def test_example_api_read_paths_hide_archived_content(legacy_kbs):
    owner, _owned, visible, archived, _foreign = legacy_kbs
    request = RequestFactory().get("/")
    request.auth = owner
    response = list_articles(request, knowledgebase=None, hierarchy_prefix=None)
    assert [article.pk for article in response["items"]] == [visible.pk]
    with pytest.raises(Http404):
        get_article(request, archived.pk)
