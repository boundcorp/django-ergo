"""Durable Telegram groups and independent sessions survive waits and restarts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest
from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet

from django_ergo.bots.runtime import TurnResult
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.conversation.models import BotJob
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.plugins.telegram import JOB_TARGET
from tests.test_bot_plugins import TELEGRAM_TOOLS
from tests.test_bot_plugins import FakeTelegram
from tests.test_bot_plugins import telegram_bot
from tests.test_bot_plugins import update
from tests.test_bots import make_bot
from tests.test_bots import say
from tests.test_conversation_structured import claude_tool
from tests.test_telegram_scheduling import bounded
from tests.test_telegram_scheduling import channel_runner

User = get_user_model()


class AsyncChannelRunner:
    """Share the host's session locks across plugin instances, without threads."""

    def __init__(self):
        self.locks = {}
        self.attempts = asyncio.Queue()
        self.finished = asyncio.Queue()

    def lock_for(self, session):
        return self.locks.setdefault(str(session.pk), asyncio.Lock())

    async def __call__(self, session, run):
        self.attempts.put_nowait(str(session.pk))
        try:
            async with self.lock_for(session):
                return await run(None)
        finally:
            self.finished.put_nowait(str(session.pk))


@asynccontextmanager
async def child_tasks():
    """Also reap legacy detached album timers, including ones already flushing."""
    existing = asyncio.all_tasks()
    try:
        yield
    finally:
        tasks = asyncio.all_tasks() - existing
        for task in tasks:
            task.cancel()
        if tasks:
            await bounded(asyncio.gather(*tasks, return_exceptions=True))


def album_update(number, file_id, *, caption=""):
    return update(
        number,
        media_group_id="receipts",
        caption=caption,
        photo=[{"file_id": file_id}],
    )


def media_ids(attachments):
    return [attachment.metadata["telegram_file_id"] for attachment in attachments or []]


async def saved_album(plugin):
    return [
        await plugin.accept(
            album_update(1, "receipt-a", caption="Save these receipts")
        ),
        await plugin.accept(album_update(2, "receipt-b")),
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("crash_at", ["collection", "download", "external_lock"])
async def test_telegram_album_waiting_for_external_turn_survives_restart(  # noqa: PLR0915 — crash/restart lifecycle
    tmp_path, settings, monkeypatch, crash_at
):
    cook = await User.objects.acreate(username="cook")
    bot, _, plugin = telegram_bot(tmp_path / "before")
    plugin.album_wait = 60 if crash_at == "collection" else 0.05
    runner = AsyncChannelRunner()
    session = await bot.root_session(cook)
    lock = runner.lock_for(session)
    old_turns = []
    recovered = []
    download_started = asyncio.Event()
    download_release = asyncio.Event()
    real_download = plugin.api.download

    async def interrupted_download(file_id):
        if crash_at == "download" and file_id == "receipt-b":
            download_started.set()
            await bounded(download_release.wait(), seconds=20)
        return await real_download(file_id)

    async def old_ask(session, text, **kwargs):
        old_turns.append(text)
        return TurnResult(session)

    async def recovered_ask(session, text, **kwargs):
        attachments = kwargs.get("attachments") or []
        recovered.append((text, media_ids(attachments)))
        assert [attachment.data for attachment in attachments] == [
            b"\x89PNG fake receipt-a",
            b"\x89PNG fake receipt-b",
        ]
        return TurnResult(session)

    monkeypatch.setattr(bot, "ask", old_ask)
    monkeypatch.setattr(plugin.api, "download", interrupted_download)
    with channel_runner(settings, runner):
        async with child_tasks():
            await bounded(lock.acquire())
            try:
                # Both updates reached durable intake before the process dies.
                # The external web/app turn prevents their grouped claim.
                async with child_tasks():
                    jobs = await saved_album(plugin)
                    assert await bounded(plugin.replay(startup=True)) == 2
                    if crash_at == "external_lock":
                        assert await bounded(runner.attempts.get()) == str(session.pk)
                    elif crash_at == "download":
                        await bounded(download_started.wait())
                    else:
                        # The collection deadline is far away. Yield to the
                        # worker, then kill the process without flushing it.
                        await bounded(asyncio.sleep(0))
                    for job in jobs:
                        await job.arefresh_from_db()
                        assert (job.status, job.started_at) == ("pending", None)
                    assert old_turns == []
                # Exiting the inner scope is process death: no old worker or
                # detached album timer can complete the request afterward.
                lock.release()
                fresh_bot, _, fresh = telegram_bot(tmp_path / "after")
                fresh.album_wait = 0.05
                monkeypatch.setattr(fresh_bot, "ask", recovered_ask)
                assert await bounded(fresh.replay(startup=True)) == 2
                await bounded(fresh.drain())
                await bounded(fresh.flush_albums())
                assert recovered == [
                    ("Save these receipts", ["receipt-a", "receipt-b"])
                ]
                assert old_turns == []
                for job in jobs:
                    await job.arefresh_from_db()
                    assert job.status == "completed"
                    assert job.started_at is not None
            finally:
                download_release.set()
                if lock.locked():
                    lock.release()


@pytest.mark.django_db(transaction=True)
async def test_telegram_overlapping_replays_claim_one_complete_album(
    tmp_path, settings, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    runner = AsyncChannelRunner()
    process_release = asyncio.Event()
    plugins = []
    turns = []

    for name in ("old", "new"):
        bot, _, plugin = telegram_bot(tmp_path / name)
        plugin.album_wait = 0.05
        plugins.append(plugin)
        real_process = plugin.process

        async def process(*args, real_process=real_process, **kwargs):
            # Both replicas must select the pending rows before either starts
            # preparing them; otherwise the overlap would depend on DB timing.
            await bounded(process_release.wait())
            return await real_process(*args, **kwargs)

        async def ask(session, text, name=name, **kwargs):
            turns.append((name, text, media_ids(kwargs.get("attachments"))))
            return TurnResult(session)

        monkeypatch.setattr(plugin, "process", process)
        monkeypatch.setattr(bot, "ask", ask)

    session = await plugins[0].bot.root_session(cook)
    lock = runner.lock_for(session)
    with channel_runner(settings, runner):
        async with child_tasks():
            await bounded(lock.acquire())
            try:
                jobs = await saved_album(plugins[0])
                assert await bounded(
                    asyncio.gather(*(p.replay(startup=True) for p in plugins))
                ) == [2, 2]
                process_release.set()
                for _ in plugins:
                    assert await bounded(runner.attempts.get()) == str(session.pk)
                lock.release()
                # Unlike drain(), these barriers also cover the baseline's
                # detached timers after they have removed themselves from maps.
                for _ in plugins:
                    assert await bounded(runner.finished.get()) == str(session.pk)
                await bounded(asyncio.gather(*(p.drain() for p in plugins)))
                assert len(turns) == 1
                assert turns[0][1:] == (
                    "Save these receipts",
                    ["receipt-a", "receipt-b"],
                )
                for job in jobs:
                    await job.arefresh_from_db()
                    assert job.status == "completed"
                    assert job.started_at is not None
            finally:
                process_release.set()
                if lock.locked():
                    lock.release()


@pytest.mark.django_db(transaction=True)
async def test_telegram_two_busy_chats_leave_idle_chat_and_other_bot_available(  # noqa: PLR0915 — cross-bot executor lifecycle
    tmp_path, settings, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    sous = await User.objects.acreate(username="sous")
    await User.objects.acreate(username="chef")
    bot, _, plugin = telegram_bot(tmp_path / "kitchen")
    plugin.users.update({"222": "sous", "333": "chef"})
    other_bot, _ = make_bot(
        tmp_path / "pantry",
        name="pantry",
        yaml_text="""
            name: pantry
            tools: [tools/pantry.py]
            plugins:
              - name: telegram
                token_env: TEST_TELEGRAM_TOKEN
                users: {333: chef}
        """,
        tools=TELEGRAM_TOOLS,
    )
    other = other_bot.plugin("telegram")
    other.api = FakeTelegram()
    runner = AsyncChannelRunner()
    turns = []
    idle_finished = {text: asyncio.Event() for text in ("idle kitchen", "idle pantry")}
    held_locks = []
    loop = asyncio.get_running_loop()
    previous_executor = loop._default_executor
    executor = ThreadPoolExecutor(max_workers=3)

    async def ask(session, text, **kwargs):
        turns.append((session.bot_name, text))
        if text in idle_finished:
            idle_finished[text].set()
        return TurnResult(session)

    monkeypatch.setattr(bot, "ask", ask)
    monkeypatch.setattr(other_bot, "ask", ask)
    for api in (plugin.api, other.api):
        real_call = api.call

        async def threaded_api(method, real_call=real_call, **params):
            # Production Telegram HTTP uses the same pool as other host work.
            await bounded(asyncio.to_thread(lambda: None))
            return await real_call(method, **params)

        monkeypatch.setattr(api, "call", threaded_api)

    try:
        loop.set_default_executor(executor)
        with channel_runner(settings, runner):
            async with child_tasks():
                try:
                    for user in (cook, sous):
                        session = await bot.root_session(user)
                        lock = runner.lock_for(session)
                        await bounded(lock.acquire())
                        held_locks.append(lock)
                    plugin.api.updates = [
                        update(1, text="cook waits"),
                        update(2, chat_id=222, text="sous waits"),
                    ]
                    assert await bounded(plugin.poll_once()) == 2
                    waiting_sessions = {
                        await bounded(runner.attempts.get()) for _ in held_locks
                    }
                    assert len(waiting_sessions) == 2
                    assert turns == []
                    plugin.api.updates = [update(3, chat_id=333, text="idle kitchen")]
                    # Reusing update_id=1 here must not deduplicate across bots.
                    other.api.updates = [update(1, chat_id=333, text="idle pantry")]
                    assert await bounded(
                        asyncio.gather(plugin.poll_once(), other.poll_once())
                    ) == [1, 1]
                    await bounded(
                        asyncio.gather(
                            *(event.wait() for event in idle_finished.values())
                        )
                    )
                    assert set(turns) == {
                        ("kitchen", "idle kitchen"),
                        ("pantry", "idle pantry"),
                    }
                    assert await bounded(asyncio.to_thread(lambda: "available")) == (
                        "available"
                    )
                    for lock in held_locks:
                        lock.release()
                    await bounded(asyncio.gather(plugin.drain(), other.drain()))
                    assert sorted(turns) == sorted(
                        [
                            ("kitchen", "cook waits"),
                            ("kitchen", "sous waits"),
                            ("kitchen", "idle kitchen"),
                            ("pantry", "idle pantry"),
                        ]
                    )
                finally:
                    for lock in held_locks:
                        if lock.locked():
                            lock.release()
    finally:
        loop._default_executor = previous_executor
        executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.django_db(transaction=True)
async def test_telegram_group_callback_for_unlocked_user_bypasses_waiting_messages(
    tmp_path, settings, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    sous = await User.objects.acreate(username="sous")
    bot, _, plugin = telegram_bot(
        tmp_path,
        claude_tool("order", {"item": "flour"}, tool_id="o1"),
        say("Ordered."),
        say("Cook first."),
        say("Cook second."),
    )
    plugin.users["222"] = "sous"
    group = -500
    runner = AsyncChannelRunner()
    delivered = asyncio.Event()
    cook_messages = []
    real_ask = bot.ask
    real_api_call = plugin.api.call

    async def ask(session, text, **kwargs):
        if session.user_id == cook.pk:
            cook_messages.append(text)
        return await real_ask(session, text, **kwargs)

    async def api_call(method, **params):
        result = await real_api_call(method, **params)
        if method == "sendMessage" and params.get("text") == "Ordered.":
            delivered.set()
        return result

    monkeypatch.setattr(bot, "ask", ask)
    monkeypatch.setattr(plugin.api, "call", api_call)
    with channel_runner(settings, runner):
        async with child_tasks():
            await bounded(
                plugin.handle_update(
                    update(
                        1, chat_id=group, text="Order flour", **{"from": {"id": 222}}
                    )
                )
            )
            sous_session = await bot.root_session(sous)
            call = await bot.pending_call(sous_session)
            assert call is not None
            prompt = plugin.api.sent()[-1]
            assert await bounded(runner.attempts.get()) == str(sous_session.pk)
            cook_session = await bot.root_session(cook)
            lock = runner.lock_for(cook_session)
            await bounded(lock.acquire())
            try:
                plugin.api.updates = [
                    update(2, chat_id=group, text="first", **{"from": {"id": 111}}),
                    update(3, chat_id=group, text="second", **{"from": {"id": 111}}),
                ]
                assert await bounded(plugin.poll_once()) == 2
                assert await bounded(runner.attempts.get()) == str(cook_session.pk)
                plugin.api.updates = [
                    {
                        "update_id": 4,
                        "callback_query": {
                            "id": "sous-approves",
                            "data": prompt["reply_markup"]["inline_keyboard"][0][0][
                                "callback_data"
                            ],
                            "from": {"id": 222},
                            "message": {"message_id": 51, "chat": {"id": group}},
                        },
                    }
                ]
                assert await bounded(plugin.poll_once()) == 1
                await bounded(delivered.wait())
                await call.arefresh_from_db()
                assert call.status == StructuredCallStatus.COMPLETED
                assert cook_messages == []
                assert (
                    "answerCallbackQuery",
                    {"callback_query_id": "sous-approves"},
                ) in (plugin.api.calls)
                assert plugin.api.sent()[-1]["chat_id"] == group
                lock.release()
                await bounded(plugin.drain())
                assert cook_messages == ["first", "second"]
            finally:
                if lock.locked():
                    lock.release()


@pytest.mark.django_db(transaction=True)
async def test_telegram_claimed_turn_recovery_warns_without_replaying_tools(  # noqa: PLR0915 — real tool/crash/recovery lifecycle
    tmp_path, settings, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    yaml_text = """
        name: kitchen
        tools: [tools/pantry.py]
        plugins:
          - name: telegram
            token_env: TEST_TELEGRAM_TOKEN
            users: {111: cook}
    """
    bot, _ = make_bot(
        tmp_path / "before",
        claude_tool("order", {"item": "flour"}, tool_id="o1"),
        say("Ordered."),
        yaml_text=yaml_text,
        tools=TELEGRAM_TOOLS.replace("@bot_tool(requires_approval=True)", "@bot_tool"),
    )
    plugin = bot.plugin("telegram")
    plugin.api = FakeTelegram()
    runner = AsyncChannelRunner()
    tool_runs = []
    tool_finished = asyncio.Event()
    never_return = asyncio.Event()
    original_ask = bot.ask
    execute_tool = FunctionToolkit.execute_tool
    replayed_turns = []

    def track_tool(toolkit, tool_name, arguments):
        result = execute_tool(toolkit, tool_name, arguments)
        if tool_name == "order":
            tool_runs.append((arguments, result))
        return result

    async def crash_after_tool(session, text, **kwargs):
        result = await original_ask(session, text, **kwargs)
        tool_finished.set()
        # The real tool and model response exist, but Telegram has not delivered
        # or completed its claimed job when the process is killed.
        await bounded(never_return.wait(), seconds=20)
        return result

    async def unexpected_replay(session, *args, **kwargs):
        replayed_turns.append(str(session.pk))
        return TurnResult(session)

    monkeypatch.setattr(FunctionToolkit, "execute_tool", track_tool)
    monkeypatch.setattr(bot, "ask", crash_after_tool)
    with channel_runner(settings, runner):
        async with child_tasks():
            async with child_tasks():
                plugin.api.updates = [update(1, text="Order flour")]
                assert await bounded(plugin.poll_once()) == 1
                await bounded(tool_finished.wait())
                session = await bot.root_session(cook)
                assert await bounded(runner.attempts.get()) == str(session.pk)
                job = await BotJob.objects.aget(target=JOB_TARGET)
                assert job.status == "in_progress"
                assert tool_runs == [({"item": "flour"}, "ordered flour")]
            # Recovery must not mistake another live replica holding this lock
            # for an interrupted turn, nor tell the user to repeat tools blindly.
            lock = runner.lock_for(session)
            await bounded(lock.acquire())
            try:
                fresh_bot, _, fresh = telegram_bot(tmp_path / "after")
                monkeypatch.setattr(fresh_bot, "ask", unexpected_replay)
                monkeypatch.setattr(fresh_bot, "resume", unexpected_replay)
                recovery = asyncio.create_task(fresh.replay(startup=True))
                assert await bounded(runner.attempts.get()) == str(session.pk)
                await job.arefresh_from_db()
                assert job.status == "in_progress"
                assert fresh.api.sent() == []
                assert replayed_turns == []
                lock.release()
                await bounded(recovery)
                await bounded(fresh.drain())
                await job.arefresh_from_db()
                assert job.status == "failed"
                assert job.completed_at is not None
                assert "interrupt" in job.error.lower()
                warnings = fresh.api.sent()
                assert len(warnings) == 1
                assert warnings[0]["chat_id"] == 111
                assert "interrupt" in warnings[0]["text"].lower()
                assert "already" in warnings[0]["text"].lower()
                assert replayed_turns == []
                assert tool_runs == [({"item": "flour"}, "ordered flour")]
                # Repeated startup recovery must not warn or execute again.
                await bounded(fresh.replay(startup=True))
                await bounded(fresh.drain())
                assert fresh.api.sent() == warnings
                assert replayed_turns == []
                assert tool_runs == [({"item": "flour"}, "ordered flour")]
            finally:
                if lock.locked():
                    lock.release()


@pytest.mark.django_db(transaction=True)
async def test_telegram_album_precedes_following_text_in_chat_fifo(
    tmp_path, monkeypatch
):
    await User.objects.acreate(username="cook")
    bot, _, plugin = telegram_bot(tmp_path)
    plugin.album_wait = 0.2
    turns = []

    async def ask(session, text, **kwargs):
        turns.append((text, media_ids(kwargs.get("attachments"))))
        return TurnResult(session)

    monkeypatch.setattr(bot, "ask", ask)
    async with child_tasks():
        jobs = await saved_album(plugin)
        jobs.append(await plugin.accept(update(3, text="Now summarize them")))
        assert await bounded(plugin.replay(startup=True)) == 3
        await bounded(plugin.drain())
        await bounded(plugin.flush_albums())
        assert turns == [
            ("Save these receipts", ["receipt-a", "receipt-b"]),
            ("Now summarize them", []),
        ]
        for job in jobs:
            await job.arefresh_from_db()
            assert job.status == "completed"


@pytest.mark.django_db(transaction=True)
async def test_telegram_interrupted_group_claim_rolls_back_all_source_jobs(
    tmp_path, settings, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    bot, _, plugin = telegram_bot(tmp_path / "before")
    plugin.album_wait = 0.05
    runner = AsyncChannelRunner()
    claim_interrupted = asyncio.Event()
    loop = asyncio.get_running_loop()
    bulk_update = QuerySet.bulk_update
    turns = []

    async def ask(session, text, **kwargs):
        turns.append((text, media_ids(kwargs.get("attachments"))))
        return TurnResult(session)

    def interrupt_claim(queryset, objects, fields, *args, **kwargs):
        result = bulk_update(queryset, objects, fields, *args, **kwargs)
        if (
            queryset.model is BotJob
            and "status" in fields
            and len(objects) == 2
            and all(job.status == "in_progress" for job in objects)
        ):
            # Interrupt after the actual SQL write but before its enclosing
            # transaction commits. No source row may survive as claimed.
            loop.call_soon_threadsafe(claim_interrupted.set)
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(bot, "ask", ask)
    with channel_runner(settings, runner):
        async with child_tasks():
            jobs = await saved_album(plugin)
            session = await bot.root_session(cook)
            with monkeypatch.context() as claim_patch:
                claim_patch.setattr(QuerySet, "bulk_update", interrupt_claim)
                assert await bounded(plugin.replay(startup=True)) == 2
                await bounded(claim_interrupted.wait())
                assert await bounded(runner.finished.get()) == str(session.pk)
                for job in jobs:
                    await job.arefresh_from_db()
                    assert (job.status, job.started_at) == ("pending", None)
                assert turns == []
                assert plugin.api.sent() == []
            fresh_bot, _, fresh = telegram_bot(tmp_path / "after")
            fresh.album_wait = 0.05
            monkeypatch.setattr(fresh_bot, "ask", ask)
            assert await bounded(fresh.replay(startup=True)) == 2
            await bounded(fresh.drain())
            assert turns == [("Save these receipts", ["receipt-a", "receipt-b"])]
            for job in jobs:
                await job.arefresh_from_db()
                assert job.status == "completed"
                assert job.started_at is not None


@pytest.mark.django_db(transaction=True)
async def test_telegram_group_callback_does_not_overtake_its_own_queued_text(
    tmp_path, settings, monkeypatch
):
    cook = await User.objects.acreate(username="cook")
    sous = await User.objects.acreate(username="sous")
    bot, engine, plugin = telegram_bot(
        tmp_path,
        claude_tool("order", {"item": "flour"}, tool_id="o1"),
        say("Not ordered."),
    )
    plugin.users["222"] = "sous"
    runner = AsyncChannelRunner()
    turns, tool_runs = [], []
    execute_tool = FunctionToolkit.execute_tool

    def track_tool(toolkit, tool_name, arguments):
        tool_runs.append(tool_name)
        return execute_tool(toolkit, tool_name, arguments)

    async def ask(session, text, **kwargs):
        turns.append((session.user_id, text))
        return TurnResult(session)

    with channel_runner(settings, runner):
        async with child_tasks():
            await bounded(
                plugin.handle_update(
                    update(1, chat_id=-500, text="Order flour", **{"from": {"id": 222}})
                )
            )
            sous_session = await bot.root_session(sous)
            assert await bounded(runner.attempts.get()) == str(sous_session.pk)
            prompt = plugin.api.sent()[-1]
            monkeypatch.setattr(bot, "ask", ask)
            monkeypatch.setattr(FunctionToolkit, "execute_tool", track_tool)
            cook_session = await bot.root_session(cook)
            lock = runner.lock_for(cook_session)
            await bounded(lock.acquire())
            try:
                plugin.api.updates = [
                    update(2, chat_id=-500, text="cook waits", **{"from": {"id": 111}}),
                    update(3, chat_id=-500, text="never mind", **{"from": {"id": 222}}),
                ]
                await bounded(plugin.poll_once())
                assert await bounded(runner.attempts.get()) == str(cook_session.pk)
                plugin.api.updates = [
                    {
                        "update_id": 4,
                        "callback_query": {
                            "id": "late-approval",
                            "data": prompt["reply_markup"]["inline_keyboard"][0][0][
                                "callback_data"
                            ],
                            "from": {"id": 222},
                            "message": {"message_id": 51, "chat": {"id": -500}},
                        },
                    }
                ]
                await bounded(plugin.poll_once())
                # Only unrelated users' decisions may bypass the FIFO. This
                # tap must wait behind its own earlier text, which declines it.
                with pytest.raises(TimeoutError):
                    await bounded(runner.attempts.get(), seconds=0.2)
                lock.release()
                await bounded(plugin.drain())
                assert turns == [(cook.pk, "cook waits"), (sous.pk, "never mind")]
                assert tool_runs == []
                assert await bot.pending_call(sous_session) is None
                assert (
                    plugin.api.sent()[-1]["text"] == "That request was already handled."
                )
                assert len(engine._client.calls) == 2
            finally:
                if lock.locked():
                    lock.release()
