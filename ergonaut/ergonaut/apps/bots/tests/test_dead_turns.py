"""Chats left busy by a turn that died (see tasks.recover_dead_turns)."""

import pytest
from django.utils import timezone
from django_ergo.conversation.models import ConversationSession, StructuredCall

from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.tests.test_api import cook  # noqa: F401


class Exists:
    def __init__(self, held):
        self.held = held

    def exists(self, key):
        return key in self.held

    def publish(self, *args):
        pass

    def llen(self, key):
        return 0


def call(session, minutes_ago):
    row = StructuredCall.objects.create(kind="chat_reply", session=session, user_id=session.user_id, request="hi")
    when = timezone.now() - timezone.timedelta(minutes=minutes_ago)
    StructuredCall.objects.filter(id=row.id).update(updated_at=when)
    return row


@pytest.mark.django_db
def test_a_dead_turn_is_failed_and_a_live_one_kept(cook, monkeypatch):  # noqa: F811
    dead = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    live = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    fresh = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    monkeypatch.setattr(tasks, "redis_client", lambda: Exists({f"ergonaut:turn:{live.id}"}))
    dead_call, live_call, fresh_call = call(dead, 30), call(live, 30), call(fresh, 1)

    assert tasks.recover_dead_turns() == 1

    dead_call.refresh_from_db()
    assert dead_call.status == "failed"
    assert "stopped without finishing" in dead_call.error
    for row in (live_call, fresh_call):
        row.refresh_from_db()
        assert row.status == "in_progress"


@pytest.mark.django_db
def test_without_redis_nothing_is_touched(cook, monkeypatch):  # noqa: F811
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    row = call(session, 60)

    assert tasks.recover_dead_turns() == 0
    row.refresh_from_db()
    assert row.status == "in_progress"
