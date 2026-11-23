"""Real consumer boundaries, run only against disposable PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone

from django_ergo.bots import admission_drain as drain
from django_ergo.bots import messaging
from django_ergo.bots import workers
from django_ergo.conversation.models import AdmissionDrain
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import Worker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "ADMISSION_DRAIN_ENABLED": True}
    AdmissionDrain.objects.get_or_create(pk=1)


@pytest.fixture
def session():
    user = get_user_model().objects.create_user(username="drain-test")
    return ConversationSession.objects.create(user=user, bot_name="test")


def lease():
    return drain.acquire(timedelta(seconds=30), timedelta(seconds=60))


def expire():
    AdmissionDrain.objects.filter(pk=1).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )


def test_execution_stays_visible_after_claim_transaction_and_lease_acquisition():
    entered, finish = Event(), Event()

    def run():
        close_old_connections()
        try:
            with drain.execution() as admitted:
                assert admitted
                entered.set()
                assert finish.wait(5)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert entered.wait(5)
        token = lease()
        assert token
        assert drain.executions_active()
        with drain.execution() as admitted:
            assert not admitted
        finish.set()
        future.result(timeout=5)
    assert not drain.executions_active()
    drain.release(token)


def test_worker_queue_and_expired_step_recovery(session, monkeypatch):
    queued = Worker.objects.create(
        session=session, bot_name="test", function="task:test", notify=False
    )
    token = lease()
    assert workers.run(str(queued.pk)) == "draining"
    queued.refresh_from_db()
    assert queued.status == "queued" and queued.polls == 0 and queued.started_at is None
    # Exercise the real claim, then a fake pure local function's normal completion.
    monkeypatch.setattr("django_ergo.bots.background.find_bot", lambda *a: object())
    monkeypatch.setattr(workers, "resolve", lambda *a: lambda: "done")
    monkeypatch.setattr(workers, "_notify", lambda *a: None)
    expire()
    assert workers.run(str(queued.pk)) == "completed"
    queued.refresh_from_db()
    assert queued.polls == 1
    assert not drain.release("stale-owner")
    drain.release(token)
    # A previously started worker continues under the drain, even after a crash
    # left an expired step lease. It is never force-cancelled to reach idle.
    running = Worker.objects.create(
        session=session,
        bot_name="test",
        function="task:test",
        notify=False,
        status="running",
        started_at=timezone.now() - timedelta(hours=2),
        step_started_at=timezone.now() - timedelta(hours=2),
    )
    lease()
    assert workers.run(str(running.pk)) == "completed"
    running.refresh_from_db()
    assert running.polls == 1


def test_thread_message_not_claimed_until_release(session, monkeypatch):
    message = ThreadMessage.objects.create(recipient_session=session, text="queued")
    calls = []
    monkeypatch.setattr(
        messaging, "_run_turn", lambda item, registry: calls.append(str(item.pk))
    )
    dispatched = []
    monkeypatch.setattr(
        messaging, "dispatch", lambda mid, registry=None: dispatched.append(str(mid))
    )
    token = lease()
    messaging.deliver(str(message.pk))
    message.refresh_from_db()
    assert message.status == "queued" and calls == []
    drain.release(token)
    assert messaging.redispatch_waiting(session) == 1
    assert dispatched == [str(message.pk)]
    messaging.deliver(str(message.pk))
    message.refresh_from_db()
    assert message.status == "delivered" and calls == [str(message.pk)]


def test_direct_async_admission_waits_and_cancellation_releases():
    entered = Event()

    @drain.async_execution
    async def consume():
        entered.set()
        await asyncio.Event().wait()

    async def scenario():
        token = await sync_to_async(lease)()
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.15)
        assert not entered.is_set()
        await sync_to_async(drain.release)(token)
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert entered.is_set()
        assert await sync_to_async(drain.executions_active)()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not await sync_to_async(drain.executions_active)()

    async_to_sync(scenario)()


def test_default_off_does_not_fence(settings):
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "ADMISSION_DRAIN_ENABLED": False}
    lease()
    with drain.execution() as admitted:
        assert admitted
    assert not drain.executions_active()


def test_direct_bot_ask_and_approval_resume_wait_without_mutation(tmp_path):
    from tests.test_bots import make_bot
    from tests.test_bots import say
    from tests.test_conversation_structured import claude_tool

    bot, engine = make_bot(
        tmp_path,
        claude_tool("add_to_list", {"item": "milk", "qty": 2}, tool_id="t1"),
        say("Added."),
    )
    user = get_user_model().objects.create_user(username="shopper")

    async def scenario():
        root = await bot.root_session(user)
        token = await sync_to_async(lease)()
        task = asyncio.create_task(bot.ask(root, "We need milk"))
        await asyncio.sleep(0.15)
        assert await root.structured_calls.acount() == 0
        assert len(engine._client.calls) == 0
        await sync_to_async(drain.release)(token)
        paused = await asyncio.wait_for(task, 3)
        assert paused.needs_approval
        token = await sync_to_async(lease)()
        task = asyncio.create_task(bot.resume(root, {"t1": True}))
        await asyncio.sleep(0.15)
        assert len(engine._client.calls) == 1
        call = await root.structured_calls.afirst()
        assert call.status == "awaiting_approval"
        await sync_to_async(drain.release)(token)
        result = await asyncio.wait_for(task, 3)
        assert result.text == "Added."
        assert len(engine._client.calls) == 2
        assert not await sync_to_async(drain.executions_active)()

    async_to_sync(scenario)()


def test_background_consumer_waits_then_executes(monkeypatch):
    from types import SimpleNamespace

    from django_ergo.bots import background

    entered = Event()
    monkeypatch.setattr(
        background,
        "find_bot",
        lambda *a: SimpleNamespace(tasks={"test": lambda: entered.set()}),
    )
    token = lease()

    def consume():
        close_old_connections()
        try:
            background.execute("test", "test", [], {})
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        assert not entered.wait(0.15)
        drain.release(token)
        future.result(timeout=3)
    assert entered.is_set() and not drain.executions_active()


def test_process_crash_drops_execution_registration():
    import json
    import subprocess
    import sys
    import time

    from django.db import connection

    # Only the disposable test database's connection parameters cross stdin.
    # The child exits without cleanup, as a worker crash would.
    params = connection.get_connection_params()
    params = {
        key: value
        for key, value in params.items()
        if key in {"dbname", "host", "port", "user", "password"}
    }
    script = """
import json, os, sys
import psycopg
params = json.loads(sys.stdin.readline())
conn = psycopg.connect(**params, autocommit=True)
conn.execute('SELECT pg_advisory_lock_shared(%s)', [int(sys.argv[1])])
print('registered', flush=True)
sys.stdin.readline()
os._exit(0)
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(drain._EXECUTION_LOCK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        child.stdin.write(json.dumps(params) + "\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "registered"
        assert drain.executions_active()
        token = drain.acquire(timedelta(seconds=0.15), timedelta(seconds=1))
        child.stdin.write("crash\n")
        child.stdin.flush()
        assert child.wait(timeout=3) == 0
    deadline = time.monotonic() + 3
    while drain.executions_active() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not drain.executions_active()
    # An abandoned owner has no finally-release; database-clock TTL restores
    # admission, and a replacement owner fences the abandoned token.
    time.sleep(0.2)
    with drain.execution() as admitted:
        assert admitted
    replacement = lease()
    assert replacement != token and not drain.release(token)
    drain.release(replacement)


def test_real_worker_claim_gap_remains_registered(session, monkeypatch):
    queued = Worker.objects.create(
        session=session, bot_name="test", function="task:test", notify=False
    )
    at_claim, proceed = Event(), Event()
    original = workers._claim

    def claim(worker_id):
        at_claim.set()
        assert proceed.wait(5)
        return original(worker_id)

    monkeypatch.setattr(workers, "_claim", claim)
    monkeypatch.setattr("django_ergo.bots.background.find_bot", lambda *a: object())
    monkeypatch.setattr(workers, "resolve", lambda *a: lambda: "done")
    monkeypatch.setattr(workers, "_notify", lambda *a: None)

    def consume():
        close_old_connections()
        try:
            return workers.run(str(queued.pk))
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        assert at_claim.wait(5)
        token = lease()
        queued.refresh_from_db()
        assert queued.status == "queued" and queued.polls == 0
        assert drain.executions_active()  # even though no Worker claim is visible yet
        proceed.set()
        assert future.result(timeout=5) == "completed"
    assert not drain.executions_active()
    drain.release(token)


def test_schedule_retains_pending_run_until_admission(session, monkeypatch):
    from types import SimpleNamespace

    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ScheduleRun

    schedule = SimpleNamespace(actions=[])
    monkeypatch.setattr("django_ergo.bots.background.find_bot", lambda *a: object())
    monkeypatch.setattr(schedules, "find_schedule", lambda *a: schedule)
    monkeypatch.setattr(schedules, "local_now", lambda *a: timezone.now())
    row = ScheduleRun.objects.create(
        bot_name="test", user=session.user, minute=timezone.now()
    )
    token = lease()
    started = Event()

    def consume():
        close_old_connections()
        try:
            started.set()
            schedules.run_actions(row.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        assert started.wait(3)
        assert not future.done()
        row.refresh_from_db()
        assert row.status == "pending" and row.started_at is None
        drain.release(token)
        future.result(timeout=3)
    row.refresh_from_db()
    assert row.status == "completed"


def test_agent_launch_defers_and_existing_external_watcher_continues(
    tmp_path, monkeypatch
):
    from django_ergo.bots import agents
    from django_ergo.bots.tools import ToolContext
    from tests.test_bot_agents import setup

    bot, session, manager, _kit = setup(tmp_path)
    ctx = ToolContext(bot=bot, session=session, user=session.user)
    monkeypatch.setattr(workers, "_publish", lambda *a: None)
    monkeypatch.setattr(workers, "_notify", lambda *a: None)
    token = lease()
    ready = Event()

    def consume():
        close_old_connections()
        try:
            ready.set()
            return agents.start(
                ctx,
                agents.AgentSpec(
                    brief="local fixture only", agent="codex", workspace=str(tmp_path)
                ),
                "box",
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consume)
        assert ready.wait(3)
        assert manager.started == [] and not Worker.objects.exists()
        drain.release(token)
        worker = future.result(timeout=3)
    assert len(manager.started) == 1 and worker.status == "queued"
    # The external job already exists even though its watcher has not polled.
    token = lease()
    assert workers.run(str(worker.pk), bot.registry) == "running"
    worker.refresh_from_db()
    assert worker.polls == 1
    drain.release(token)


def test_stale_or_other_session_context_does_not_bypass_lease():
    from contextvars import copy_context

    with drain.execution(scope="first"):
        inherited = copy_context()
        token = lease()
        with drain.execution(inherit=True, scope="second") as admitted:
            assert not admitted

    def stale():
        with drain.execution(inherit=True, scope="first") as admitted:
            assert not admitted

    inherited.run(stale)
    drain.release(token)


def test_worker_process_crash_recovers_persisted_step_under_drain(session, monkeypatch):
    import json
    import subprocess
    import sys

    from django.db import connection

    queued = Worker.objects.create(
        session=session, bot_name="test", function="task:test", notify=False
    )
    script = """
import json, os, sys
from django.conf import settings
settings.configure(
    DATABASES={'default': json.loads(sys.stdin.readline())},
    INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes', 'django_ergo'],
    SECRET_KEY='isolated-test', USE_TZ=True,
    DJANGO_ERGO={'ADMISSION_DRAIN_ENABLED': True},
)
import django
django.setup()
from django_ergo.bots import admission_drain as drain, workers
with drain.execution() as admitted:
    assert admitted
    worker, reason = workers._claim(sys.argv[1])
    assert worker is not None and worker.polls == 1
    print('claimed', flush=True)
    sys.stdin.readline()
    os._exit(0)
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(queued.pk)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        child.stdin.write(json.dumps(connection.settings_dict) + "\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "claimed"
        token = lease()
        assert drain.executions_active()
        child.stdin.write("crash\n")
        child.stdin.flush()
        assert child.wait(timeout=3) == 0
    queued.refresh_from_db()
    assert queued.status == "running" and queued.polls == 1 and queued.step_started_at
    # Advance only the disposable row's step timestamp, rather than waiting the
    # full existing worker recovery interval. The crash and claim are real.
    Worker.objects.filter(pk=queued.pk).update(
        step_started_at=timezone.now() - timedelta(hours=2)
    )
    monkeypatch.setattr("django_ergo.bots.background.find_bot", lambda *a: object())
    monkeypatch.setattr(workers, "resolve", lambda *a: lambda: "recovered")
    monkeypatch.setattr(workers, "_notify", lambda *a: None)
    assert workers.run(str(queued.pk)) == "completed"
    queued.refresh_from_db()
    assert queued.polls == 2 and queued.result == "recovered"
    drain.release(token)
