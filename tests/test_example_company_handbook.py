"""Example 2: Company Handbook — build a KB from team conversation history.

This integration test demonstrates:
1. Multiple team members have conversations about company topics
2. Admin reviews conversations via ChatWithHistoryToolkit
3. Absorption agent builds a structured handbook KB
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession
from django_ergo.kb_pipelines import absorb_conversation
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


def _create_conversation(user, messages):
    """Helper to create a conversation with message pairs."""
    session = ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="completed",
    )
    for seq, (role, text) in enumerate(messages):
        msg = ClaudeMessage.objects.create(session=session, role=role, sequence=seq)
        ClaudeContentBlock.objects.create(
            message=msg,
            block_type="text",
            sequence=0,
            text=text,
        )
    return session


@pytest.fixture()
def admin():
    return User.objects.create_user(username="admin", password="testpass")


@pytest.fixture()
def dev_alice():
    return User.objects.create_user(username="dev_alice", password="testpass")


@pytest.fixture()
def dev_bob():
    return User.objects.create_user(username="dev_bob", password="testpass")


@pytest.fixture()
def handbook_kb(admin):
    return Knowledgebase.objects.create(
        name="Company Handbook",
        description="Company processes, architecture decisions, and team practices",
        owner_id=str(admin.id),
    )


@pytest.fixture()
def alice_arch_chat(dev_alice):
    """Alice discusses architecture decisions."""
    return _create_conversation(
        dev_alice,
        [
            (
                "user",
                "We decided to use PostgreSQL with pgvector for all our vector search needs.",
            ),
            (
                "assistant",
                "Good choice — pgvector integrates natively with Django ORM.",
            ),
            (
                "user",
                "Yeah, and we're using HNSW indexes for anything over 10k vectors.",
            ),
            ("assistant", "HNSW gives good recall/speed tradeoff at that scale."),
        ],
    )


@pytest.fixture()
def bob_deploy_chat(dev_bob):
    """Bob discusses deployment process."""
    return _create_conversation(
        dev_bob,
        [
            (
                "user",
                "Our deploy process is: PR review, merge to main, CI runs, auto-deploy to staging, manual promote to prod.",
            ),
            ("assistant", "That's a solid pipeline. How long does CI take?"),
            ("user", "About 8 minutes. We run pytest and ruff in parallel."),
            ("assistant", "Fast CI is important for developer velocity."),
        ],
    )


@pytest.fixture()
def bob_onboarding_chat(dev_bob):
    """Bob discusses onboarding."""
    return _create_conversation(
        dev_bob,
        [
            (
                "user",
                "New devs should clone the repo, run make env, then make migrate. Takes about 5 minutes.",
            ),
            ("assistant", "Simple onboarding is a great sign of a healthy codebase."),
        ],
    )


def _make_mock_engine():
    engine = MagicMock()
    engine.engine_type = "claude"
    engine.get_tool_adapter.return_value = MagicMock()
    engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
        "name": "mock"
    }
    return engine


class TestCompanyHandbookSetup:
    """Verify test fixtures create the right conversations."""

    def test_three_conversations_created(
        self, alice_arch_chat, bob_deploy_chat, bob_onboarding_chat
    ):
        assert ConversationSession.objects.filter(status="completed").count() == 3

    def test_history_toolkit_sees_all(
        self, alice_arch_chat, bob_deploy_chat, bob_onboarding_chat
    ):
        toolkit = ChatWithHistoryToolkit(
            sessions=[alice_arch_chat, bob_deploy_chat, bob_onboarding_chat],
        )
        overview = toolkit.render_overview()
        assert "PostgreSQL" in overview or "pgvector" in overview
        assert "deploy" in overview.lower()
        assert "clone" in overview.lower() or "onboarding" in overview.lower()


class TestHandbookAbsorption:
    """Test absorbing team conversations into a handbook KB."""

    @patch("django_ergo.kb_pipelines.run_workflow_task")
    def test_absorb_architecture_chat(
        self, mock_run_task, alice_arch_chat, handbook_kb
    ):
        """Architecture decisions get absorbed into the handbook."""

        async def fake_run_task(*args, **kwargs):
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    tk.execute_tool(
                        "kb_suggest_create",
                        {
                            "title": "Database Architecture",
                            "content": "We use PostgreSQL with pgvector for vector search. HNSW indexes are used for collections over 10k vectors.",
                            "hierarchy_code": "0",
                        },
                    )

        mock_run_task.side_effect = fake_run_task

        suggestions = async_to_sync(absorb_conversation)(
            alice_arch_chat,
            handbook_kb,
            _make_mock_engine(),
        )

        assert len(suggestions.get_suggestions()) == 1
        assert suggestions.get_suggestions()[0]["title"] == "Database Architecture"

    @patch("django_ergo.kb_pipelines.run_workflow_task")
    def test_build_full_handbook(  # noqa: PLR0913
        self,
        mock_run_task,
        alice_arch_chat,
        bob_deploy_chat,
        bob_onboarding_chat,
        handbook_kb,
    ):
        """Absorb all conversations and build a structured handbook."""
        call_count = {"n": 0}

        async def fake_run_task(*args, **kwargs):
            call_count["n"] += 1
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    if call_count["n"] == 1:
                        tk.execute_tool(
                            "kb_suggest_create",
                            {
                                "title": "Database Architecture",
                                "content": "PostgreSQL with pgvector. HNSW indexes for 10k+ vectors.",
                                "hierarchy_code": "0",
                            },
                        )
                    elif call_count["n"] == 2:
                        tk.execute_tool(
                            "kb_suggest_create",
                            {
                                "title": "Deployment Process",
                                "content": "PR review → merge to main → CI (8 min, pytest + ruff) → staging → manual prod promote.",
                                "hierarchy_code": "1",
                            },
                        )
                    else:
                        tk.execute_tool(
                            "kb_suggest_create",
                            {
                                "title": "Developer Onboarding",
                                "content": "Clone repo, run make env, then make migrate. Takes about 5 minutes.",
                                "hierarchy_code": "2",
                            },
                        )

        mock_run_task.side_effect = fake_run_task
        engine = _make_mock_engine()

        # Absorb each conversation
        for chat in [alice_arch_chat, bob_deploy_chat, bob_onboarding_chat]:
            suggestions = async_to_sync(absorb_conversation)(chat, handbook_kb, engine)
            suggestions.apply_suggestions()

        # Verify the handbook
        assert handbook_kb.articles.count() == 3
        titles = list(
            handbook_kb.articles.order_by("hierarchy_code").values_list(
                "title", flat=True
            )
        )
        assert titles == [
            "Database Architecture",
            "Deployment Process",
            "Developer Onboarding",
        ]

    def test_handbook_is_browseable(self, handbook_kb):
        """After absorption, the handbook is fully browseable via KBToolkit."""
        Article.objects.create(
            knowledgebase=handbook_kb,
            hierarchy_code="0",
            title="Database Architecture",
            content="PostgreSQL with pgvector.",
        )
        Article.objects.create(
            knowledgebase=handbook_kb,
            hierarchy_code="1",
            title="Deployment Process",
            content="PR review, CI, staging, prod.",
        )
        Article.objects.create(
            knowledgebase=handbook_kb,
            hierarchy_code="2",
            title="Developer Onboarding",
            content="Clone, make env, make migrate.",
        )

        toolkit = KBToolkit(knowledgebases=[handbook_kb])

        # Overview shows handbook structure
        overview = toolkit.render_overview()
        assert "Company Handbook" in overview
        assert "Database Architecture" in overview
        assert "Deployment Process" in overview
        assert "Developer Onboarding" in overview

        # TOC is structured
        toc = toolkit.execute_tool(
            "kb_table_of_contents", {"kb_name": "Company Handbook"}
        )
        assert "0: Database Architecture" in toc
        assert "1: Deployment Process" in toc
        assert "2: Developer Onboarding" in toc

        # Articles are readable
        article = toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": "Company Handbook", "hierarchy_code": "1"},
        )
        assert "PR review" in article


class TestHandbookWithSuggestionReview:
    """Test the suggest-then-review pattern for handbook curation."""

    @patch("django_ergo.kb_pipelines.run_workflow_task")
    def test_selective_apply(
        self, mock_run_task, alice_arch_chat, bob_deploy_chat, handbook_kb
    ):
        """Admin can review suggestions and selectively apply them."""

        async def fake_run_task(*args, **kwargs):
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    tk.execute_tool(
                        "kb_suggest_create",
                        {
                            "title": "Database Architecture",
                            "content": "PostgreSQL with pgvector.",
                            "hierarchy_code": "0",
                        },
                    )
                    tk.execute_tool(
                        "kb_suggest_create",
                        {
                            "title": "Internal Joke",
                            "content": "Bob always spills coffee on deploy day.",
                            "hierarchy_code": "1",
                        },
                    )

        mock_run_task.side_effect = fake_run_task

        suggestions = async_to_sync(absorb_conversation)(
            alice_arch_chat,
            handbook_kb,
            _make_mock_engine(),
        )

        # Admin reviews — keep architecture, reject joke
        all_suggestions = suggestions.get_suggestions()
        assert len(all_suggestions) == 2

        # Only apply the first suggestion (architecture)
        results = suggestions.apply_suggestions(indices=[0])
        assert len(results) == 1
        assert "Created" in results[0]

        # Handbook has architecture but not the joke
        assert handbook_kb.articles.count() == 1
        assert handbook_kb.articles.first().title == "Database Architecture"

        # Remaining suggestion is still available
        remaining = suggestions.get_suggestions()
        assert len(remaining) == 1
        assert remaining[0]["title"] == "Internal Joke"
