"""Host consumer fencing and the deliberately closed rollout boundary."""

import asyncio
from datetime import timedelta
from threading import Event

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from celery.exceptions import Retry
from django_ergo.bots import admission_drain as drain
from django_ergo.conversation.models import AdmissionDrain, ConversationSession, Worker

from ergonaut import upgrades
from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.management.commands import wait_idle
from ergonaut.apps.users.factories import UserFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def enabled(settings, monkeypatch):
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "ADMISSION_DRAIN_ENABLED": True}
    AdmissionDrain.objects.get_or_create(pk=1)
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    tasks._local_inboxes.clear()
    tasks._local_pending.clear()
    yield
    tasks._local_inboxes.clear()
    tasks._local_pending.clear()


@pytest.fixture
def session():
    return ConversationSession.objects.create(user=UserFactory(), bot_name="test")


def lease():
    return drain.acquire(timedelta(seconds=30), timedelta(seconds=60))


def test_inbox_preserved_and_sweep_requeues(session, monkeypatch):
    sid = str(session.pk)
    tasks.push_message(sid, "keep this")
    consumed = []
    monkeypatch.setattr(tasks, "answer_inbox", lambda s: consumed.extend(tasks.drain_inbox(str(s.pk))))
    monkeypatch.setattr(tasks, "queue_waiting", lambda *a: None)
    token = lease()
    tasks.run_turn.run(sid)
    assert tasks.peek_inbox(sid)[0]["text"] == "keep this" and consumed == []
    drain.release(token)
    dispatched = []
    monkeypatch.setattr(tasks.run_turn, "delay", lambda *a: dispatched.append(a))
    assert sid in tasks.redispatch_inboxes(older_than=-1)
    assert dispatched
    tasks.run_turn.run(sid)
    assert consumed[0]["text"] == "keep this" and not tasks.inbox_waiting(sid)


@pytest.mark.parametrize("kind", ["approval", "revalidation"])
def test_approval_tasks_retry_before_consumption(kind, session, monkeypatch):
    sid = str(session.pk)
    lease()
    monkeypatch.setattr(tasks, "answer_approval", lambda *a, **k: pytest.fail("approval consumed"))
    with pytest.raises(Retry):
        if kind == "approval":
            tasks.run_turn.run(sid, approve=True, approval_ids=["original-id"])
        else:
            tasks.revalidate_pending.run(sid, "call-id", "digest")


@pytest.mark.parametrize("cancel", [False, True])
def test_channel_deferral_and_cleanup(session, cancel):
    ran = Event()

    async def run(control):
        ran.set()
        assert await sync_to_async(drain.executions_active)()
        if cancel:
            await asyncio.Event().wait()
        return "finished"

    async def scenario():
        token = await sync_to_async(lease)()
        task = asyncio.create_task(tasks.run_channel_turn(session, run))
        await asyncio.sleep(0.15)
        assert not ran.is_set()
        await sync_to_async(drain.release)(token)
        if cancel:
            for _ in range(100):
                if ran.is_set():
                    break
                await asyncio.sleep(0.01)
            assert ran.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await asyncio.wait_for(task, 3) == "finished"
        assert not await sync_to_async(drain.executions_active)()

    async_to_sync(scenario)()


def test_idle_retains_queued_workers_but_waits_running(session):
    queued = Worker.objects.create(session=session, bot_name="test", function="task:test", title="queued")
    assert wait_idle.active_workers(draining=True) == []
    queued.status = "running"
    queued.save(update_fields=["status"])
    assert wait_idle.active_workers(draining=True) == ["test: queued"]


def test_preparation_never_crosses_unimplemented_handoff(monkeypatch):
    observations = []
    original = wait_idle.busy

    def observe(**kwargs):
        with drain.execution() as admitted:
            observations.append(admitted)
        return original(**kwargs)

    monkeypatch.setattr(wait_idle, "busy", observe)
    result = upgrades.prepare_drain(wait_timeout=1, quiet_for=0, poll=0.01, workers=True, log=lambda *a: None)
    assert "fenced rollout handoff is not implemented" in result
    assert observations == [False]
    assert AdmissionDrain.objects.get(pk=1).owner is None


def test_preparation_timeout_and_exception_release():
    with drain.execution() as admitted:
        assert admitted
        result = upgrades.prepare_drain(wait_timeout=0.02, quiet_for=0, poll=0.01, workers=True, log=lambda *a: None)
        assert "timed out" in result
    assert AdmissionDrain.objects.get(pk=1).owner is None
    with drain.execution():
        with pytest.raises(RuntimeError, match="log failed"):
            upgrades.prepare_drain(
                wait_timeout=1,
                quiet_for=0,
                poll=0.01,
                workers=True,
                log=lambda *a: (_ for _ in ()).throw(RuntimeError("log failed")),
            )
    assert AdmissionDrain.objects.get(pk=1).owner is None


def test_upgrade_entrypoint_does_not_call_legacy_upgrader(monkeypatch, tmp_path):
    from ergonaut.apps.bots.tests.test_upgrades import RELEASE, Recorder
    from ergonaut.upgrades import github

    Recorder.calls = []
    monkeypatch.setattr(upgrades, "_state_path", lambda: tmp_path / "upgrade.json")
    monkeypatch.setattr(github, "latest", lambda *a: RELEASE)
    monkeypatch.setattr(github, "compare", lambda *a: "ahead")
    monkeypatch.setattr(upgrades, "load_upgrader", lambda: Recorder())
    assert "fenced rollout handoff" in upgrades.run(wait_timeout=1, quiet_for=0, poll=0.01)
    assert Recorder.calls == []
    assert AdmissionDrain.objects.get(pk=1).owner is None


def test_external_agent_watcher_is_never_treated_as_deferred_queue(session):
    Worker.objects.create(session=session, bot_name="test", function="agent:local", title="launched")
    assert wait_idle.active_workers(draining=True) == ["test: launched"]


def test_existing_inbox_turn_finishes_but_next_batch_defers(session, monkeypatch):
    sid = str(session.pk)
    tasks.push_message(sid, "first")
    completed = []

    class Bot:
        async def ask(self, session, message, **kwargs):
            completed.append(message)
            # A coordinator acquired the lease during this admitted turn.
            await sync_to_async(lease)()
            tasks.push_message(sid, "next")

    monkeypatch.setattr(tasks, "loaded_bot", lambda *a: Bot())
    tasks.answer_inbox(session)
    assert completed == ["first"]
    assert tasks.peek_inbox(sid)[0]["text"] == "next"
    assert not drain.executions_active()


def test_telegram_durable_source_claim_waits_for_host_admission(session, settings):
    from django_ergo.conversation.models import BotJob
    from django_ergo.plugins.telegram import TelegramPlugin

    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "CHANNEL_TURN_RUNNER": tasks.run_channel_turn}
    job = BotJob.objects.create(bot_name="test", name="update", target="telegram:update", args={})

    async def claimed_run(control):
        await TelegramPlugin._claim([job], session)
        assert await sync_to_async(drain.executions_active)()
        await TelegramPlugin._complete([job])

    async def scenario():
        token = await sync_to_async(lease)()
        task = asyncio.create_task(TelegramPlugin.with_session_lock(None, session, claimed_run))
        await asyncio.sleep(0.15)
        await job.arefresh_from_db()
        assert job.status == "pending" and job.started_at is None
        await sync_to_async(drain.release)(token)
        await asyncio.wait_for(task, 3)
        await job.arefresh_from_db()
        assert job.status == "completed"

    async_to_sync(scenario)()


@pytest.mark.parametrize("kind", ["task", "schedule"])
def test_deferred_celery_consumers_retry_without_consuming(kind, monkeypatch):
    from django_ergo.bots import background, schedules

    lease()
    monkeypatch.setattr(background, "execute", lambda *a: pytest.fail("task consumed"))
    monkeypatch.setattr(schedules, "run_actions", lambda *a: pytest.fail("schedule consumed"))
    with pytest.raises(Retry):
        if kind == "task":
            tasks.run_bot_task.run("test", "task", ["original"], {"arg": "preserved"})
        else:
            tasks.run_schedule.run(123)


def test_gate_losing_lease_during_observation_preserves_replacement(monkeypatch):
    from django.utils import timezone

    replacement = []

    def replaced(**kwargs):
        AdmissionDrain.objects.filter(pk=1).update(expires_at=timezone.now() - timedelta(seconds=1))
        replacement.append(lease())
        return []

    monkeypatch.setattr(wait_idle, "busy", replaced)
    result = upgrades.prepare_drain(wait_timeout=1, quiet_for=0, poll=0.01, workers=True, log=lambda *a: None)
    assert "lease expired" in result
    assert AdmissionDrain.objects.get(pk=1).owner == replacement[0]
    drain.release(replacement[0])
