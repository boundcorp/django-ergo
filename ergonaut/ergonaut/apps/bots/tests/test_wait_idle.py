"""``ergonaut manage wait_idle`` (see management/commands/wait_idle.py)."""

import pytest
from django.core.management import call_command
from django_ergo.conversation.models import ConversationSession, StructuredCall

from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.management.commands import wait_idle
from ergonaut.apps.bots.tests.test_api import cook  # noqa: F401


class FakeRedis:
    def __init__(self, held):
        self.held = held

    def exists(self, key):
        return key in self.held

    def scan_iter(self, pattern):
        return iter(self.held)


@pytest.mark.django_db
def test_running_sessions_counts_held_locks_not_their_stop_or_inbox_keys(cook, monkeypatch):  # noqa: F811
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    held = {f"ergonaut:turn:{session.id}", f"ergonaut:turn:{session.id}:inbox", "ergonaut:turn:other:stop"}
    monkeypatch.setattr(tasks, "redis_client", lambda: FakeRedis(held))

    assert wait_idle.running_sessions() == [str(session.id)]


@pytest.mark.django_db
def test_without_redis_a_recent_in_progress_call_counts(cook, monkeypatch):  # noqa: F811
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    assert wait_idle.running_sessions() == []

    StructuredCall.objects.create(kind="chat_reply", session=session, user_id=cook.id, request="hi")
    assert wait_idle.running_sessions() == [str(session.id)]


@pytest.mark.django_db
def test_returns_when_idle_and_fails_after_the_timeout(monkeypatch):
    monkeypatch.setattr(wait_idle, "running_sessions", lambda: [])
    call_command("wait_idle", quiet_for=0, poll=0)

    monkeypatch.setattr(wait_idle, "running_sessions", lambda: ["abc"])
    with pytest.raises(SystemExit):
        call_command("wait_idle", timeout=0, poll=0)


@pytest.mark.django_db
def test_active_workers_block_unless_ignored(cook, monkeypatch):  # noqa: F811
    from django_ergo.conversation.models import Worker

    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    monkeypatch.setattr(wait_idle, "running_sessions", lambda: [])
    worker = Worker.objects.create(session=session, bot_name="kitchen", title="Build", function="task:build")

    assert wait_idle.busy() == ["worker kitchen: Build"]
    assert wait_idle.busy(workers=False) == []
    with pytest.raises(SystemExit):
        call_command("wait_idle", timeout=0, poll=0)
    call_command("wait_idle", timeout=0, quiet_for=0, poll=0, ignore_workers=True)

    worker.status = "completed"
    worker.save()
    call_command("wait_idle", quiet_for=0, poll=0)
