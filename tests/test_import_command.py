"""Tests for import_conversations management command."""

import json
import tempfile
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django_ergo.conversation.models import ConversationSession

User = get_user_model()
pytestmark = pytest.mark.django_db(transaction=True)

SAMPLE_SESSION = [
    {
        "type": "user",
        "message": {"role": "user", "content": "Hello"},
        "sessionId": "session-abc",
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    },
]


class TestImportCommand:
    def test_import_jsonl(self):
        user = User.objects.create_user(username="cmduser1", password="testpass")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for msg in SAMPLE_SESSION:
                f.write(json.dumps(msg) + "\n")
            f.flush()
            call_command("import_conversations", f.name, "--user", "cmduser1")
        assert ConversationSession.objects.filter(user=user).count() == 1

    def test_import_json(self):
        user = User.objects.create_user(username="cmduser2", password="testpass")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(SAMPLE_SESSION, f)
            f.flush()
            call_command("import_conversations", f.name, "--user", "cmduser2")
        assert ConversationSession.objects.filter(user=user).count() == 1

    def test_import_directory(self):
        expected_count = 2
        user = User.objects.create_user(username="cmduser3", password="testpass")
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(expected_count):
                path = Path(tmpdir) / f"session_{i}.jsonl"
                with Path(path).open("w") as f:
                    for msg in SAMPLE_SESSION:
                        f.write(json.dumps(msg) + "\n")
            call_command("import_conversations", tmpdir, "--user", "cmduser3")
        assert ConversationSession.objects.filter(user=user).count() == expected_count
