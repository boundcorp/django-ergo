"""Tests for ConversationKBUsage model and toolkit integration."""

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.models import ConversationKBUsage
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import KBUsageMode
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def kb(user):
    return Knowledgebase.objects.create(
        name="Test KB",
        description="For testing",
        owner_id=str(user.id),
    )


@pytest.fixture()
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
    )


class TestKBUsageMode:
    def test_choices(self):
        assert KBUsageMode.READ == "read"
        assert KBUsageMode.WRITE == "write"
        assert KBUsageMode.SUGGEST == "suggest"


class TestConversationKBUsageModel:
    def test_create_usage(self, session, kb):
        usage = ConversationKBUsage.objects.create(
            session=session,
            knowledgebase=kb,
            mode=KBUsageMode.READ,
        )
        assert usage.session == session
        assert usage.knowledgebase == kb
        assert usage.mode == "read"

    def test_unique_together(self, session, kb):
        from django.db import IntegrityError

        ConversationKBUsage.objects.create(
            session=session,
            knowledgebase=kb,
            mode=KBUsageMode.READ,
        )
        with pytest.raises(IntegrityError):
            ConversationKBUsage.objects.create(
                session=session,
                knowledgebase=kb,
                mode=KBUsageMode.READ,
            )

    def test_same_kb_different_modes(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session,
            knowledgebase=kb,
            mode=KBUsageMode.READ,
        )
        ConversationKBUsage.objects.create(
            session=session,
            knowledgebase=kb,
            mode=KBUsageMode.WRITE,
        )
        assert ConversationKBUsage.objects.filter(session=session).count() == 2  # noqa: PLR2004

    def test_session_reverse_relation(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session,
            knowledgebase=kb,
            mode=KBUsageMode.READ,
        )
        assert session.kb_usages.count() == 1

    def test_kb_reverse_relation(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session,
            knowledgebase=kb,
            mode=KBUsageMode.READ,
        )
        assert kb.conversation_usages.count() == 1


class TestToolkitGetBoundKnowledgebases:
    def test_kb_toolkit_returns_read(self, kb):
        from django_ergo.kb_toolkit import KBToolkit

        toolkit = KBToolkit(knowledgebases=[kb])
        bindings = toolkit.get_bound_knowledgebases()
        assert len(bindings) == 1
        assert bindings[0] == (kb, "read")

    def test_kb_toolkit_multiple_kbs(self, user):
        from django_ergo.kb_toolkit import KBToolkit

        kb2 = Knowledgebase.objects.create(
            name="KB 2",
            description="Second",
            owner_id=str(user.id),
        )
        kb3 = Knowledgebase.objects.create(
            name="KB 3",
            description="Third",
            owner_id=str(user.id),
        )
        toolkit = KBToolkit(knowledgebases=[kb2, kb3])
        bindings = toolkit.get_bound_knowledgebases()
        assert len(bindings) == 2  # noqa: PLR2004
        assert all(mode == "read" for _, mode in bindings)

    def test_kb_write_toolkit_returns_write(self, kb):
        from django_ergo.kb_write_toolkit import KBWriteToolkit

        toolkit = KBWriteToolkit(knowledgebase=kb)
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == [(kb, "write")]

    def test_kb_suggest_toolkit_returns_suggest(self, kb):
        from django_ergo.kb_suggest_toolkit import KBSuggestToolkit

        toolkit = KBSuggestToolkit(knowledgebase=kb)
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == [(kb, "suggest")]

    def test_history_toolkit_returns_empty(self, session):
        from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit

        toolkit = ChatWithHistoryToolkit(sessions=[session])
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == []

    def test_default_returns_empty(self):
        from django_ergo.conversation.toolkit import Toolkit

        class FakeToolkit(Toolkit):
            def has_tool(self, n):
                return False

            def execute_tool(self, n, a):
                return ""

            def get_tools_schema(self, a):
                return []

            def render_overview(self):
                return ""

        assert FakeToolkit().get_bound_knowledgebases() == []
