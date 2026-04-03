"""Integration test: static expert system example.

Exercises the full KB toolkit stack end-to-end:
- Create a KB with articles
- Use KBToolkit to search/browse
- Use KBWriteToolkit to add content
- Verify ConversationKBUsage tracking
"""

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import ConversationKBUsage
from django_ergo.conversation.models import ConversationSession
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.kb_write_toolkit import KBWriteToolkit
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

TOP_LEVEL_COUNT = 4
CHILDREN_OF_2_COUNT = 2
MULTI_TOOLKIT_USAGE_COUNT = 2

User = get_user_model()
pytestmark = pytest.mark.django_db

LAWN_CARE_ARTICLES = [
    (
        "0",
        "Mowing",
        "Mow your lawn regularly at a height of 3 inches. Never cut more than one-third of the grass blade at once.",
    ),
    (
        "1",
        "Watering",
        "Water deeply but infrequently. Aim for 1 inch of water per week, preferably in the early morning.",
    ),
    (
        "2",
        "Fertilizing",
        "Apply fertilizer in spring and fall. Use a slow-release nitrogen fertilizer for best results.",
    ),
    (
        "20",
        "Spring Fertilizing",
        "In spring, apply fertilizer when the grass starts actively growing, usually when soil temps reach 55F.",
    ),
    (
        "21",
        "Fall Fertilizing",
        "Fall fertilization is the most important application. Apply 4-6 weeks before the first expected frost.",
    ),
    (
        "3",
        "Weed Control",
        "Prevent weeds with a thick, healthy lawn. Apply pre-emergent herbicide in early spring before crabgrass germinates.",
    ),
]


@pytest.fixture()
def user():
    return User.objects.create_user(username="gardener", password="testpass")


@pytest.fixture()
def lawn_kb(user):
    kb = Knowledgebase.objects.create(
        name="Lawn Care Expert",
        description="Complete guide to lawn care and maintenance",
        owner_id=str(user.id),
    )
    for code, title, content in LAWN_CARE_ARTICLES:
        Article.objects.create(
            knowledgebase=kb,
            hierarchy_code=code,
            title=title,
            content=content,
        )
    return kb


@pytest.fixture()
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
    )


class TestExpertSystemSetup:
    def test_kb_has_articles(self, lawn_kb):
        assert lawn_kb.articles.count() == len(LAWN_CARE_ARTICLES)

    def test_hierarchy_structure(self, lawn_kb):
        top_level = lawn_kb.articles.filter(hierarchy_code__regex=r"^.$")
        assert top_level.count() == TOP_LEVEL_COUNT  # 0, 1, 2, 3
        children_of_2 = lawn_kb.articles.filter(
            hierarchy_code__startswith="2", hierarchy_code__regex=r"^..$"
        )
        assert children_of_2.count() == CHILDREN_OF_2_COUNT  # 20, 21


class TestReadToolkit:
    def test_overview_shows_top_level(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        overview = toolkit.render_overview()
        assert "Lawn Care Expert" in overview
        assert "Mowing" in overview
        assert "Watering" in overview
        assert "Fertilizing" in overview
        assert "Weed Control" in overview
        # Nested articles should NOT be in overview
        assert "Spring Fertilizing" not in overview

    def test_table_of_contents(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool(
            "kb_table_of_contents", {"kb_name": "Lawn Care Expert"}
        )
        assert "0: Mowing" in result
        assert "1: Watering" in result
        assert "2: Fertilizing" in result
        # Nested articles should be indented
        assert "  20: Spring Fertilizing" in result
        assert "  21: Fall Fertilizing" in result
        assert "3: Weed Control" in result

    def test_get_article(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": "Lawn Care Expert", "hierarchy_code": "0"},
        )
        assert "Mowing" in result
        assert "3 inches" in result

    def test_search(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool(
            "kb_search",
            {"query": "when should I water my lawn", "kb_name": "Lawn Care Expert"},
        )
        # Should return results or graceful error (no OpenAI key in tests)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_list_kbs(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool("kb_list", {})
        assert "Lawn Care Expert" in result
        assert "6" in result  # 6 articles


class TestWriteToolkit:
    def test_add_article_to_expert(self, lawn_kb):
        toolkit = KBWriteToolkit(knowledgebase=lawn_kb)
        result = toolkit.execute_tool(
            "kb_create_article",
            {
                "title": "Aeration",
                "content": "Aerate your lawn in fall to reduce soil compaction and improve root growth.",
            },
        )
        assert "Aeration" in result
        # Should get next top-level code: "4"
        assert Article.objects.filter(
            knowledgebase=lawn_kb, hierarchy_code="4"
        ).exists()

    def test_add_sub_article(self, lawn_kb):
        toolkit = KBWriteToolkit(knowledgebase=lawn_kb)
        result = toolkit.execute_tool(
            "kb_create_article",
            {
                "title": "Winter Fertilizing",
                "content": "In cold climates, skip winter fertilization entirely.",
                "parent_code": "2",
            },
        )
        assert "22" in result
        assert Article.objects.filter(
            knowledgebase=lawn_kb, hierarchy_code="22"
        ).exists()


class TestUsageTracking:
    def test_read_toolkit_reports_bindings(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        bindings = toolkit.get_bound_knowledgebases()
        assert len(bindings) == 1
        assert bindings[0] == (lawn_kb, "read")

    def test_write_toolkit_reports_bindings(self, lawn_kb):
        toolkit = KBWriteToolkit(knowledgebase=lawn_kb)
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == [(lawn_kb, "write")]

    def test_usage_recorded_in_db(self, session, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        for kb, mode in toolkit.get_bound_knowledgebases():
            ConversationKBUsage.objects.get_or_create(
                session=session,
                knowledgebase=kb,
                mode=mode,
            )

        usages = ConversationKBUsage.objects.filter(session=session)
        assert usages.count() == 1
        assert usages.first().mode == "read"
        assert usages.first().knowledgebase == lawn_kb

    def test_multi_toolkit_usage(self, session, lawn_kb):
        read_toolkit = KBToolkit(knowledgebases=[lawn_kb])
        write_toolkit = KBWriteToolkit(knowledgebase=lawn_kb)

        for toolkit in [read_toolkit, write_toolkit]:
            for kb, mode in toolkit.get_bound_knowledgebases():
                ConversationKBUsage.objects.get_or_create(
                    session=session,
                    knowledgebase=kb,
                    mode=mode,
                )

        usages = ConversationKBUsage.objects.filter(session=session)
        assert usages.count() == MULTI_TOOLKIT_USAGE_COUNT
        modes = set(usages.values_list("mode", flat=True))
        assert modes == {"read", "write"}
