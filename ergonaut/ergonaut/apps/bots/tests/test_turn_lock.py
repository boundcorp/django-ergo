import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ergonaut.apps.bots import tasks


class FakeLock:
    def __init__(self, name, timeout, thread_local):
        self.name = name
        self.timeout = timeout
        self.thread_local = thread_local
        self.renewed = threading.Event()
        self.released = False

    def acquire(self, blocking, blocking_timeout):
        return True

    def reacquire(self):
        self.renewed.set()

    def release(self):
        self.released = True


class FakeRedis:
    def __init__(self):
        self.locks = []

    def lock(self, name, timeout, thread_local):
        lock = FakeLock(name, timeout, thread_local)
        self.locks.append(lock)
        return lock


def test_turn_lock_is_short_lived_and_renewed_while_held(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(tasks, "redis_client", lambda: client)
    monkeypatch.setattr(tasks, "LOCK_RENEW_SECONDS", 0.01)

    with tasks.session_lock("abc") as acquired:
        assert acquired
        lock = client.locks[0]
        # A restart mid-turn leaves at most a minute-long lock behind, not hours.
        assert lock.name == "ergonaut:turn:abc"
        assert lock.timeout == tasks.LOCK_TTL_SECONDS <= 60
        assert lock.thread_local is False
        assert lock.renewed.wait(2)

    assert lock.released


def test_channel_turn_holds_the_lock_and_steers_with_the_inbox(monkeypatch):
    from asgiref.sync import async_to_sync

    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    queued = []
    monkeypatch.setattr(tasks, "queue_waiting", queued.append)
    monkeypatch.setattr(tasks, "notify", lambda session_id: None)
    session = SimpleNamespace(id="chan-1")
    seen = {}

    async def run(control):
        seen["running"] = tasks.turn_running("chan-1")
        seen["control"] = control
        return "result"

    assert async_to_sync(tasks.run_channel_turn)(session, run) == "result"

    assert seen["running"] is True
    assert isinstance(seen["control"], tasks.InboxControl)
    assert tasks.turn_running("chan-1") is False
    # A message the app sent as the turn ended gets a turn of its own.
    assert queued == ["chan-1"]


def test_busy_channel_sessions_leave_executor_and_idle_turns_available(monkeypatch):
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    monkeypatch.setattr(tasks, "notify", lambda session_id: None)
    queued = []
    monkeypatch.setattr(tasks, "queue_waiting", queued.append)
    real_session_lock = tasks.session_lock
    probe_threads = set()
    busy_ids = {f"busy-channel-{index}" for index in range(6)}

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=3))
        probed = {session_id: asyncio.Event() for session_id in busy_ids}

        @contextlib.contextmanager
        def session_lock(session_id, *, wait=True):
            probe_threads.add(threading.get_ident())
            assert wait is False
            with real_session_lock(session_id, wait=wait) as acquired:
                if session_id in probed:
                    loop.call_soon_threadsafe(probed[session_id].set)
                yield acquired

        monkeypatch.setattr(tasks, "session_lock", session_lock)

        async def unexpected_run(control):
            pytest.fail("A busy session started its channel turn")

        async def idle_run(control):
            assert tasks.turn_running("idle-channel")
            return "idle result"

        with contextlib.ExitStack() as held:
            for session_id in busy_ids:
                assert held.enter_context(real_session_lock(session_id, wait=False))
            waiters = [
                asyncio.create_task(tasks.run_channel_turn(SimpleNamespace(id=session_id), unexpected_run))
                for session_id in busy_ids
            ]
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in probed.values())), 2)
                assert (
                    await asyncio.wait_for(asyncio.to_thread(lambda: "executor available"), 2) == "executor available"
                )
                assert (
                    await asyncio.wait_for(tasks.run_channel_turn(SimpleNamespace(id="idle-channel"), idle_run), 2)
                    == "idle result"
                )
                assert not tasks.turn_running("idle-channel")
                # All busy probes share one thread instead of occupying the pool.
                assert len(probe_threads) == 1
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 2)
            assert all(tasks.turn_running(session_id) for session_id in busy_ids)

    asyncio.run(scenario())
    assert queued == ["idle-channel"]
    assert all(not tasks.turn_running(session_id) for session_id in busy_ids)


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_channel_turn_always_cleans_uploads_releases_and_dispatches(monkeypatch, outcome):
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    session = SimpleNamespace(id=f"channel-{outcome}")
    upload = object()
    events = []
    tasks.set_stop(session.id)

    def remove_uploads(uploads):
        assert tasks.turn_running(session.id)
        assert uploads == [upload]
        events.append("uploads")

    def after_release(kind, session_id):
        assert session_id == session.id
        assert not tasks.turn_running(session_id)
        events.append(kind)

    monkeypatch.setattr(tasks, "remove_uploads", remove_uploads)
    monkeypatch.setattr(tasks, "notify", lambda session_id: after_release("notify", session_id))
    monkeypatch.setattr(tasks, "queue_waiting", lambda session_id: after_release("queued", session_id))

    async def scenario():
        started = asyncio.Event()

        async def run(control):
            assert not tasks.stop_requested(session.id)
            control.uploads.append(upload)
            started.set()
            if outcome == "failed":
                raise ValueError("turn failed")
            if outcome == "cancelled":
                await asyncio.Event().wait()
            return "result"

        turn = asyncio.create_task(tasks.run_channel_turn(session, run))
        await asyncio.wait_for(started.wait(), 2)
        if outcome == "cancelled":
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, 2)
        elif outcome == "failed":
            with pytest.raises(ValueError, match="turn failed"):
                await asyncio.wait_for(turn, 2)
        else:
            assert await asyncio.wait_for(turn, 2) == "result"

    asyncio.run(scenario())
    assert events == ["uploads", "notify", "queued"]
    assert not tasks.turn_running(session.id)


def test_channel_turn_releases_when_upload_cleanup_fails(monkeypatch):
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    events = []
    monkeypatch.setattr(tasks, "notify", lambda session_id: events.append("notify"))
    monkeypatch.setattr(tasks, "queue_waiting", lambda session_id: events.append("queued"))

    def remove_uploads(uploads):
        raise OSError("upload cleanup failed")

    monkeypatch.setattr(tasks, "remove_uploads", remove_uploads)

    async def run(control):
        return "result"

    with pytest.raises(OSError, match="upload cleanup failed"):
        asyncio.run(tasks.run_channel_turn(SimpleNamespace(id="channel-cleanup-failure"), run))
    assert not tasks.turn_running("channel-cleanup-failure")
    assert events == ["notify", "queued"]


def test_channel_turn_cancelled_during_acquire_releases_its_late_lock(monkeypatch):
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    real_session_lock = tasks.session_lock
    release_probe = threading.Event()
    session = SimpleNamespace(id="channel-cancel-acquire")

    async def scenario():
        loop = asyncio.get_running_loop()
        acquired = asyncio.Event()
        finished = asyncio.Event()
        queued = []

        def queue_waiting(session_id):
            queued.append(session_id)
            loop.call_soon_threadsafe(finished.set)

        monkeypatch.setattr(tasks, "notify", lambda session_id: None)
        monkeypatch.setattr(tasks, "queue_waiting", queue_waiting)

        @contextlib.contextmanager
        def slow_probe(session_id, *, wait=True):
            assert wait is False
            with real_session_lock(session_id, wait=False) as locked:
                assert locked
                loop.call_soon_threadsafe(acquired.set)
                assert release_probe.wait(5)
                yield locked

        monkeypatch.setattr(tasks, "session_lock", slow_probe)

        async def run(control):
            pytest.fail("Cancelled acquisition must not run the turn")

        turn = asyncio.create_task(tasks.run_channel_turn(session, run))
        try:
            await asyncio.wait_for(acquired.wait(), 2)
            turn.cancel()
            await asyncio.sleep(0)
            assert tasks.turn_running(session.id)
        finally:
            release_probe.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, 2)
        await asyncio.wait_for(finished.wait(), 2)
        assert queued == [session.id]
        assert not tasks.turn_running(session.id)

    asyncio.run(scenario())


def test_channel_turn_busy_lock_keeps_its_deadline(monkeypatch):
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    monkeypatch.setattr(tasks, "LOCK_SECONDS", 0.01)
    monkeypatch.setattr(tasks, "notify", lambda session_id: pytest.fail("Never acquired the lock"))
    monkeypatch.setattr(tasks, "queue_waiting", lambda session_id: pytest.fail("Never acquired the lock"))
    session = SimpleNamespace(id="channel-deadline")

    async def run(control):
        pytest.fail("A busy session started its channel turn")

    async def scenario():
        with tasks.session_lock(session.id, wait=False) as held:
            assert held
            with pytest.raises(RuntimeError, match=f"Session {session.id} is busy"):
                await asyncio.wait_for(tasks.run_channel_turn(session, run), 2)
            assert tasks.turn_running(session.id)

    asyncio.run(scenario())


def test_channel_turn_second_cancellation_cannot_discard_lock_cleanup(monkeypatch):
    monkeypatch.setattr(tasks, "redis_client", lambda: None)
    session = SimpleNamespace(id="channel-cancel-cleanup")
    release_cleanup = threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        cleaning = asyncio.Event()
        finished = asyncio.Event()
        queued = []

        def remove_uploads(uploads):
            loop.call_soon_threadsafe(cleaning.set)
            assert release_cleanup.wait(5)

        def queue_waiting(session_id):
            queued.append(session_id)
            loop.call_soon_threadsafe(finished.set)

        monkeypatch.setattr(tasks, "remove_uploads", remove_uploads)
        monkeypatch.setattr(tasks, "notify", lambda session_id: None)
        monkeypatch.setattr(tasks, "queue_waiting", queue_waiting)

        async def run(control):
            started.set()
            await asyncio.Event().wait()

        turn = asyncio.create_task(tasks.run_channel_turn(session, run))
        try:
            await asyncio.wait_for(started.wait(), 2)
            turn.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, 2)
            assert tasks.turn_running(session.id)
        finally:
            release_cleanup.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert not tasks.turn_running(session.id)
        assert queued == [session.id]

    asyncio.run(scenario())
