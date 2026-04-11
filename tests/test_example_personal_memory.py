"""Example 1: Personal Memory — AI that remembers user preferences.

This integration test demonstrates the absorption loop:
1. User has conversations about their preferences
2. absorb_conversation() extracts knowledge into a personal KB
3. Future conversations have KB context via KBToolkit
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession
from django_ergo.kb_pipelines import absorb_conversation
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def alice():
    return User.objects.create_user(username="alice", password="testpass")


@pytest.fixture()
def alice_kb(alice):
    return Knowledgebase.objects.create(
        name="Alice's Memory",
        description="Facts about Alice's preferences, habits, and ongoing projects",
        owner_id=str(alice.id),
    )


@pytest.fixture()
def alice_chat_1(alice):
    """Alice's first conversation — mentions deployment preferences."""
    session = ConversationSession.objects.create(
        user=alice,
        engine_type="claude",
        transport_type="api",
        status="completed",
    )
    m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0,
        block_type="text",
        sequence=0,
        text="I always deploy in the morning before standup. I find it's the least risky time.",
    )
    m1 = ClaudeMessage.objects.create(
        session=session, role="assistant", sequence=1, stop_reason="end_turn"
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="text",
        sequence=0,
        text="Morning deployments make sense — fewer users online, easier to catch issues.",
    )
    return session


@pytest.fixture()
def alice_chat_2(alice):
    """Alice's second conversation — mentions testing preferences."""
    session = ConversationSession.objects.create(
        user=alice,
        engine_type="claude",
        transport_type="api",
        status="completed",
    )
    m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0,
        block_type="text",
        sequence=0,
        text="I use pytest with the --tb=short flag. I also like to run coverage reports.",
    )
    m1 = ClaudeMessage.objects.create(
        session=session, role="assistant", sequence=1, stop_reason="end_turn"
    )
    ClaudeContentBlock.objects.create(
        message=m1,
        block_type="text",
        sequence=0,
        text="Good testing practices! pytest --tb=short keeps output clean.",
    )
    return session


def _make_mock_engine():
    engine = MagicMock()
    engine.engine_type = "claude"
    engine.get_tool_adapter.return_value = MagicMock()
    engine.get_tool_adapter.return_value.to_engine_schema.return_value = {
        "name": "mock"
    }
    return engine


class TestPersonalMemoryAbsorption:
    """Test the full absorption loop: conversation → absorb → KB."""

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_absorb_first_conversation(self, mock_runner, alice_chat_1, alice_kb):
        """Absorption agent extracts deployment preferences from chat 1."""

        async def fake_runner(*args, **kwargs):
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    tk.execute_tool(
                        "kb_suggest_create",
                        {
                            "title": "Deployment Preferences",
                            "content": "Alice prefers morning deployments before standup. She considers this the least risky time due to fewer users online.",
                        },
                    )
            yield EngineResponse(event_type="done", text="Absorbed.")

        mock_runner.side_effect = fake_runner

        result = async_to_sync(absorb_conversation)(
            alice_chat_1,
            alice_kb,
            _make_mock_engine(),
        )

        suggestions = result.get_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["title"] == "Deployment Preferences"
        assert "morning" in suggestions[0]["content"]

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_absorb_and_apply(self, mock_runner, alice_chat_1, alice_kb):
        """Absorb and apply — KB now has the article."""

        async def fake_runner(*args, **kwargs):
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    tk.execute_tool(
                        "kb_suggest_create",
                        {
                            "title": "Deployment Preferences",
                            "content": "Alice prefers morning deployments.",
                            "hierarchy_code": "0",
                        },
                    )
            yield EngineResponse(event_type="done", text="Absorbed.")

        mock_runner.side_effect = fake_runner

        suggestions = async_to_sync(absorb_conversation)(
            alice_chat_1,
            alice_kb,
            _make_mock_engine(),
        )
        suggestions.apply_suggestions()

        assert alice_kb.articles.count() == 1
        article = alice_kb.articles.first()
        assert article.title == "Deployment Preferences"

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_multi_conversation_absorption(
        self, mock_runner, alice_chat_1, alice_chat_2, alice_kb
    ):
        """Absorb two conversations — KB accumulates knowledge from both."""
        call_count = {"n": 0}

        async def fake_runner(*args, **kwargs):
            call_count["n"] += 1
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    if call_count["n"] == 1:
                        tk.execute_tool(
                            "kb_suggest_create",
                            {
                                "title": "Deployment Preferences",
                                "content": "Alice prefers morning deployments.",
                                "hierarchy_code": "0",
                            },
                        )
                    else:
                        tk.execute_tool(
                            "kb_suggest_create",
                            {
                                "title": "Testing Preferences",
                                "content": "Alice uses pytest with --tb=short and runs coverage reports.",
                                "hierarchy_code": "1",
                            },
                        )
            yield EngineResponse(event_type="done", text="Absorbed.")

        mock_runner.side_effect = fake_runner
        engine = _make_mock_engine()

        # Absorb chat 1
        s1 = async_to_sync(absorb_conversation)(alice_chat_1, alice_kb, engine)
        s1.apply_suggestions()

        # Absorb chat 2
        s2 = async_to_sync(absorb_conversation)(alice_chat_2, alice_kb, engine)
        s2.apply_suggestions()

        assert alice_kb.articles.count() == 2
        titles = set(alice_kb.articles.values_list("title", flat=True))
        assert titles == {"Deployment Preferences", "Testing Preferences"}

    def test_kb_toolkit_reads_absorbed_knowledge(self, alice_kb):
        """After absorption, KBToolkit can read the absorbed articles."""
        # Manually create articles (simulating post-absorption state)
        Article.objects.create(
            knowledgebase=alice_kb,
            hierarchy_code="0",
            title="Deployment Preferences",
            content="Alice prefers morning deployments before standup.",
        )
        Article.objects.create(
            knowledgebase=alice_kb,
            hierarchy_code="1",
            title="Testing Preferences",
            content="Alice uses pytest with --tb=short flag.",
        )

        toolkit = KBToolkit(knowledgebases=[alice_kb])

        # Agent can see what's in the KB
        overview = toolkit.render_overview()
        assert "Deployment Preferences" in overview
        assert "Testing Preferences" in overview

        # Agent can read specific articles
        result = toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": "Alice's Memory", "hierarchy_code": "0"},
        )
        assert "morning deployments" in result

        # Agent can list the KB
        result = toolkit.execute_tool("kb_list", {})
        assert "Alice's Memory" in result
        assert "2" in result  # 2 articles


class TestPersonalMemoryIsolation:
    """Test that personal KBs are isolated between users."""

    def test_separate_user_kbs(self, alice, alice_kb):
        bob = User.objects.create_user(username="bob", password="testpass")
        bob_kb = Knowledgebase.objects.create(
            name="Bob's Memory",
            description="Facts about Bob",
            owner_id=str(bob.id),
        )
        Article.objects.create(
            knowledgebase=alice_kb,
            hierarchy_code="0",
            title="Alice's Pref",
            content="Alice likes morning deploys.",
        )
        Article.objects.create(
            knowledgebase=bob_kb,
            hierarchy_code="0",
            title="Bob's Pref",
            content="Bob likes evening deploys.",
        )

        # Alice's toolkit only sees Alice's KB
        alice_toolkit = KBToolkit(knowledgebases=[alice_kb])
        result = alice_toolkit.execute_tool("kb_list", {})
        assert "Alice's Memory" in result
        assert "Bob's Memory" not in result

        # Bob's toolkit only sees Bob's KB
        bob_toolkit = KBToolkit(knowledgebases=[bob_kb])
        result = bob_toolkit.execute_tool("kb_list", {})
        assert "Bob's Memory" in result
        assert "Alice's Memory" not in result
