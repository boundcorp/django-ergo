"""Tests for ClaudeCLIEngine."""

from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.engines.claude_cli import ClaudeCLIEngine
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def session(user):
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="cli",
        status="active",
        session_id="test-session-123",
    )


@pytest.fixture()
def engine():
    return ClaudeCLIEngine(config={})


class TestHealthCheck:
    def test_healthy(self, engine):
        engine.process = MagicMock()
        engine.process.returncode = None
        assert engine._health_check() is True  # noqa: SLF001

    def test_dead(self, engine):
        engine.process = MagicMock()
        engine.process.returncode = 1
        assert engine._health_check() is False  # noqa: SLF001

    def test_no_process(self, engine):
        assert engine._health_check() is False  # noqa: SLF001


class TestReconstructMessages:
    def test_empty(self, engine, session):
        assert engine.reconstruct_messages(session) == []

    def test_roundtrip(self, engine, session):
        m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
        ClaudeContentBlock.objects.create(
            message=m0, block_type="text", sequence=0, text="Hello"
        )
        m1 = ClaudeMessage.objects.create(
            session=session, role="assistant", sequence=1, stop_reason="end_turn"
        )
        ClaudeContentBlock.objects.create(
            message=m1, block_type="text", sequence=0, text="Hi!"
        )
        messages = engine.reconstruct_messages(session)
        assert len(messages) == 2  # noqa: PLR2004
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
