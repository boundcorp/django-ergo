"""Hindsight is an optional, best-effort companion to the deterministic KB."""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from django.test import override_settings

from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.tool_dispatch import ToolError
from django_ergo.plugins import hindsight_memory as memory


def plugin(config=None):
    bot = SimpleNamespace(name="helper")
    instance = memory.HindsightMemoryPlugin(bot, config or {})
    instance.on_load()
    return instance


def session():
    return SimpleNamespace(id="chat-1", user_id="alice", parent_id="root")


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
        "bot:helper",
        "chat:chat-1",
        "user:alice",
        "thread:chat-1",
        "source:chat",
    }
    assert item["metadata"] == {
        "bot": "helper",
        "chat": "chat-1",
        "user": "alice",
        "thread": "chat-1",
        "source": "chat",
    }
    assert "helper" in item["context"]
    assert "authoritative" in item["context"]
    assert item["timestamp"]


@override_settings(
    HINDSIGHT_URL="http://hindsight.test:8888", HINDSIGHT_API_KEY="test-key"
)
def test_retain_http_payload(monkeypatch):
    response = Mock()
    response.json.return_value = {"success": True}
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    memory.retain_item("ergonaut", {"content": "Tea", "tags": ["user:alice"]})
    assert (
        post.call_args.args[0]
        == "http://hindsight.test:8888/v1/default/banks/ergonaut/memories"
    )
    assert post.call_args.kwargs["json"] == {
        "items": [{"content": "Tea", "tags": ["user:alice"]}],
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
    response.json.return_value = {"results": [{"text": "Alice likes tea"}]}
    monkeypatch.setattr(httpx, "post", Mock(return_value=response))
    p = plugin()
    toolkit = p.make_toolkit(ToolContext(bot=p.bot, session=session()))
    assert "Alice likes tea" in toolkit.execute_tool("memory_recall", {"query": "tea"})
    assert "/ergonaut/memories/recall" in httpx.post.call_args.args[0]
    with pytest.raises(ToolError, match="unknown argument"):
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


# -- hygiene -------------------------------------------------------------------

import asyncio  # noqa: E402

from django_ergo import signals  # noqa: E402


def recall_response(*texts):
    response = Mock()
    response.json.return_value = {
        "results": [{"id": f"id-{i}", "text": t} for i, t in enumerate(texts)]
    }
    return response


def rendered(p, message="tea", turn=None):
    ctx = ToolContext(bot=p.bot, session=session(), turn=turn or {})
    sources = p.always_context_sources(ctx, message)
    return [s.render(1000) for s in sources]


def turn_result(text="Done.", metadata=None, **call_fields):
    call = SimpleNamespace(pk="t1", metadata=metadata or {}, error="", **call_fields)
    return SimpleNamespace(text=text, error="", needs_approval=False, call=call)


@pytest.fixture
def events():
    seen = []

    def receiver(sender, **kwargs):
        seen.append(kwargs)

    signals.memory_event.connect(receiver, weak=False)
    yield seen
    signals.memory_event.disconnect(receiver)


@pytest.mark.parametrize(
    "turn",
    [
        {"message_author": {"kind": "system"}},
        {"message_author": {"kind": "bot"}},
        {"message_provenance": {"kind": "message"}},
        {"recurring_wake": "w1"},
        {"periodic_checkins": True},
        {"message_author": {"kind": "telegram_user", "ref": "9"}},
    ],
)
@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_prefetch_skipped_on_machine_turns(monkeypatch, events, turn):
    post = Mock(return_value=recall_response("a fact"))
    monkeypatch.setattr(httpx, "post", post)
    assert (
        plugin().always_context_sources(
            ToolContext(
                bot=SimpleNamespace(name="helper"), session=session(), turn=turn
            ),
            "Worker finished",
        )
        == []
    )
    post.assert_not_called()
    assert events[-1]["outcome"] == "skipped"
    assert events[-1]["event"] == "prefetch"


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_prefetch_runs_for_human_and_mapped_telegram_turns(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", Mock(return_value=recall_response("Alice likes tea"))
    )
    p = plugin({"telegram_users": [9]})
    for turn in (
        {},
        {"message_author": {"kind": "django_user", "ref": "1"}},
        {"message_author": {"kind": "telegram_user", "ref": "9"}},
    ):
        [section] = rendered(p, turn=turn)
        assert "Alice likes tea" in section.body


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_toolcontext_turn_reaches_plugin_from_runtime():
    assert ToolContext().turn == {}


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_recall_payload_hygiene_and_bot_first_scope(monkeypatch):
    calls = []

    def post(url, json, headers, timeout):
        calls.append(json)
        if "tag_groups" in json:
            return recall_response("bot fact one", "bot fact two", "bot fact three")
        return recall_response("bot fact one", "shared fact about quota")

    monkeypatch.setattr(httpx, "post", post)
    p = plugin({"min_score": 0.4, "top_k": 5})
    [section] = rendered(p)
    bot_call = next(c for c in calls if "tag_groups" in c)
    shared_call = next(c for c in calls if "tags" in c)
    assert bot_call["prefer_observations"] is True
    assert bot_call["query_timestamp"]
    assert bot_call["min_scores"] == {"reranker": 0.4}
    leaves = bot_call["tag_groups"][0]["and"]
    assert [leaf["tags"] for leaf in leaves] == [["bot:helper"], ["user:alice"]]
    assert shared_call["tags"] == ["user:alice"]
    assert shared_call["min_scores"]["reranker"] == 0.8
    lines = section.body.splitlines()
    # duplicate "bot fact one" from the shared pass is dropped
    assert lines == [
        "- bot fact one",
        "- bot fact two",
        "- bot fact three",
        "- shared fact about quota",
    ]
    facts = section.details["hindsight"]["facts"]
    assert [f["scope"] for f in facts] == ["bot", "bot", "bot", "shared"]
    assert section.details["hindsight"]["ids"] == ["id-0", "id-1", "id-2", "id-1"]


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_bot_pass_limited_to_top_k_minus_two(monkeypatch):
    def post(url, json, headers, timeout):
        if "tag_groups" in json:
            return recall_response(*[f"alpha {i} unique{i}" for i in range(9)])
        return recall_response("shared zebra")

    monkeypatch.setattr(httpx, "post", post)
    [section] = rendered(plugin({"top_k": 5}))
    assert len(section.body.splitlines()) == 4  # 3 bot slots + 1 shared


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_user_scope_makes_one_user_wide_request(monkeypatch):
    post = Mock(return_value=recall_response("a fact"))
    monkeypatch.setattr(httpx, "post", post)
    rendered(plugin({"scope": "user", "min_score": 0.3}))
    assert post.call_count == 1
    payload = post.call_args.kwargs["json"]
    assert payload["tags"] == ["user:alice"]
    assert "tag_groups" not in payload
    assert payload["min_scores"] == {"reranker": 0.3}


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_nothing_above_floor_gives_empty_block(monkeypatch, events):
    monkeypatch.setattr(httpx, "post", Mock(return_value=recall_response()))
    assert rendered(plugin()) == [None]
    assert events[-1]["outcome"] == "empty"
    assert events[-1]["count"] == 0


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_prefetch_outcomes_recorded(monkeypatch, events):
    monkeypatch.setattr(httpx, "post", Mock(return_value=recall_response("a fact")))
    rendered(plugin())
    assert (events[-1]["event"], events[-1]["outcome"], events[-1]["count"]) == (
        "prefetch",
        "ok",
        1,
    )
    assert events[-1]["duration"] >= 0
    monkeypatch.setattr(httpx, "post", Mock(side_effect=httpx.TimeoutException("x")))
    assert rendered(plugin({"timeout_seconds": 0.2})) == [None]
    assert events[-1]["outcome"] == "error"


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_prefetch_timeout_outcome(monkeypatch, events):
    import time

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: time.sleep(1) or recall_response("x")
    )
    assert rendered(plugin({"timeout_seconds": 0.2})) == [None]
    assert events[-1]["outcome"] == "timeout"


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_mental_models_block_and_cache(monkeypatch):
    memory._models_cache.clear()
    gets = []

    def get(url, headers, timeout):
        gets.append(url)
        response = Mock()
        response.json.return_value = {
            "name": "App A",
            "content": "Release 1.4 is in review.",
        }
        return response

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(
        httpx,
        "post",
        Mock(return_value=recall_response(*[f"fact {n} z{n}" for n in range(6)])),
    )
    p = plugin({"mental_models": ["mm-1"], "top_k": 5})
    [section] = rendered(p)
    assert section.body.startswith("### App A\nRelease 1.4 is in review.")
    assert section.body.count("\n- ") <= 3
    assert section.details["hindsight"]["mental_models"] == ["mm-1"]
    rendered(p)
    assert len(gets) == 1  # cached
    assert gets[0].endswith("/ergonaut/mental-models/mm-1")


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_mental_models_off_by_default_and_never_block(monkeypatch):
    import time

    memory._models_cache.clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: time.sleep(1))
    monkeypatch.setattr(httpx, "post", Mock(return_value=recall_response("a fact")))
    assert plugin().mental_models == []
    started = time.monotonic()
    [section] = rendered(plugin({"mental_models": ["mm-1"], "timeout_seconds": 0.3}))
    assert time.monotonic() - started < 0.9
    assert "a fact" in section.body and "###" not in section.body


# -- retain filter -------------------------------------------------------------


def retained(monkeypatch):
    queued = []
    monkeypatch.setattr(memory._pool, "submit", lambda fn, *a: queued.append(a))
    return queued


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_suggestions_and_status_stripped_from_retained_text(monkeypatch, events):
    queued = retained(monkeypatch)
    reply = (
        "We settled on the native CLI for workers.\n"
        "Status: waiting on CI\n"
        "\n"
        "Want me to open the PR?\n"
        "\n"
        "Suggested replies:\n- Yes\n- No\n"
    )
    asyncio.run(plugin().after_turn(session(), "use native CLIs", turn_result(reply)))
    [(bank, item)] = queued
    assert (
        item["content"]
        == "User: use native CLIs\nAssistant: We settled on the native CLI for workers."
    )
    assert events[-1]["outcome"] == "queued"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Done.\nWhich do you prefer?\n- A\n- B\n- C", "Done."),
        ("Shipped it.\nNext check-in: 03:28 UTC", "Shipped it."),
        ("Merged #12.\nShall I deploy?", "Merged #12."),
        ("Steps:\n- one\n- two", "Steps:\n- one\n- two"),
        ("Plain answer.", "Plain answer."),
    ],
)
def test_clean_assistant_text(text, expected):
    assert memory.clean_assistant_text(text) == expected


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_config_only_turn_skipped_unless_decision(monkeypatch, events):
    queued = retained(monkeypatch)
    monkeypatch.setattr(
        memory.HindsightMemoryPlugin,
        "_tool_names",
        staticmethod(lambda s, c: ["ergo_self_read", "ergo_skill_load"]),
    )
    p = plugin()
    asyncio.run(p.after_turn(session(), "what are your rules?", turn_result()))
    assert queued == []
    assert (events[-1]["outcome"], events[-1]["reason"]) == ("skipped", "config_only")
    asyncio.run(
        p.after_turn(session(), "from now on use codex for reviews", turn_result())
    )
    assert len(queued) == 1


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_mixed_tool_turn_retained(monkeypatch):
    queued = retained(monkeypatch)
    monkeypatch.setattr(
        memory.HindsightMemoryPlugin,
        "_tool_names",
        staticmethod(lambda s, c: ["ergo_self_read", "send_reply"]),
    )
    asyncio.run(plugin().after_turn(session(), "what happened?", turn_result()))
    assert len(queued) == 1


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_telegram_author_retained_only_when_mapped(monkeypatch, events):
    queued = retained(monkeypatch)
    meta = {"message_author": {"kind": "telegram_user", "ref": "9"}}
    asyncio.run(
        plugin().after_turn(session(), "dinner is at six", turn_result(metadata=meta))
    )
    assert queued == []
    assert events[-1]["reason"] == "telegram_unmapped"
    asyncio.run(
        plugin({"telegram_users": ["9"]}).after_turn(
            session(), "dinner is at six", turn_result(metadata=meta)
        )
    )
    assert len(queued) == 1


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_machine_turn_retain_skip_recorded(monkeypatch, events):
    retained(monkeypatch)
    meta = {"message_provenance": {"kind": "message"}}
    asyncio.run(plugin().after_turn(session(), "hi", turn_result(metadata=meta)))
    assert (events[-1]["event"], events[-1]["outcome"], events[-1]["reason"]) == (
        "retain",
        "skipped",
        "provenance",
    )


def test_invalid_hygiene_config_rejected():
    with override_settings(HINDSIGHT_URL="http://x"):
        with pytest.raises(ValueError, match="scope"):
            plugin({"scope": "everything"})
        with pytest.raises(ValueError, match="min_score"):
            plugin({"min_score": 2})


@pytest.mark.django_db(transaction=True)
async def test_turn_metadata_reaches_plugin_and_injection_is_persisted(
    tmp_path, monkeypatch, settings
):
    settings.HINDSIGHT_URL = "http://hindsight.test:8888"
    from django.contrib.auth import get_user_model

    from tests.test_bots import make_bot
    from tests.test_bots import say

    post = Mock(
        side_effect=lambda *a, **k: recall_response("Alice prefers native CLIs")
    )
    monkeypatch.setattr(httpx, "post", post)
    user = await get_user_model().objects.acreate(username="cook")
    bot, engine = make_bot(
        tmp_path,
        say("Noted."),
        say("Worker done."),
        yaml_text="name: helper\nplugins: [{name: hindsight_memory}]\n",
        tools="",
    )
    root = await bot.root_session(user)

    human = await bot.ask(root, "which CLI for workers?")
    seen = post.call_count
    assert seen >= 1
    assert "Alice prefers native CLIs" in str(engine._client.calls[0]["messages"])
    sections = human.call.metadata["context"]["sections"]
    [memory_section] = [s for s in sections if s["title"] == memory.TITLE]
    assert memory_section["details"]["hindsight"]["facts"][0]["text"] == (
        "Alice prefers native CLIs"
    )

    machine = await bot.ask(
        root, "Worker finished", author={"kind": "system", "ref": "w1"}
    )
    assert post.call_count == seen  # no recall for a machine turn
    assert memory.TITLE not in str(machine.call.metadata["context"]["sections"])
