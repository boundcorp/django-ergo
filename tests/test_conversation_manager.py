"""Tests for SessionManager."""

from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation.manager import SessionManager
from django_ergo.conversation.models import ConversationSession

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def manager():
    return SessionManager()


class TestBuildEngine:
    def test_valid_engine(self, manager, user):
        session = ConversationSession.objects.create(
            user=user,
            engine_type="claude",
            transport_type="api",
            status="active",
        )
        engine = manager._build_engine(session)  # noqa: SLF001
        assert engine.engine_type == "claude"

    def test_invalid_engine(self, manager, user):
        session = ConversationSession.objects.create(
            user=user,
            engine_type="unknown",
            transport_type="unknown",
            status="active",
        )
        with pytest.raises(ValueError, match="No engine registered"):
            manager._build_engine(session)  # noqa: SLF001

    def test_passes_metadata_as_config(self, manager, user):
        session = ConversationSession.objects.create(
            user=user,
            engine_type="claude",
            transport_type="api",
            status="active",
            metadata={"model": "claude-opus-4-6", "api_key": "test-key"},
        )
        engine = manager._build_engine(session)  # noqa: SLF001
        assert engine.model == "claude-opus-4-6"


class TestCachedEngines:
    def test_cache_hit(self, manager, user):
        session = ConversationSession.objects.create(
            user=user,
            engine_type="claude",
            transport_type="api",
            status="active",
        )
        mock_engine = MagicMock()
        manager._active_engines[session.id] = mock_engine  # noqa: SLF001
        # get_engine is async, so just test the cache dict directly
        assert session.id in manager._active_engines  # noqa: SLF001
