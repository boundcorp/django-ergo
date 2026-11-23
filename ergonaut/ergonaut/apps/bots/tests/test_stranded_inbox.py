"""Messages left waiting in an idle session (see tasks.redispatch_inboxes), and the
one-task-per-session dispatch that keeps the broker from filling with duplicates.

Incident 2026-11-23: two user messages sat "held" in an idle chat while the default
queue held ~85 tasks, most of them bot-to-bot deliveries the minute sweep re-sent for
recipients that were busy or already had one queued.
"""

import fnmatch
import time

import pytest
from django.conf import settings
from django.utils import timezone
from django_ergo.bots import messaging
from django_ergo.conversation.models import ConversationSession, StructuredCall, ThreadMessage

from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.tests.fakes import say
from ergonaut.apps.bots.tests.test_api import bot_folder, cook, use_bots  # noqa: F401, F811
from ergonaut.celery import app


class FakeLock:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def acquire(self, blocking=True, blocking_timeout=None):
        if self.name in self.store.values:
            return False
        self.store.values[self.name] = b"lock"
        return True

    def reacquire(self):
        pass

    def release(self):
        self.store.values.pop(self.name, None)


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))

        return queue

    def execute(self):
        return [getattr(self.store, name)(*args, **kwargs) for name, args, kwargs in self.ops]


class FakeRedis:
    """The few Redis commands the turn lock, inbox and dispatch markers use."""

    def __init__(self):
        self.values = {}
        self.lists = {}
        self.ttls = {}

    def lock(self, name, timeout=None, thread_local=True):
        return FakeLock(self, name)

    def pipeline(self):
        return FakePipeline(self)

    def rpush(self, key, item):
        self.lists.setdefault(key, []).append(item.encode() if isinstance(item, str) else item)

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def ttl(self, key):
        return self.ttls.get(key, -1)

    def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrem(self, key, count, item):
        items = self.lists.get(key, [])
        raw = item.encode() if isinstance(item, str) else item
        if raw in items:
            items.remove(raw)
            return 1
        return 0

    def delete(self, key):
        self.lists.pop(key, None)
        self.values.pop(key, None)

    def exists(self, key):
        return key in self.values or bool(self.lists.get(key))

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def scan_iter(self, match="*", count=None):
        return [k.encode() for k in list(self.lists) if self.lists[k] and fnmatch.fnmatch(k, match)]

    def publish(self, *args):
        pass


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(tasks, "redis_client", lambda: fake)
    return fake


@pytest.fixture
def session(cook):  # noqa: F811
    return ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "root"})


def strand(session_id, text, *, seconds_ago=5 * 60):
    """A message whose run_turn never ran (lost with a killed worker child)."""
    tasks.push_message(session_id, text)
    key = tasks._inbox_key(session_id)
    tasks.redis_client().lists[key][-1] = (
        tasks.redis_client()
        .lists[key][-1]
        .replace(b'"queued_at": ', f'"queued_at": {time.time() - seconds_ago}, "was": '.encode())
    )


@pytest.mark.django_db(transaction=True)
def test_a_stranded_inbox_in_an_idle_session_is_answered_and_drained(session, redis, use_bots):  # noqa: F811
    use_bots(say("Tacos."))
    strand(session.id, "Dinner?")
    strand(session.id, "And dessert?")
    assert not tasks.turn_running(session.id)
    assert tasks.stranded_inboxes() == [str(session.id)]

    assert tasks.redispatch_inboxes() == [str(session.id)]

    call = session.structured_calls.get()
    assert (call.status, call.request) == ("completed", "Dinner?\n\nAnd dessert?")
    assert tasks.peek_inbox(session.id) == []
    assert tasks.stranded_inboxes() == []
    assert tasks.redispatch_inboxes() == []  # idempotent: nothing runs twice
    assert session.structured_calls.count() == 1


@pytest.mark.django_db
def test_inboxes_a_turn_will_take_are_left_alone(session, redis, monkeypatch):
    sent = []
    monkeypatch.setattr(tasks.run_turn, "delay", lambda *args: sent.append(args))

    tasks.push_message(session.id, "Just sent")  # younger than INBOX_STRANDED_SECONDS
    assert tasks.redispatch_inboxes() == []

    strand(session.id, "Waiting")
    redis.values[f"ergonaut:turn:{session.id}"] = b"lock"  # a turn holds the lock: it takes it
    assert tasks.redispatch_inboxes() == []
    del redis.values[f"ergonaut:turn:{session.id}"]

    assert tasks.queue_inbox_turn(session.id)  # a run_turn is in the broker already
    assert tasks.redispatch_inboxes() == []
    assert sent == [(str(session.id),)]


@pytest.mark.django_db
def test_old_inbox_items_without_a_time_are_dated_by_the_key_ttl(session, redis, monkeypatch):
    sent = []
    monkeypatch.setattr(tasks.run_turn, "delay", lambda *args: sent.append(args))
    key = tasks._inbox_key(session.id)
    redis.rpush(key, '{"id": "c9838dee", "text": "held", "attachment_ids": []}')
    redis.expire(key, tasks.INBOX_TTL_SECONDS - 600)

    assert tasks.redispatch_inboxes() == [str(session.id)]
    assert sent == [(str(session.id),)]


@pytest.mark.django_db
def test_one_run_turn_per_session_waits_in_the_broker(session, redis, monkeypatch, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = False
    sent = []
    monkeypatch.setattr(tasks.run_turn, "delay", lambda *args: sent.append(args))

    for text in ("one", "two", "three"):
        assert tasks.queue_message(session.id, text)
    assert sent == [(str(session.id),)]  # the queued turn takes all three

    # The turn started (and found the inbox): the next message needs a task of its own.
    tasks._release_dispatch(session.id, "turn")
    tasks.queue_message(session.id, "four")
    assert len(sent) == 2

    # Approval answers always go out: they're not inbox turns.
    tasks.queue_turn(session.id, approve=True, approval_ids=["x"])
    assert len(sent) == 3


@pytest.mark.django_db
def test_one_delivery_per_recipient_waits_in_the_broker(session, cook, redis, monkeypatch, settings):  # noqa: F811
    settings.CELERY_TASK_ALWAYS_EAGER = False
    sent = []
    monkeypatch.setattr(tasks.deliver_thread_message, "delay", lambda *args: sent.append(args))
    other = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    first, second, third = (
        ThreadMessage.objects.create(recipient_session=recipient, text=text)
        for recipient, text in ((session, "a"), (session, "b"), (other, "c"))
    )

    for message in (first, second, first, third):
        tasks.queue_thread_message(str(message.id))
    assert sent == [(str(first.id),), (str(third.id),)]

    # The delivery task starts: a later dispatch for that recipient queues again.
    monkeypatch.setattr(tasks, "deliver_locked", lambda message_id: None)
    tasks.deliver_thread_message(str(first.id))
    tasks.queue_thread_message(str(second.id))
    assert sent[-1] == (str(second.id),)


@pytest.mark.django_db
def test_the_sweep_sends_one_delivery_per_idle_recipient(
    session,
    cook,  # noqa: F811
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    busy = ConversationSession.objects.create(user=cook, bot_name="kitchen")
    StructuredCall.objects.create(kind="chat_reply", session=busy, user_id=cook.id, request="x")  # running
    dispatched = []
    monkeypatch.setattr(messaging, "dispatch", lambda message_id, registry=None: dispatched.append(message_id))
    old = timezone.now() - timezone.timedelta(minutes=5)
    rows = [
        ThreadMessage.objects.create(recipient_session=recipient, text=str(n))
        for n, recipient in enumerate((session, session, session, busy, busy))
    ]
    ThreadMessage.objects.update(updated_at=old)

    with django_capture_on_commit_callbacks(execute=True):
        assert messaging.redispatch_waiting(older_than=60) == 1
    # The idle recipient's oldest only (its delivery sends on the next); none for the
    # busy one, whose turn's end sends its queue on.
    assert dispatched == [str(rows[0].id)]


def test_turns_have_their_own_queue_and_a_bounded_slot():
    queue = settings.ERGONAUT_TURN_QUEUE
    for name in ("ergonaut.run_turn", "ergonaut.deliver_thread_message", "ergonaut.revalidate_pending"):
        route = app.amqp.router.route({}, name)
        assert route["queue"].name == queue
    assert app.amqp.router.route({}, "ergonaut.refresh_pull_requests")["queue"].name == "celery"
    assert settings.CELERY_WORKER_PREFETCH_MULTIPLIER == 1
    assert tasks.run_turn.time_limit == tasks.deliver_thread_message.time_limit == tasks.TURN_TIME_LIMIT_SECONDS
    assert "redispatch-inboxes" in settings.CELERY_BEAT_SCHEDULE
