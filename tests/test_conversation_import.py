"""Tests for conversation importers."""

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django_ergo.conversation.importers import ImportService
from django_ergo.conversation.importers.claude_cli import ClaudeCLIImporter
from django_ergo.conversation.models import ClaudeMessage

User = get_user_model()
pytestmark = pytest.mark.django_db

EXPECTED_MESSAGE_COUNT = 6
EXPECTED_INPUT_TOKENS = 50

SAMPLE_SESSION = [
    {
        "type": "user",
        "message": {"role": "user", "content": "Hello"},
        "uuid": "msg-001",
        "sessionId": "session-abc",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi there!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 20},
        },
        "uuid": "msg-002",
    },
    {
        "type": "user",
        "message": {"role": "user", "content": "List files"},
        "uuid": "msg-003",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 30},
        },
        "uuid": "msg-004",
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": "file1.py",
                    "is_error": False,
                }
            ],
        },
        "uuid": "msg-005",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Here are the files."}],
            "stop_reason": "end_turn",
        },
        "uuid": "msg-006",
    },
]


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


class TestClaudeCLIImporter:
    def setup_method(self):
        self.importer = ClaudeCLIImporter()

    def test_detect_valid(self):
        assert self.importer.detect_format(SAMPLE_SESSION) is True

    def test_detect_invalid(self):
        assert self.importer.detect_format([{"role": "user"}]) is False
        assert self.importer.detect_format({"not": "a list"}) is False
        assert self.importer.detect_format([]) is False

    def test_import_conversation(self, user):
        session = async_to_sync(self.importer.import_conversation)(SAMPLE_SESSION, user)
        assert session.engine_type == "claude"
        assert session.status == "paused"
        assert session.session_id == "session-abc"
        assert session.metadata.get("imported_from") == "cli_session"

        messages = list(
            ClaudeMessage.objects.filter(session=session).order_by("sequence")
        )
        assert len(messages) == EXPECTED_MESSAGE_COUNT

        # Check first user message (string content)
        blocks_0 = list(messages[0].content_blocks.all())
        assert messages[0].role == "user"
        assert blocks_0[0].block_type == "text"
        assert blocks_0[0].text == "Hello"

        # Check assistant with usage
        assert messages[1].stop_reason == "end_turn"
        assert messages[1].input_tokens == EXPECTED_INPUT_TOKENS

        # Check tool_use
        blocks_3 = list(messages[3].content_blocks.all())
        assert blocks_3[0].block_type == "tool_use"
        assert blocks_3[0].tool_name == "Bash"

        # Check tool_result
        blocks_4 = list(messages[4].content_blocks.all())
        assert blocks_4[0].block_type == "tool_result"
        assert blocks_4[0].tool_result_for == "toolu_01"


class TestImportService:
    def test_auto_detect(self, user):
        service = ImportService()
        session = async_to_sync(service.import_auto)(SAMPLE_SESSION, user)
        assert session.engine_type == "claude"

    def test_unknown_format(self, user):
        service = ImportService()
        with pytest.raises(ValueError, match="Unrecognized"):
            async_to_sync(service.import_auto)({"unknown": "format"}, user)
