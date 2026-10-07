"""Live refresh for bot pages: which tables changed, as server-sent events.

    GET /api/bots/<bot>/tables/events?tables=A,B&since=<fingerprints>

It takes the web app's session or an API key, like ``stream.py``. Each event is JSON:
``{"changed": ["A"], "fingerprints": {"A": [3, "2026-10-06T…", 12], "B": […]}}``. The
first event after connecting says what changed while the client was away, and carries
the baseline fingerprints; its ``changed`` is empty when nothing did.

A table's fingerprint is ``[row count, latest updated_at, highest id]``. The stream
compares them on connect with ``since`` (the ``fingerprints`` of the last event the client
got; the ``id:`` of each event holds the same, so an EventSource that reconnects by itself
sends it back as ``Last-Event-ID``), so a viewer that was disconnected catches up with one
event, and on every poll tick, so a missed notice costs one tick. Nothing is stored.

With ``REDIS_URL`` set the stream also wakes on the notices ``table_changed`` publishes (see
``ergonaut.apps.bots.tasks.notify_table``) and takes a notice as a change even when the
fingerprint didn't move: a bulk ``.update()`` followed by ``touch()`` changes no row count,
``updated_at`` or id. Without Redis a table written only by such updates shows up when
something else changes it. The stream ends after a few minutes; EventSource reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

from asgiref.sync import sync_to_async
from django.db.models import Count, Max
from django.http import HttpResponse, StreamingHttpResponse
from ninja.errors import HttpError

from ergonaut.api.auth import ApiKeyAuth
from ergonaut.api.bots import get_bot

POLL_SECONDS = 1.0
PUBSUB_POLL_SECONDS = 3.0
STREAM_SECONDS = 240
KEEPALIVE_SECONDS = 15
MAX_TABLES = 20


def fingerprint(model) -> list:
    found = model.objects.aggregate(n=Count("pk"), t=Max("updated_at"), m=Max("pk"))
    return [found["n"], found["t"].isoformat() if found["t"] else None, found["m"]]


def fingerprints(models: dict) -> dict:
    return {name: fingerprint(model) for name, model in models.items()}


def parse_since(value: str | None) -> dict | None:
    """The fingerprints a client last saw, or None when it sent none (or nonsense)."""
    if not value:
        return None
    try:
        found = json.loads(value)
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _event(payload: dict) -> str:
    # The id lets an EventSource that reconnects by itself send back where it was.
    return f"id: {json.dumps(payload['fingerprints'], separators=(',', ':'))}\ndata: {json.dumps(payload)}\n\n"


async def notices(bot_name: str, names: set[str]):
    """Yield, per wait, the watched tables a notice named; empty when the wait ran out.

    Waits a poll interval at a time: for Redis notices it wakes sooner."""
    url = os.environ.get("REDIS_URL")
    if not url:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            yield set()
    import redis.asyncio as aioredis

    from ergonaut.apps.bots.tasks import TABLES_CHANNEL

    client = aioredis.Redis.from_url(url)
    pubsub = client.pubsub()
    await pubsub.subscribe(TABLES_CHANNEL)
    loop = asyncio.get_running_loop()
    try:
        while True:
            deadline = loop.time() + PUBSUB_POLL_SECONDS
            named: set[str] = set()
            while loop.time() < deadline:
                notice = await pubsub.get_message(ignore_subscribe_messages=True, timeout=deadline - loop.time())
                data = notice.get("data") if notice else None
                if isinstance(data, bytes):
                    data = data.decode()
                bot, _, table = (data or "").partition(":")
                if bot == bot_name and table in names:
                    named.add(table)
                    break
            yield named
    finally:
        await pubsub.aclose()
        await client.aclose()


def _models(bot, requested: str) -> dict:
    """The watched tables by their model names, from ``?tables=A,B`` (any case). A name that
    isn't one of the bot's tables is skipped (a page's typo shouldn't end its live refresh)."""
    wanted = [name.strip() for name in requested.split(",") if name.strip()]
    if not wanted:
        raise HttpError(400, "Say which tables: ?tables=A,B")
    if len(wanted) > MAX_TABLES:
        raise HttpError(400, f"At most {MAX_TABLES} tables")
    models = {}
    for name in wanted:
        with contextlib.suppress(LookupError):
            model = bot.table(name)
            models[model.__name__] = model
    if not models:
        raise HttpError(404, f"{bot.name} has none of the tables {', '.join(wanted)}")
    return models


async def table_events(request, bot_name):
    user = await sync_to_async(ApiKeyAuth())(request) or await request.auser()
    if not user.is_authenticated:
        return HttpResponse(status=401)
    try:
        bot = get_bot(bot_name, user)
        models = _models(bot, request.GET.get("tables", ""))
    except HttpError as exc:
        return HttpResponse(str(exc.message), status=exc.status_code, content_type="text/plain")
    since = parse_since(request.GET.get("since") or request.headers.get("Last-Event-ID"))

    async def stream():
        loop = asyncio.get_running_loop()
        end = loop.time() + STREAM_SECONDS
        seen = await sync_to_async(fingerprints)(models)
        # Behind the client's last look: one event for everything that moved since.
        yield _event(
            {"changed": [n for n in models if since is not None and since.get(n) != seen[n]], "fingerprints": seen}
        )
        quiet = 0.0
        waits = notices(bot_name, set(models))
        try:
            while loop.time() < end:
                started = loop.time()
                named = await anext(waits)
                now = await sync_to_async(fingerprints)(models)
                changed = [n for n in models if n in named or now[n] != seen[n]]
                seen = now
                if changed:
                    yield _event({"changed": changed, "fingerprints": seen})
                    quiet = 0.0
                else:
                    quiet += loop.time() - started
                    if quiet >= KEEPALIVE_SECONDS:
                        yield ": keepalive\n\n"
                        quiet = 0.0
        finally:
            await waits.aclose()

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
