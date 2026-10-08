import asyncio
import json
from urllib.parse import urlencode

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ergonaut.api import page_events
from ergonaut.apps.bots import tasks
from ergonaut.apps.bots.tests.pagefixtures import write_bot

BOT = """
    name: kitchen
    engine: {type: claude}
    orchestration: false
    tables: [tables.py]
    permissions: {users: [cook]}
"""

TABLES = """
    from django.db import models
    from django_ergo.bots import BotTable

    class Pantry(BotTable):
        name = models.CharField(max_length=100)

    class Recipe(BotTable):
        title = models.CharField(max_length=100)
"""


@pytest.fixture
def kitchen(tmp_path, install):
    registry, _ = install(write_bot(tmp_path / "kitchen", BOT, tables=TABLES), "ergo_bot_kitchen")
    return registry.get("kitchen")


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(page_events, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(page_events, "PUBSUB_POLL_SECONDS", 0.3)
    monkeypatch.setattr(page_events, "STREAM_SECONDS", 3)


async def login(async_client, name="cook"):
    await sync_to_async(get_user_model().objects.create_user)(name, f"{name}@example.com", "pw")
    response = await async_client.post(
        "/api/auth/login", {"username": name, "password": "pw"}, content_type="application/json"
    )
    assert response.status_code == 200


async def events(response, done):
    """Events until ``done(events)``; each is ``{"id": ..., **data}``."""
    found = []
    async for chunk in response.streaming_content:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.startswith("id: "):
            ident, _, data = text.partition("\ndata: ")
            found.append({"id": ident[4:], **json.loads(data)})
            if done(found):
                break
    return found


def pantry(kitchen, **values):
    return sync_to_async(kitchen.table("Pantry").objects.create)(**values)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_says_which_tables_changed_by_their_fingerprints(async_client, kitchen, fast):
    await login(async_client)
    row = await pantry(kitchen, name="flour")
    response = await async_client.get("/api/bots/kitchen/tables/events?tables=pantry,Recipe")
    assert response["Content-Type"] == "text/event-stream"

    async def change():
        await asyncio.sleep(0.3)
        await pantry(kitchen, name="salt")
        await asyncio.sleep(0.3)
        row.name = "rye"
        await sync_to_async(row.save)()
        await asyncio.sleep(0.3)
        await sync_to_async(kitchen.table("Recipe").objects.create)(title="bread")

    task = asyncio.create_task(change())
    found = await events(response, lambda e: len(e) >= 4)
    await task

    baseline, added, edited, other = found[:4]
    # The first event is the baseline: nothing changed, and where each table stands.
    assert baseline["changed"] == []
    assert set(baseline["fingerprints"]) == {"Pantry", "Recipe"}  # the models' names, not the request's
    assert baseline["fingerprints"]["Pantry"][0] == 1 and baseline["fingerprints"]["Recipe"][0] == 0
    assert added["changed"] == ["Pantry"] and added["fingerprints"]["Pantry"][0] == 2  # a new row
    assert edited["changed"] == ["Pantry"] and edited["fingerprints"]["Pantry"][0] == 2  # an edit moves updated_at
    assert edited["fingerprints"] != added["fingerprints"]
    assert other["changed"] == ["Recipe"]
    # Each event's id is its fingerprints, for an EventSource that reconnects by itself.
    assert json.loads(other["id"]) == other["fingerprints"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_client_that_was_away_catches_up_with_one_event(async_client, kitchen, fast):
    await login(async_client)
    await pantry(kitchen, name="flour")
    url = "/api/bots/kitchen/tables/events?tables=Pantry,Recipe"

    def with_since(since):
        return f"{url}&{urlencode({'since': since})}"

    [seen] = await events(await async_client.get(url), lambda e: len(e) == 1)

    # Back with nothing missed: no change reported.
    since = json.dumps(seen["fingerprints"])
    [quiet] = await events(await async_client.get(with_since(since)), lambda e: len(e) == 1)
    assert quiet["changed"] == []

    # Away while two tables changed (one of them twice): one event names both.
    await pantry(kitchen, name="salt")
    await pantry(kitchen, name="pepper")
    await sync_to_async(kitchen.table("Recipe").objects.create)(title="bread")
    [caught_up] = await events(await async_client.get(with_since(since)), lambda e: len(e) == 1)
    assert caught_up["changed"] == ["Pantry", "Recipe"]

    # The reconnect header carries the same thing as ?since=.
    header = await async_client.get(url, headers={"Last-Event-ID": since})
    [again] = await events(header, lambda e: len(e) == 1)
    assert again["changed"] == ["Pantry", "Recipe"]
    # Nonsense for either is the same as nothing.
    [lost] = await events(await async_client.get(with_since("{nope")), lambda e: len(e) == 1)
    assert lost["changed"] == []


class FakePubSub:
    def __init__(self, queue):
        self.queue = queue

    async def subscribe(self, channel):
        self.channel = channel

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        try:
            return {"data": await asyncio.wait_for(self.queue.get(), timeout)}
        except TimeoutError:
            return None

    async def aclose(self):
        pass


class FakeRedis:
    queue: asyncio.Queue

    @classmethod
    def from_url(cls, url):
        return cls()

    def pubsub(self):
        return FakePubSub(FakeRedis.queue)

    async def aclose(self):
        pass


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_redis_notice_is_a_change_even_when_the_rows_look_the_same(async_client, kitchen, fast, monkeypatch):
    import redis.asyncio

    FakeRedis.queue = asyncio.Queue()
    monkeypatch.setattr(redis.asyncio, "Redis", FakeRedis)
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    await login(async_client)
    await pantry(kitchen, name="flour")
    response = await async_client.get("/api/bots/kitchen/tables/events?tables=Pantry,Recipe")

    async def publish():
        await asyncio.sleep(0.3)
        # Another bot's table, and a table nobody here watches: not ours.
        await FakeRedis.queue.put(b"bakery:Pantry")
        await FakeRedis.queue.put(b"kitchen:Pizza")
        await asyncio.sleep(0.5)
        # A bulk update then touch(): no new row, no new updated_at, but a notice.
        await sync_to_async(kitchen.table("Pantry").objects.update)(name="rye")
        await FakeRedis.queue.put(b"kitchen:Pantry")

    task = asyncio.create_task(publish())
    found = await events(response, lambda e: len(e) >= 2)
    await task
    assert [e["changed"] for e in found] == [[], ["Pantry"]]
    assert found[0]["fingerprints"] == found[1]["fingerprints"]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_stream_needs_login_a_visible_bot_and_known_tables(async_client, kitchen, fast):
    url = "/api/bots/kitchen/tables/events"
    assert (await async_client.get(f"{url}?tables=Pantry")).status_code == 401
    await sync_to_async(get_user_model().objects.create_user)("stranger", "s@example.com", "pw")
    await async_client.post(
        "/api/auth/login", {"username": "stranger", "password": "pw"}, content_type="application/json"
    )
    assert (await async_client.get(f"{url}?tables=Pantry")).status_code == 404  # not a user of this bot
    await async_client.post("/api/auth/logout")
    await login(async_client)
    assert (await async_client.get(url)).status_code == 400
    assert (await async_client.get(f"{url}?tables=Nope")).status_code == 404
    # A name that isn't a table is skipped, so one typo in a page doesn't end its live refresh.
    mixed = await async_client.get(f"{url}?tables=Nope,Pantry")
    [first] = await events(mixed, lambda e: len(e) == 1)
    assert list(first["fingerprints"]) == ["Pantry"]
    assert (await async_client.get("/api/bots/nobody/tables/events?tables=Pantry")).status_code == 404
    assert (await async_client.get(f"{url}?tables=Pantry")).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_a_table_change_is_published_for_open_streams(kitchen, monkeypatch):
    published = []

    class Client:
        def publish(self, channel, message):
            published.append((channel, message))

    monkeypatch.setattr(tasks, "redis_client", Client)
    table = kitchen.table("Pantry")
    row = table.objects.create(name="flour")
    assert published == [("ergonaut:tables", "kitchen:Pantry")]
    published.clear()
    table.objects.all().update(name="rye")
    assert published == []
    table.touch()
    row.delete()
    assert published == [("ergonaut:tables", "kitchen:Pantry")] * 2

    # Best effort: a broken Redis doesn't break the write.
    class Broken:
        def publish(self, *args):
            raise ConnectionError

    monkeypatch.setattr(tasks, "redis_client", Broken)
    table.objects.create(name="salt")
    assert table.objects.count() == 1
