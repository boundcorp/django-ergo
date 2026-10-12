import threading

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
    from types import SimpleNamespace

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
