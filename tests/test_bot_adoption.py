"""Adoption preserves a thread and its descendants while changing its role."""

from io import StringIO

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from django_ergo.bots import archival
from django_ergo.bots import overview
from django_ergo.bots.adoption import adopt_chat
from django_ergo.bots.registry import BotRegistry
from django_ergo.conversation.models import ConversationSession
from tests.test_bots import make_bot

YAML = """
name: kitchen
engine: {type: claude, config: {model: claude-test}}
chats:
  reports: {description: Weekly reports, instructions: Keep reports short.}
"""


@pytest.fixture
def setup_chat(tmp_path, transactional_db):
    bot, _ = make_bot(tmp_path, yaml_text=YAML, tools="")
    user = get_user_model().objects.create_user(
        username="cook", email="cook@example.com"
    )
    main = ConversationSession.objects.create(
        user=user,
        bot_name=bot.name,
        engine_type="claude",
        transport_type="api",
        status="active",
        metadata={"bot_role": "main"},
        system_prompt="main",
    )
    thread = ConversationSession.objects.create(
        user=user,
        bot_name=bot.name,
        engine_type="claude",
        transport_type="api",
        status="active",
        parent=main,
        metadata={"bot_role": "thread", "title": "My work", "other": 7},
        system_prompt="old prompt",
    )
    return bot, user, main, thread


def test_adopt_and_rollback_preserve_session_and_children(setup_chat):
    bot, user, main, thread = setup_chat
    child = ConversationSession.objects.create(
        user=user,
        bot_name=bot.name,
        engine_type="claude",
        transport_type="api",
        status="active",
        parent=thread,
        metadata={"bot_role": "thread"},
    )
    original = dict(thread.metadata)
    adopted, changed = adopt_chat(bot, "reports", thread, user=user)
    assert changed and adopted.pk == thread.pk
    assert adopted.parent_id is None
    assert adopted.metadata["bot_role"] == "chat"
    assert adopted.metadata["chat"] == "reports"
    assert adopted.metadata["title"] == "My work"
    assert adopted.metadata["adopted_from"]["parent_id"] == str(main.pk)
    assert adopted.system_prompt == bot.instructions_for("reports")
    child.refresh_from_db()
    assert child.parent_id == thread.pk
    assert async_to_sync(bot.chat_session)(user, "reports").pk == thread.pk
    assert overview.chat_name(adopted) == "reports (named chat)"
    assert "- reports (named chat):" in overview.overview(bot.tool_context(main))
    assert not archival.idle_threads(bot).filter(pk=thread.pk).exists()
    assert adopt_chat(bot, "reports", thread)[1] is False
    restored, changed = adopt_chat(bot, "reports", thread, rollback=True)
    assert changed and restored.parent_id == main.pk
    assert restored.metadata == original
    assert restored.status == "active"
    assert restored.system_prompt == "old prompt"
    child.refresh_from_db()
    assert child.parent_id == thread.pk


def test_dry_run_and_archived_thread(setup_chat):
    bot, user, main, thread = setup_chat
    thread.status = "completed"
    thread.metadata["archived_at"] = "2020-01-01T00:00:00Z"
    thread.save(update_fields=["status", "metadata"])
    before = dict(thread.metadata)
    assert adopt_chat(bot, "reports", thread, dry_run=True)[1]
    thread.refresh_from_db()
    assert (
        thread.status == "completed"
        and thread.metadata == before
        and thread.parent_id == main.pk
    )
    adopt_chat(bot, "reports", thread)
    assert async_to_sync(bot.chat_session)(user, "reports").pk == thread.pk
    assert adopt_chat(bot, "reports", thread, rollback=True, dry_run=True)[1]
    thread.refresh_from_db()
    assert thread.metadata["bot_role"] == "chat"
    restored, _ = adopt_chat(bot, "reports", thread, rollback=True)
    assert restored.status == "completed" and restored.metadata == before


def test_custom_prompt_is_preserved(setup_chat):
    bot, _, _, thread = setup_chat
    thread.metadata["custom_instructions"] = True
    thread.save(update_fields=["metadata"])
    adopted, _ = adopt_chat(bot, "reports", thread)
    assert adopted.system_prompt == "old prompt"
    restored, _ = adopt_chat(bot, "reports", thread, rollback=True)
    assert restored.system_prompt == "old prompt"


def test_refusals_and_existing_chat(setup_chat):
    bot, user, main, thread = setup_chat
    other = get_user_model().objects.create_user(username="other")
    with pytest.raises(ValueError, match="no named chat"):
        adopt_chat(bot, "missing", thread)
    with pytest.raises(ValueError, match="no named chat"):
        adopt_chat(bot, "main", thread)
    with pytest.raises(ValueError, match="main chat"):
        adopt_chat(bot, "reports", main)
    with pytest.raises(ValueError, match="different user"):
        adopt_chat(bot, "reports", thread, user=other)
    thread.bot_name = "other"
    thread.save(update_fields=["bot_name"])
    with pytest.raises(ValueError, match="different bot"):
        adopt_chat(bot, "reports", thread)
    thread.bot_name = bot.name
    thread.save(update_fields=["bot_name"])
    with pytest.raises(ValueError, match="not been adopted"):
        adopt_chat(bot, "reports", thread, rollback=True)
    existing = ConversationSession.objects.create(
        user=user,
        bot_name=bot.name,
        engine_type="claude",
        transport_type="api",
        status="active",
        metadata={"bot_role": "chat", "chat": "reports"},
    )
    with pytest.raises(ValueError, match=str(existing.pk)):
        adopt_chat(bot, "reports", thread)
    thread.refresh_from_db()
    assert thread.parent_id == main.pk and thread.metadata["bot_role"] == "thread"
    existing.status = "completed"
    existing.save(update_fields=["status"])
    assert adopt_chat(bot, "reports", thread)[1]


def test_rollback_restores_absent_title_and_checks_parent(setup_chat):
    bot, _, main, thread = setup_chat
    thread.metadata.pop("title")
    thread.save(update_fields=["metadata"])
    adopt_chat(bot, "reports", thread)
    main.delete()
    with pytest.raises(ValueError, match="no longer exists"):
        adopt_chat(bot, "reports", thread, rollback=True)
    thread.refresh_from_db()
    assert thread.metadata["bot_role"] == "chat"


def test_rollback_restores_missing_title(setup_chat):
    bot, _, _, thread = setup_chat
    thread.metadata.pop("title")
    thread.save(update_fields=["metadata"])
    adopt_chat(bot, "reports", thread)
    restored, _ = adopt_chat(bot, "reports", thread, rollback=True)
    assert "title" not in restored.metadata and "chat" not in restored.metadata


def test_rollback_after_chat_definition_removed(setup_chat):
    bot, _, main, thread = setup_chat
    adopt_chat(bot, "reports", thread)
    del bot.definition.chats["reports"]
    with pytest.raises(ValueError, match="not the requested"):
        adopt_chat(bot, "different", thread, rollback=True)
    assert adopt_chat(bot, "reports", thread, rollback=True, dry_run=True)[1]
    restored, _ = adopt_chat(bot, "reports", thread, rollback=True)
    assert restored.parent_id == main.pk and restored.metadata["bot_role"] == "thread"


def test_management_command(setup_chat, monkeypatch):
    bot, _, main, thread = setup_chat
    registry = BotRegistry()
    registry.add(bot)
    monkeypatch.setattr("django_ergo.bots.webhooks.get_registry", lambda: registry)
    args = ["--bot", bot.name, "--chat", "reports", "--session", str(thread.pk)]
    out = StringIO()
    call_command(
        "ergo_adopt_chat", *args, "--user", "cook@example.com", "--dry-run", stdout=out
    )
    assert "Would adopt" in out.getvalue()
    assert all(
        part in out.getvalue()
        for part in (
            "role ",
            "parent ",
            "title ",
            "status ",
            "refresh named chat prompt",
        )
    )
    thread.refresh_from_db()
    assert thread.parent_id == main.pk
    with pytest.raises(CommandError, match="different user"):
        call_command("ergo_adopt_chat", *args, "--user", "other@example.com")
    call_command("ergo_adopt_chat", *args, stdout=StringIO())
    out = StringIO()
    call_command("ergo_adopt_chat", *args, stdout=out)
    assert "Already adopted" in out.getvalue()
    call_command("ergo_adopt_chat", *args, "--rollback", stdout=StringIO())
    thread.refresh_from_db()
    assert thread.parent_id == main.pk and thread.metadata["bot_role"] == "thread"
