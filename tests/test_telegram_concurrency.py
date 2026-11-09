"""Telegram context races use independent connections, like app/channel workers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from threading import Event

import pytest
from asgiref.sync import async_to_sync
from django.db import close_old_connections
from django.db import connection

from django_ergo.conversation.messages import add_message
from tests.test_bot_plugins import User
from tests.test_bot_plugins import group_update
from tests.test_bot_plugins import passive_telegram_bot


@pytest.mark.django_db(transaction=True)
def test_telegram_passive_and_active_allocate_distinct_sequences(tmp_path):
    User.objects.create(username="cook")
    _, _, plugin = passive_telegram_bot(tmp_path)
    message = group_update(1, text="partner context")["message"]
    session = async_to_sync(plugin.session_for)({}, message["chat"])
    active_read, passive_attempted, passive_done = Event(), Event(), Event()

    def active():
        close_old_connections()

        def pause_allocation(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "MAX(" in sql:
                active_read.set()
                assert passive_attempted.wait(5)
                # Before the fix passive commits the same sequence here. With
                # the shared row lock it waits until the active write commits.
                passive_done.wait(0.5)
            return result

        try:
            with connection.execute_wrapper(pause_allocation):
                async_to_sync(add_message)(
                    session, "assistant", [{"block_type": "text", "text": "app reply"}]
                )
        finally:
            connection.close()

    def passive():
        close_old_connections()

        def observe_lock(execute, sql, params, many, context):
            if "FOR UPDATE" in sql:
                passive_attempted.set()
            return execute(sql, params, many, context)

        try:
            assert active_read.wait(5)
            with connection.execute_wrapper(observe_lock):
                async_to_sync(plugin.capture_passive)([message])
            passive_done.set()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(active), workers.submit(passive)]
        for result in results:
            result.result(timeout=10)

    rows = list(session.messages.prefetch_related("content_blocks"))
    assert [row.sequence for row in rows] == [0, 1]
    assert [row.content_blocks.get().text for row in rows] == [
        "app reply",
        "partner context",
    ]


@pytest.mark.django_db(transaction=True)
def test_telegram_concurrent_first_messages_share_one_thread(tmp_path):
    User.objects.create(username="cook")
    bot, _, first = passive_telegram_bot(tmp_path / "first")
    _, _, second = passive_telegram_bot(tmp_path / "second")
    both_looked_up = Barrier(2)

    def capture(plugin, update_id):
        close_old_connections()
        initial_lookup = True

        def synchronize_missing_thread(execute, sql, params, many, context):
            nonlocal initial_lookup
            result = execute(sql, params, many, context)
            if initial_lookup and "telegram_thread" in str(params):
                initial_lookup = False
                both_looked_up.wait(timeout=5)
            return result

        try:
            with connection.execute_wrapper(synchronize_missing_thread):
                async_to_sync(plugin.capture_passive)(
                    [
                        group_update(update_id, text=f"observation {update_id}")[
                            "message"
                        ]
                    ]
                )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [
            workers.submit(capture, first, 1),
            workers.submit(capture, second, 2),
        ]
        for result in results:
            result.result(timeout=10)

    session = bot.sessions().get(metadata__telegram_thread="Telegram")
    assert session.parent_id is not None
    assert bot.sessions().filter(parent__isnull=True).count() == 1
    assert list(session.messages.values_list("sequence", flat=True)) == [0, 1]
    assert set(
        session.messages.values_list("provenance__origin__message_id", flat=True)
    ) == {
        101,
        102,
    }
