"""Telegram intake must not race, lose queued work, or exhaust the host executor."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.bots.runtime import TurnResult
from django_ergo.conversation.models import BotJob
from django_ergo.plugins.telegram import JOB_TARGET
from django_ergo.settings import api_settings
from tests.test_bot_plugins import telegram_bot
from tests.test_bot_plugins import telegram_thread_bot
from tests.test_bot_plugins import update

User = get_user_model()
WAIT_SECONDS = 5


async def bounded(awaitable, seconds=WAIT_SECONDS):
    return await asyncio.wait_for(awaitable, timeout=seconds)


async def cancel_workers(*plugins):
    tasks = {task for plugin in plugins for task in plugin._tasks}
    for task in tasks:
        task.cancel()
    if tasks:
        await bounded(asyncio.gather(*tasks, return_exceptions=True))


@contextmanager
def channel_runner(settings, runner):
    original = getattr(settings, "DJANGO_ERGO", {})
    settings.DJANGO_ERGO = {**original, "CHANNEL_TURN_RUNNER": runner}
    api_settings.reload()
    try:
        yield
    finally:
        settings.DJANGO_ERGO = original
        api_settings.reload()


class BoundedSessionLock:
    """A real nonblocking session lock, with cancellation/failure cleanup."""

    def __init__(self, lock, closing):
        self.lock = lock
        self.closing = closing
        self.state = threading.Lock()
        self.acquired = False

    def __enter__(self):
        locked = self.lock.acquire(blocking=False)
        with self.state:
            if locked and self.closing.is_set():
                self.lock.release()
                return False
            self.acquired = locked
        return locked

    def __exit__(self, *_):
        with self.state:
            if self.acquired:
                self.acquired = False
                self.lock.release()


class ThreadedChannelRunner:
    """Ergonaut's lock enter/run/exit shape, without importing the host project."""

    def __init__(self):
        self.locks = {}
        self.contexts = []
        self.closing = threading.Event()
        self.attempts = asyncio.Queue()

    async def __call__(self, session, run):
        session_id = str(session.id)
        lock = BoundedSessionLock(
            self.locks.setdefault(session_id, threading.Lock()), self.closing
        )
        self.contexts.append(lock)
        self.attempts.put_nowait(session_id)
        deadline = asyncio.get_running_loop().time() + 10
        try:
            # Match the host: serialize brief nonblocking probes, then wait on
            # asyncio instead of occupying default-executor threads.
            while not await sync_to_async(lock.__enter__, thread_sensitive=True)():
                if asyncio.get_running_loop().time() >= deadline:
                    message = f"Session {session_id} is busy"
                    raise RuntimeError(message)
                await asyncio.sleep(0.01)
            return await run(None)
        finally:
            # Serial cleanup follows even a probe whose result was cancelled.
            await asyncio.shield(
                sync_to_async(lock.__exit__, thread_sensitive=True)(None, None, None)
            )

    def close(self):
        # A cancelled nonblocking probe may still complete in the serial executor.
        # Mark it abandoned and release acquired locks before shutdown.
        self.closing.set()
        for context in self.contexts:
            context.__exit__(None, None, None)


@pytest.mark.django_db(transaction=True)
async def test_telegram_busy_sessions_leave_executor_and_callback_intake_available(  # noqa: PLR0915 — full executor/lock lifecycle
    tmp_path, settings, monkeypatch
):
    await User.objects.acreate(username="lee")
    plugins = []
    completed = []
    dispatch_started = set()
    dispatch_release = asyncio.Event()
    holder_started = asyncio.Event()
    holder_release = asyncio.Event()
    runner = ThreadedChannelRunner()
    holder = None
    loop = asyncio.get_running_loop()
    previous_executor = loop._default_executor
    executor = ThreadPoolExecutor(max_workers=3)

    async def ask(session, text, **kwargs):
        completed.append(text)
        return TurnResult(session)

    async def hold(control):
        holder_started.set()
        await bounded(holder_release.wait(), seconds=20)

    try:
        loop.set_default_executor(executor)
        with channel_runner(settings, runner):
            for index in range(4):
                bot, _, plugin = telegram_thread_bot(tmp_path / str(index))
                monkeypatch.setattr(bot, "ask", ask)
                real_dispatch = plugin.dispatch

                async def dispatch(item, real_dispatch=real_dispatch):
                    dispatch_started.add(item["update_id"])
                    await bounded(dispatch_release.wait(), seconds=20)
                    return await real_dispatch(item)

                monkeypatch.setattr(plugin, "dispatch", dispatch)
                plugins.append(plugin)

            # All four plugin instances and all their distinct Telegram chats
            # resolve to this one existing shared thread/session.
            session = await plugins[0].session_for({}, {"id": -100})
            holder = asyncio.create_task(runner(session, hold))
            assert await bounded(runner.attempts.get()) == str(session.id)
            await bounded(holder_started.wait())
            for index, plugin in enumerate(plugins):
                plugin.api.updates = [
                    update(
                        index * 10 + offset,
                        chat_id=-100 - offset,
                        text=f"{index}:{offset}",
                    )
                    for offset in range(1, 4)
                ]
            assert await bounded(
                asyncio.gather(*(plugin.poll_once() for plugin in plugins))
            ) == [3, 3, 3, 3]
            await bounded(asyncio.sleep(0))
            started_count = len(dispatch_started)
            assert started_count >= 2
            dispatch_release.set()
            # Every admitted chat can reach its asynchronous lock wait, even
            # though there are many more waiters than default-executor threads.
            for _ in range(started_count):
                assert await bounded(runner.attempts.get()) == str(session.id)

            assert await bounded(asyncio.to_thread(lambda: "executor available")) == (
                "executor available"
            )

            api_call = plugins[0].api.call

            async def threaded_api(method, **params):
                # TelegramAPI.call uses this same default pool in production.
                await bounded(asyncio.to_thread(lambda: None))
                return await api_call(method, **params)

            monkeypatch.setattr(plugins[0].api, "call", threaded_api)
            plugins[0].api.updates = [
                {
                    "update_id": 100,
                    "callback_query": {
                        "id": "queued-tap",
                        "data": "ignored",
                        "from": {"id": 111},
                        "message": {"message_id": 50, "chat": {"id": -101}},
                    },
                }
            ]
            assert await bounded(plugins[0].poll_once()) == 1
            assert any(
                method == "answerCallbackQuery"
                and params["callback_query_id"] == "queued-tap"
                for method, params in plugins[0].api.calls
            )
            assert not completed
            holder_release.set()
            await bounded(holder)
            assert not runner.contexts[0].acquired
            await bounded(asyncio.gather(*(plugin.drain() for plugin in plugins)))
            assert len(completed) == 12
            assert len(set(completed)) == 12
            assert (
                not await BotJob.objects.filter(target=JOB_TARGET)
                .exclude(status="completed")
                .aexists()
            )
    finally:
        dispatch_release.set()
        holder_release.set()
        runner.close()
        try:
            await cancel_workers(*plugins)
            if holder is not None:
                holder.cancel()
                await bounded(asyncio.gather(holder, return_exceptions=True))
        finally:
            loop._default_executor = previous_executor
            executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("held_by", ["telegram", "external"])
async def test_telegram_restart_replays_updates_waiting_for_a_turn(
    tmp_path, settings, monkeypatch, held_by
):
    await User.objects.acreate(username="cook")
    bot, _, plugin = telegram_bot(tmp_path / "before")
    session_lock = asyncio.Lock()
    turn_started = asyncio.Event()
    turn_release = asyncio.Event()
    runner_entered = asyncio.Event()
    fresh_plugin = None
    resumed = []

    async def runner(session, run):
        runner_entered.set()
        async with session_lock:
            return await run(None)

    async def ask(session, text, **kwargs):
        turn_started.set()
        await bounded(turn_release.wait(), seconds=20)
        return TurnResult(session)

    async def recovered_ask(session, text, **kwargs):
        resumed.append(text)
        return TurnResult(session)

    monkeypatch.setattr(bot, "ask", ask)
    with channel_runner(settings, runner):
        try:
            if held_by == "external":
                await bounded(session_lock.acquire())
            plugin.api.updates = [update(1, text="first")]
            assert await bounded(plugin.poll_once()) == 1
            await bounded(runner_entered.wait())
            if held_by == "telegram":
                await bounded(turn_started.wait())
            plugin.api.updates = [update(n, text=f"waiting {n}") for n in range(2, 5)]
            assert await bounded(plugin.poll_once()) == 3
            waiting_ids = [2, 3, 4] if held_by == "telegram" else [1, 2, 3, 4]
            jobs = [
                job
                async for job in BotJob.objects.filter(target=JOB_TARGET).order_by(
                    "created_at"
                )
                if job.args["update"]["update_id"] in waiting_ids
            ]
            assert len(jobs) == len(waiting_ids)
            assert [(job.status, job.started_at) for job in jobs] == [
                ("pending", None)
            ] * len(jobs)
            assert plugin._offset == 5

            # A process stop cancels its workers, but must not make waiting
            # messages look as though their turns had already begun.
            await cancel_workers(plugin)
            if held_by == "external":
                session_lock.release()
            fresh_bot, _, fresh_plugin = telegram_bot(tmp_path / "after")
            monkeypatch.setattr(fresh_bot, "ask", recovered_ask)
            # These jobs are deliberately recent: startup recovery must not
            # impose the periodic replay's two-minute retry delay.
            assert await bounded(fresh_plugin.replay(startup=True)) == len(waiting_ids)
            await bounded(fresh_plugin.drain())
            assert resumed == [
                "first" if n == 1 else f"waiting {n}" for n in waiting_ids
            ]
            for job in jobs:
                await job.arefresh_from_db()
                assert job.status == "completed"
                assert job.started_at is not None
        finally:
            turn_release.set()
            await cancel_workers(plugin, *([fresh_plugin] if fresh_plugin else []))
            if session_lock.locked():
                session_lock.release()


@pytest.mark.django_db(transaction=True)
async def test_telegram_slow_photo_preserves_chat_order_without_blocking_other_chats(
    tmp_path, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    sous = await User.objects.acreate(username="sous")
    bot, _, plugin = telegram_bot(tmp_path)
    plugin.users["222"] = "sous"
    download_started = asyncio.Event()
    download_release = asyncio.Event()
    other_finished = asyncio.Event()
    turns = []
    real_download = plugin.api.download

    async def download(file_id):
        if file_id == "slow-photo":
            download_started.set()
            await bounded(download_release.wait(), seconds=20)
        return await real_download(file_id)

    async def ask(session, text, **kwargs):
        turns.append((session.user_id, text))
        if session.user_id == sous.pk:
            other_finished.set()
        return TurnResult(session)

    monkeypatch.setattr(plugin.api, "download", download)
    monkeypatch.setattr(bot, "ask", ask)
    try:
        plugin.api.updates = [
            update(1, caption="first photo", photo=[{"file_id": "slow-photo"}])
        ]
        assert await bounded(plugin.poll_once()) == 1
        await bounded(download_started.wait())
        plugin.api.updates = [
            update(2, text="second text"),
            update(3, chat_id=222, text="other chat"),
        ]
        assert await bounded(plugin.poll_once()) == 2
        await bounded(other_finished.wait())
        # Another chat progresses while the photo's chat has not started a turn.
        assert turns == [(sous.pk, "other chat")]
        download_release.set()
        await bounded(plugin.drain())
        assert [text for user_id, text in turns if user_id == cook.pk] == [
            "first photo",
            "second text",
        ]
        assert (
            not await BotJob.objects.filter(target=JOB_TARGET)
            .exclude(status="completed")
            .aexists()
        )
    finally:
        download_release.set()
        await cancel_workers(plugin)


@pytest.mark.django_db(transaction=True)
async def test_telegram_worker_failure_leaves_replayable_jobs_and_cleans_queues(
    tmp_path, monkeypatch, caplog
):
    await User.objects.acreate(username="cook")
    bot, _, plugin = telegram_bot(tmp_path)
    fail_release = asyncio.Event()
    turn_started = asyncio.Event()
    turn_release = asyncio.Event()
    real_process = plugin.process
    turns = []

    async def broken_process(job):
        await bounded(fail_release.wait(), seconds=20)
        message = "worker failed before dispatch"
        raise RuntimeError(message)

    async def ask(session, text, **kwargs):
        turns.append(text)
        if text == "first":
            turn_started.set()
            await bounded(turn_release.wait(), seconds=20)
        return TurnResult(session)

    monkeypatch.setattr(plugin, "process", broken_process)
    monkeypatch.setattr(bot, "ask", ask)
    try:
        plugin.api.updates = [update(1, text="first"), update(2, text="second")]
        assert await bounded(plugin.poll_once()) == 2
        fail_release.set()
        await bounded(plugin.drain())
        assert "worker failed before dispatch" in caplog.text
        assert [
            status
            async for status in BotJob.objects.filter(target=JOB_TARGET).values_list(
                "status", flat=True
            )
        ] == ["pending", "pending"]
        assert not plugin._update_queues
        assert not plugin._queued_jobs

        monkeypatch.setattr(plugin, "process", real_process)
        assert await bounded(plugin.replay(startup=True)) == 2
        await bounded(turn_started.wait())
        # The first turn is running and the second is already queued. Replaying
        # the still-pending second job must not append another copy of it.
        assert await bounded(plugin.replay(startup=True)) == 1
        turn_release.set()
        await bounded(plugin.drain())
        assert turns == ["first", "second"]
        assert not plugin._update_queues
        assert not plugin._queued_jobs

        # An idle queue must be removed, so later intake creates a live worker.
        plugin.api.updates = [update(3, text="third")]
        assert await bounded(plugin.poll_once()) == 1
        await bounded(plugin.drain())
        assert turns == ["first", "second", "third"]
        assert not plugin._update_queues
        assert not plugin._queued_jobs
        assert (
            not await BotJob.objects.filter(target=JOB_TARGET)
            .exclude(status="completed")
            .aexists()
        )
    finally:
        fail_release.set()
        turn_release.set()
        await cancel_workers(plugin)
