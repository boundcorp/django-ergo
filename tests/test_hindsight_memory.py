"""Hindsight is an optional, best-effort companion to the deterministic KB."""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from django.test import override_settings

from django_ergo.bots.tools import ToolContext
from django_ergo.plugins import hindsight_memory as memory


def plugin(config=None):
    bot = SimpleNamespace(name="kitchen")
    instance = memory.HindsightMemoryPlugin(bot, config or {})
    instance.on_load()
    return instance


def session():
    return SimpleNamespace(id="chat-1", user_id="lee", parent_id="root")


@override_settings(
    HINDSIGHT_URL="http://hindsight.test:8888", HINDSIGHT_API_KEY="test-key"
)
def test_retain_enqueued_without_http_on_turn(monkeypatch):
    queued = []
    monkeypatch.setattr(
        memory._pool, "submit", lambda fn, *args: queued.append((fn, args))
    )
    p = plugin()
    call = SimpleNamespace(pk="turn-1", metadata={}, error="")
    result = SimpleNamespace(
        text="I will do that.", error="", needs_approval=False, call=call
    )
    import asyncio

    asyncio.run(p.after_turn(session(), "Please remember tea.", result))
    assert len(queued) == 1
    fn, (bank, item) = queued[0]
    assert fn is memory._enqueue
    assert bank == "ergonaut"
    assert item["content"] == "User: Please remember tea.\nAssistant: I will do that."
    assert item["document_id"] == "ergonaut-turn-turn-1"
    assert set(item["tags"]) == {
        "bot:kitchen",
        "chat:chat-1",
        "user:lee",
        "thread:chat-1",
    }
    assert item["metadata"] == {
        "bot": "kitchen",
        "chat": "chat-1",
        "user": "lee",
        "thread": "chat-1",
    }
    assert item["timestamp"]


@override_settings(
    HINDSIGHT_URL="http://hindsight.test:8888", HINDSIGHT_API_KEY="test-key"
)
def test_retain_http_payload(monkeypatch):
    response = Mock()
    response.json.return_value = {"success": True}
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    memory.retain_item("ergonaut", {"content": "Tea", "tags": ["user:lee"]})
    assert (
        post.call_args.args[0]
        == "http://hindsight.test:8888/v1/default/banks/ergonaut/memories"
    )
    assert post.call_args.kwargs["json"] == {
        "items": [{"content": "Tea", "tags": ["user:lee"]}],
        "async": True,
    }
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_automated_turn_not_retained(monkeypatch):
    queued = []
    monkeypatch.setattr(memory._pool, "submit", lambda *args: queued.append(args))
    p = plugin()
    call = SimpleNamespace(
        pk="turn-1", metadata={"message_author": {"kind": "system"}}, error=""
    )
    result = SimpleNamespace(text="Done", error="", needs_approval=False, call=call)
    import asyncio

    asyncio.run(p.after_turn(session(), "Scheduled wake", result))
    assert queued == []


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888", HINDSIGHT_API_KEY="")
def test_prefetch_timeout_swallowed(monkeypatch):
    monkeypatch.setattr(httpx, "post", Mock(side_effect=httpx.TimeoutException("slow")))
    p = plugin({"prefetch": "every_turn", "timeout_seconds": 0.2})
    ctx = ToolContext(bot=p.bot, session=session())
    [source] = p.always_context_sources(ctx, "tea")
    assert source.render(1000) is None
    assert httpx.post.call_args.kwargs["timeout"] == 0.2


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888", HINDSIGHT_API_KEY="")
def test_tool_bank_cannot_be_overridden(monkeypatch):
    response = Mock()
    response.json.return_value = {"results": [{"text": "Lee likes tea"}]}
    monkeypatch.setattr(httpx, "post", Mock(return_value=response))
    p = plugin()
    toolkit = p.make_toolkit(ToolContext(bot=p.bot, session=session()))
    assert "Lee likes tea" in toolkit.execute_tool("memory_recall", {"query": "tea"})
    assert "/ergonaut/memories/recall" in httpx.post.call_args.args[0]
    with pytest.raises(TypeError):
        toolkit.execute_tool("memory_recall", {"query": "tea", "bank": "other"})


@override_settings(HINDSIGHT_URL="", HINDSIGHT_API_KEY="")
def test_unconfigured_plugin_disabled(caplog):
    p = plugin()
    assert not p.enabled
    assert p.toolkits(ToolContext(bot=p.bot, session=session())) == []
    assert (
        p.always_context_sources(ToolContext(bot=p.bot, session=session()), "tea") == []
    )
    assert "HINDSIGHT_URL is unset" in caplog.text
