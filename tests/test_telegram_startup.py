"""Telegram startup retries without taking down other bot channels."""

import asyncio
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from django_ergo.bots.runtime import Bot
from django_ergo.plugins import telegram


def plugin_with(call):
    plugin = telegram.TelegramPlugin.__new__(telegram.TelegramPlugin)
    plugin.bot = SimpleNamespace(name="testbot")
    plugin.mode = "polling"
    plugin.api = SimpleNamespace(call=call)
    plugin.replay = AsyncMock()
    plugin.poll_once = AsyncMock()
    return plugin


def http_error(code):
    return urllib.error.HTTPError(
        "https://example.invalid/botSECRET/deleteWebhook", code, "SECRET", {}, None
    )


async def test_transient_cleanup_recovers_before_polling(monkeypatch, caplog):
    calls = []

    async def call(method, **kwargs):
        calls.append(method)
        if len(calls) == 1:
            raise http_error(502)

    plugin = plugin_with(call)
    plugin.poll_once = AsyncMock(side_effect=asyncio.CancelledError)
    delays = []
    monkeypatch.setattr(
        telegram.asyncio, "sleep", AsyncMock(side_effect=lambda n: delays.append(n))
    )
    with pytest.raises(asyncio.CancelledError):
        await plugin.serve()
    assert calls == ["deleteWebhook", "deleteWebhook"]
    assert delays == [1]
    plugin.poll_once.assert_awaited_once()
    assert "SECRET" not in caplog.text


async def test_persistent_transient_failure_has_capped_delays(monkeypatch, caplog):
    call = AsyncMock(side_effect=http_error(502))
    plugin = plugin_with(call)
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 9:
            raise asyncio.CancelledError

    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await plugin.serve()
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    plugin.poll_once.assert_not_awaited()
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_permanent_failure_isolated_from_other_plugins_and_bots(code, caplog):
    plugin = plugin_with(AsyncMock(side_effect=http_error(code)))
    events = []

    async def healthy(name):
        events.append(name)

    one = SimpleNamespace(
        plugins=[plugin, SimpleNamespace(serve=lambda: healthy("plugin"))]
    )
    two = SimpleNamespace(plugins=[SimpleNamespace(serve=lambda: healthy("bot"))])
    await asyncio.gather(Bot.serve(one), Bot.serve(two))
    assert set(events) == {"plugin", "bot"}
    plugin.poll_once.assert_not_awaited()
    plugin.api.call.assert_awaited_once()
    assert "polling disabled" in caplog.text
    assert "SECRET" not in caplog.text


async def test_cancellation_during_cleanup_request():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def call(method, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    plugin = plugin_with(call)
    task = asyncio.create_task(plugin.serve())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    plugin.poll_once.assert_not_awaited()


async def test_cancellation_during_backoff(monkeypatch):
    started = asyncio.Event()

    async def sleep(delay):
        started.set()
        await asyncio.Event().wait()

    plugin = plugin_with(AsyncMock(side_effect=http_error(502)))
    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)
    task = asyncio.create_task(plugin.serve())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    plugin.poll_once.assert_not_awaited()


def _retry_error(headers=None, body=None):
    import io
    import json

    fp = io.BytesIO(json.dumps(body).encode()) if body is not None else None
    return urllib.error.HTTPError(
        "https://example.invalid/botSECRET/deleteWebhook",
        429,
        "SECRET",
        headers or {},
        fp,
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (lambda: _retry_error({"Retry-After": "120"}), 120),
        (lambda: _retry_error(body={"parameters": {"retry_after": 90}}), 90),
        (lambda: _retry_error({"Retry-After": "99999"}), telegram.RETRY_AFTER_MAX),
        (lambda: _retry_error({"Retry-After": "0"}), 1),
    ],
)
async def test_429_honors_retry_after(monkeypatch, error, expected):
    plugin = plugin_with(AsyncMock(side_effect=[error(), None]))
    delays = []
    monkeypatch.setattr(
        telegram.asyncio, "sleep", AsyncMock(side_effect=lambda n: delays.append(n))
    )
    assert await plugin._delete_webhook_for_polling()
    assert delays == [expected]


async def test_unsupported_api_base_fails_permanently_once(monkeypatch, caplog):
    api = telegram.TelegramAPI("SECRET", "invalid-scheme://example.invalid")
    plugin = plugin_with(api.call)
    sleep = AsyncMock()
    monkeypatch.setattr(telegram.asyncio, "sleep", sleep)
    await plugin.serve()
    sleep.assert_not_awaited()
    plugin.poll_once.assert_not_awaited()
    assert caplog.text.count("failed permanently") == 1
    assert "SECRET" not in caplog.text


async def test_connection_error_reason_is_transient(monkeypatch):
    err = urllib.error.URLError(ConnectionRefusedError())
    plugin = plugin_with(AsyncMock(side_effect=[err, None]))
    monkeypatch.setattr(telegram.asyncio, "sleep", AsyncMock())
    assert await plugin._delete_webhook_for_polling()
