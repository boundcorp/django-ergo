"""Chats left busy by a turn that died (see tasks.recover_dead_turns)."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.utils import timezone
from django_ergo.conversation.models import ConversationSession, StructuredCall, ThreadMessage

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


@pytest.mark.django_db
def test_stop_fails_a_dead_turn_at_once(cook, monkeypatch):  # noqa: F811
    """A worker killed mid-turn (OOM) leaves no lock; pressing stop shouldn't wait for beat."""
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    other = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    monkeypatch.setattr(tasks, "redis_client", lambda: Exists(set()))
    dead, just_started, elsewhere = call(session, 3), call(session, 0), call(other, 3)

    assert tasks.recover_stopped_session(session.id) == 1

    dead.refresh_from_db()
    assert dead.status == "failed"
    for row in (just_started, elsewhere):  # too new to judge, or another chat
        row.refresh_from_db()
        assert row.status == "in_progress"


@pytest.mark.django_db
def test_stop_keeps_a_turn_that_holds_its_lock(cook, monkeypatch):  # noqa: F811
    session = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    monkeypatch.setattr(tasks, "redis_client", lambda: Exists({f"ergonaut:turn:{session.id}"}))
    row = call(session, 30)

    assert tasks.recover_stopped_session(session.id) == 0
    row.refresh_from_db()
    assert row.status == "in_progress"


@pytest.fixture
def recovery(monkeypatch, settings):
    held = set()
    queued = []
    monkeypatch.setattr(tasks, "redis_client", lambda: Exists(held))

    @contextmanager
    def lock(session_id, *, wait):
        assert not wait
        yield f"ergonaut:turn:{session_id}" not in held

    monkeypatch.setattr(tasks, "session_lock", lock)
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "THREAD_MESSAGE_RUNNER": queued.append}
    return held, queued


def delivery(user, *, status="delivered"):
    sender = ConversationSession.objects.create(user=user, bot_name="kitchen")
    recipient = ConversationSession.objects.create(
        user=user, bot_name="kitchen", parent=sender, metadata={"bot_role": "thread"}
    )
    message = ThreadMessage.objects.create(
        sender_session=sender, recipient_session=recipient, text="Do the work", status=status
    )
    attempt = call(recipient, 30)
    attempt.metadata = {"thread_message": str(message.pk)}
    attempt.save(update_fields=["metadata"])
    return message, attempt


@pytest.mark.django_db(transaction=True)
def test_dead_delivery_is_failed_and_reports_upward_once(cook, recovery):  # noqa: F811
    message, attempt = delivery(cook)
    assert tasks.recover_dead_turns() == 1
    message.refresh_from_db()
    assert message.status == "failed"
    assert message.error == tasks.DEAD_TURN_ERROR
    reply = ThreadMessage.objects.get(in_reply_to=message)
    assert reply.recipient_session_id == message.sender_session_id
    assert "Could not deliver" in reply.text
    assert recovery[1] == [str(reply.pk)]
    assert tasks.recover_dead_deliveries() == 0
    assert ThreadMessage.objects.filter(in_reply_to=message).count() == 1


@pytest.mark.django_db(transaction=True)
def test_old_orphan_blocks_self_resolve_until_recovered(cook, recovery):  # noqa: F811
    """Nov 28: a reply turn was blocked by a Nov 15 delivery whose turn died."""
    from django_ergo.bots import orchestrator
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.tool_dispatch import ToolError

    message, attempt = delivery(cook)
    StructuredCall.objects.filter(pk=attempt.pk).update(status="failed", error=tasks.DEAD_TURN_ERROR)
    incoming = ThreadMessage.objects.create(
        recipient_session=message.recipient_session,
        sender_session=message.sender_session,
        in_reply_to=message,
        text="Handoff complete",
        status="delivered",
    )
    current = call(message.recipient_session, 0)
    current.metadata = {"thread_message": str(incoming.pk)}
    current.save(update_fields=["metadata"])
    ctx = ToolContext(bot=SimpleNamespace(name="kitchen"), session=message.recipient_session, user=cook)
    with pytest.raises(ToolError, match="still working on a request"):
        orchestrator.resolve(ctx)
    # Recovery must wait while this turn holds its lock, then clean the historical
    # orphan on the next sweep. The current reply is never settled by recovery.
    recovery[0].add(f"ergonaut:turn:{message.recipient_session_id}")
    assert tasks.recover_dead_deliveries() == 0
    recovery[0].clear()
    assert tasks.recover_dead_deliveries() == 1
    assert orchestrator.resolve(ctx, summary="handoff complete")["status"] == "resolved"
    incoming.refresh_from_db()
    assert incoming.status == "delivered"


@pytest.mark.django_db
@pytest.mark.parametrize("latest_status", ["in_progress", "awaiting_approval", "completed", "stopped", "turn_limited"])
def test_recovery_preserves_newer_attempts(cook, recovery, latest_status):  # noqa: F811
    message, attempt = delivery(cook, status="waiting")
    StructuredCall.objects.filter(pk=attempt.pk).update(status="failed", error=tasks.DEAD_TURN_ERROR)
    StructuredCall.objects.create(
        session=message.recipient_session,
        user=cook,
        kind="chat_reply",
        request="retry",
        metadata=attempt.metadata,
        status=latest_status,
    )
    assert tasks.recover_dead_deliveries() == 0
    message.refresh_from_db()
    assert message.status == "waiting"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["queued", "answered", "failed"])
def test_recovery_preserves_other_message_states(cook, recovery, status):  # noqa: F811
    message, attempt = delivery(cook, status=status)
    StructuredCall.objects.filter(pk=attempt.pk).update(status="failed", error=tasks.DEAD_TURN_ERROR)
    assert tasks.recover_dead_deliveries() == 0
    message.refresh_from_db()
    assert message.status == status


@pytest.mark.django_db
def test_recovery_rechecks_after_lock_acquisition(cook, recovery, monkeypatch):  # noqa: F811
    message, attempt = delivery(cook)
    StructuredCall.objects.filter(pk=attempt.pk).update(status="failed", error=tasks.DEAD_TURN_ERROR)

    @contextmanager
    def lock(*args, **kwargs):
        # A retry finished between selecting the orphan and acquiring its lock.
        ThreadMessage.objects.filter(pk=message.pk).update(status="answered")
        yield True

    monkeypatch.setattr(tasks, "session_lock", lock)
    assert tasks.recover_dead_deliveries() == 0
    message.refresh_from_db()
    assert message.status == "answered"


@pytest.mark.django_db
def test_recovery_requires_a_matching_dead_attempt(cook, recovery, monkeypatch):  # noqa: F811
    message, attempt = delivery(cook)
    # An ordinary failure must not be mistaken for an abandoned worker.
    StructuredCall.objects.filter(pk=attempt.pk).update(status="failed", error="model failed")
    assert tasks.recover_dead_deliveries() == 0
    # Nor can a dead call belonging to a different session fail this delivery.
    StructuredCall.objects.filter(pk=attempt.pk).update(session=message.sender_session, error=tasks.DEAD_TURN_ERROR)
    assert tasks.recover_dead_deliveries() == 0
    # Without a shared lock service even a matching orphan is left alone.
    StructuredCall.objects.filter(pk=attempt.pk).update(session=message.recipient_session)
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    assert tasks.recover_dead_deliveries() == 0
    message.refresh_from_db()
    assert message.status == "delivered"


@pytest.mark.django_db(transaction=True)
def test_stop_recovers_only_its_own_orphaned_delivery(cook, recovery):  # noqa: F811
    message, attempt = delivery(cook)
    other, other_attempt = delivery(cook)
    StructuredCall.objects.filter(pk__in=[attempt.pk, other_attempt.pk]).update(
        status="failed", error=tasks.DEAD_TURN_ERROR
    )
    assert tasks.recover_stopped_session(message.recipient_session_id) == 0
    message.refresh_from_db()
    other.refresh_from_db()
    assert message.status == "failed"
    assert other.status == "delivered"
