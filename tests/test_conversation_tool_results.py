"""Tests for stubbing older large tool results in what each model call sends."""

from __future__ import annotations

import copy
import json

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.images import image_ref
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import run_structured_call
from django_ergo.conversation.tool_results import trim_tool_results
from django_ergo.conversation.toolkit import Toolkit
from django_ergo.settings import api_settings
from tests.test_conversation_structured import VALID_PLAN
from tests.test_conversation_structured import FakeOpenAIClient
from tests.test_conversation_structured import Plan
from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_tool
from tests.test_conversation_structured import openai_tool

User = get_user_model()

BIG = "\n".join(f"node {i}: rect" for i in range(60))  # 60 lines, > 500 chars


@pytest.fixture(autouse=True)
def count_only(settings):
    """Most tests here check the count; the size budget would keep every BIG."""
    settings.DJANGO_ERGO = {"TOOL_RESULTS_CHARS_IN_CONTEXT": 0}


def claude_exchange(tool_id, result, *, name="tree", is_error=False):
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result,
                    "is_error": is_error,
                }
            ],
        },
    ]


def openai_exchange(call_id, result, *, name="tree"):
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def _results(messages):
    out = []
    for message in messages:
        if message["role"] == "tool":
            out.append(message["content"])
        elif isinstance(message["content"], list):
            out.extend(
                b["content"] for b in message["content"] if b["type"] == "tool_result"
            )
    return out


# ---------------------------------------------------------------------------
# trim_tool_results
# ---------------------------------------------------------------------------


def test_claude_keeps_newest_large_results_and_stubs_older_ones():
    messages = [{"role": "user", "content": [{"type": "text", "text": "draw"}]}]
    for i in range(5):
        messages += claude_exchange(f"t{i}", BIG)
    before = copy.deepcopy(messages)

    sent = trim_tool_results(messages, keep=3)

    results = _results(sent)
    assert results[2:] == [BIG, BIG, BIG]
    assert results[0] == results[1]
    assert results[0].startswith("[tree result, 60 lines, ")
    assert (
        "Note what you need; call the tool again only if you still need detail."
        in (results[0])
    )
    assert messages == before  # stored history is untouched
    # Pairing stays valid: every tool_use still has its tool_result.
    uses = [b["id"] for m in sent if m["role"] == "assistant" for b in m["content"]]
    ids = [
        b["tool_use_id"]
        for m in sent
        if m["role"] == "user"
        for b in m["content"]
        if b["type"] == "tool_result"
    ]
    assert uses == ids


def test_short_results_and_errors_stay_and_do_not_count():
    messages = [
        *claude_exchange("big0", BIG),
        *claude_exchange("err", "E" * 900, is_error=True),
        *claude_exchange("short", "ok"),
        *claude_exchange("big1", BIG),
    ]

    sent = trim_tool_results(messages, keep=1)

    assert _results(sent) == [_results(sent)[0], "E" * 900, "ok", BIG]
    assert _results(sent)[0].startswith("[tree result")


def test_openai_tool_messages_are_stubbed_by_name():
    messages = [{"role": "system", "content": "sys"}]
    messages += openai_exchange("c0", BIG, name="penpot_tree")
    messages += openai_exchange("c1", BIG, name="penpot_tree")

    sent = trim_tool_results(messages, keep=1)

    assert sent[2]["content"].startswith("[penpot_tree result, 60 lines, ")
    assert sent[2]["tool_call_id"] == "c0"
    assert sent[4]["content"] == BIG


def test_images_in_a_stubbed_result_are_left_to_the_image_window():
    ref = image_ref(name="shot.png", media_type="image/png", url="https://x/s.png")
    messages = [
        *claude_exchange("t0", [{"type": "text", "text": BIG}, ref]),
        *claude_exchange("t1", BIG),
    ]

    sent = trim_tool_results(messages, keep=1)

    content = _results(sent)[0]
    assert content[0]["text"].startswith("[tree result")
    assert content[1] == ref


def test_setting_controls_the_default_and_none_turns_it_off(settings):
    messages = [m for i in range(7) for m in claude_exchange(f"t{i}", BIG)]

    assert api_settings.TOOL_RESULTS_IN_CONTEXT == 6
    sent = _results(trim_tool_results(messages))
    assert sent[0].startswith("[tree result")
    assert sent[1:] == [BIG] * 6

    settings.DJANGO_ERGO = {
        "TOOL_RESULTS_IN_CONTEXT": 1,
        "TOOL_RESULTS_CHARS_IN_CONTEXT": 0,
    }
    assert _results(trim_tool_results(messages))[0].startswith("[tree result")

    settings.DJANGO_ERGO = {"TOOL_RESULTS_IN_CONTEXT": None}
    assert trim_tool_results(messages) is messages


def test_size_budget_keeps_more_small_results(settings):
    settings.DJANGO_ERGO = {}  # defaults: keep 6, 20% of a 200k window
    small = [m for i in range(10) for m in claude_exchange(f"f{i}", BIG)]
    assert _results(trim_tool_results(small)) == [BIG] * 10

    settings.DJANGO_ERGO = {"TOOL_RESULTS_CHARS_IN_CONTEXT": 40_000}

    huge = "x" * 15_000
    messages = [
        *claude_exchange("old", BIG),
        *claude_exchange("h0", huge),
        *claude_exchange("h1", huge),
        *claude_exchange("h2", huge),
        *claude_exchange("new", BIG),
    ]
    sent = _results(trim_tool_results(messages, keep=3))
    # The newest three always stay; h0 would go over the budget, so it and
    # everything older is stubbed.
    assert sent[2:] == [huge, huge, BIG]
    assert sent[0].startswith("[tree result") and sent[1].startswith("[tree result")


# ---------------------------------------------------------------------------
# In the structured-call loop
# ---------------------------------------------------------------------------


class TreeToolkit(Toolkit):
    def has_tool(self, tool_name):
        return tool_name == "tree"

    def execute_tool(self, tool_name, arguments):
        return f"tree #{arguments['n']}\n{BIG}"

    def get_tools_schema(self, adapter):
        return [{"name": "tree", "input_schema": {"type": "object"}}]

    def render_overview(self):
        return ""


@pytest.fixture
def user():
    return User.objects.create_user(username="trimmer", password="x")


def _tree_calls(count):
    return [claude_tool("tree", {"n": i}, tool_id=f"t{i}") for i in range(count)]


@pytest.mark.django_db(transaction=True)
async def test_standalone_call_sends_stubs_but_stores_full_results(user):
    engine = claude_engine(*_tree_calls(5), claude_tool("submit_output", VALID_PLAN))
    engine.tool_results_in_context = 2
    spec = StructuredCallSpec(
        kind="designer", response_model=Plan, toolkits=[TreeToolkit()]
    )

    result = await run_structured_call(spec, "Draw it", user=user, engine=engine)

    assert result.ok
    last = engine._client.calls[-1]["messages"]
    sent = _results(last)
    assert [s.startswith("[tree result") for s in sent] == [True] * 3 + [False] * 2
    assert sent[-1].startswith("tree #4")
    stored = _results(result.call.transcript)
    assert len(stored) == 6  # five trees and the submit_output result
    assert all(r.startswith("tree #") for r in stored[:5])


@pytest.mark.django_db(transaction=True)
async def test_session_call_sends_stubs_but_stores_full_results(user):
    session = await ConversationSession.objects.acreate(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    engine = claude_engine(*_tree_calls(5), claude_tool("submit_output", VALID_PLAN))
    engine.tool_results_in_context = 3
    engine.tool_results_tokens = 1
    spec = StructuredCallSpec(
        kind="designer", response_model=Plan, toolkits=[TreeToolkit()]
    )

    result = await run_structured_call(spec, "Draw it", session=session, engine=engine)

    assert result.ok
    assert result.call.metadata["context"]["stubbed_results"] == 0
    assert engine.last_request_info["stubbed_results"] == 2
    sent = _results(engine._client.calls[-1]["messages"])
    assert [s.startswith("[tree result") for s in sent] == [True] * 2 + [False] * 3
    stored = await sync_to_async(engine.history_rows)(session)
    full = _results([message for _, message in stored])
    assert len(full) == 6  # five trees and the submit_output result
    assert all(r.startswith("tree #") for r in full[:5])


@pytest.mark.django_db(transaction=True)
async def test_openai_session_call_stubs_older_results(user):
    session = await ConversationSession.objects.acreate(
        user=user, engine_type="openai", transport_type="api", status="active"
    )
    engine = OpenAIAPIEngine(config={"model": "gpt-test"})
    engine.tool_results_in_context = 1
    engine._client = FakeOpenAIClient(
        *[openai_tool("tree", {"n": i}, call_id=f"c{i}") for i in range(3)],
        openai_tool("submit_output", VALID_PLAN, call_id="s"),
    )
    spec = StructuredCallSpec(
        kind="designer", response_model=Plan, toolkits=[TreeToolkit()]
    )

    result = await run_structured_call(spec, "Draw it", session=session, engine=engine)

    assert result.ok
    messages = json.loads(json.dumps(engine._client.calls[-1]["messages"]))
    tools = [m for m in messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["c0", "c1", "c2"]
    assert tools[0]["content"].startswith("[tree result")
    assert tools[1]["content"].startswith("[tree result")
    assert tools[2]["content"].startswith("tree #2")


@pytest.mark.parametrize("exchange", [claude_exchange, openai_exchange])
def test_budget_keeps_the_newest_counts_and_keeps_request_copy(exchange):
    messages = [m for i in range(8) for m in exchange(f"t{i}", "x" * 1000)]
    before = copy.deepcopy(messages)
    stats = {}
    sent = trim_tool_results(messages, keep=3, max_chars=2000, stats=stats)
    assert stats["stubbed_results"] == 5
    assert _results(sent)[-3:] == ["x" * 1000] * 3
    assert all("trimmed from context" in r for r in _results(sent)[:-3])
    assert messages == before
    assert trim_tool_results(messages, keep=3, max_chars=8000) is messages


def _stubbed(messages):
    return sum(
        "trimmed from context" in r for r in _results(trim_tool_results(messages))
    )


def test_token_budget_comes_from_bot_then_settings_then_window(settings):
    from types import SimpleNamespace

    from django_ergo.conversation.tool_results import budget_chars

    messages = [m for i in range(6) for m in claude_exchange(f"t{i}", BIG)]
    settings.DJANGO_ERGO = {"TOOL_RESULTS_TOKENS": 1, "TOOL_RESULTS_IN_CONTEXT": 3}
    assert _stubbed(messages) == 3
    settings.DJANGO_ERGO = {"TOOL_RESULTS_TOKENS": 1, "TOOL_RESULTS_IN_CONTEXT": 5}
    assert _stubbed(messages) == 1
    # A token budget beats the character setting.
    settings.DJANGO_ERGO = {
        "TOOL_RESULTS_TOKENS": 10_000,
        "TOOL_RESULTS_CHARS_IN_CONTEXT": 0,
        "TOOL_RESULTS_IN_CONTEXT": 3,
    }
    assert _stubbed(messages) == 0
    # Neither set: 20% of the window, which keeps them all.
    settings.DJANGO_ERGO = {"TOOL_RESULTS_IN_CONTEXT": 3}
    assert _stubbed(messages) == 0
    assert budget_chars(SimpleNamespace(context_window=1_000_000)) == 800_000
    engine = SimpleNamespace(context_window=1_000_000, tool_results_tokens=100)
    assert budget_chars(engine) == 400
    assert trim_tool_results(messages, keep=-1) is messages


def test_a_result_that_does_not_fit_is_stubbed_with_everything_older():
    messages = [m for i in range(4) for m in claude_exchange(f"t{i}", "x" * 1000)]
    sent = trim_tool_results(messages, keep=0, max_chars=2500)
    assert _results(sent)[2:] == ["x" * 1000] * 2
    assert all("trimmed from context" in r for r in _results(sent)[:2])


@pytest.mark.django_db(transaction=True)
async def test_openai_errors_survive_trimming_and_are_not_sent_as_api_fields(user):
    from django_ergo.conversation.engine import SeededToolCall
    from django_ergo.conversation.images import prepare_messages
    from django_ergo.conversation.renderer import ConversationRenderer

    engine = OpenAIAPIEngine(config={})
    engine.tool_results_in_context = 0
    calls = [
        SeededToolCall("error", "read", {}, "ERROR /repo/file " + BIG, True),
        SeededToolCall("normal", "read", {}, BIG),
    ]
    raw = engine.tool_result_messages(
        [(c.tool_use_id, c.result, c.is_error) for c in calls]
    )
    trimmed = trim_tool_results(raw, keep=0)
    assert trimmed[0]["content"].startswith("ERROR /repo/file")
    assert trimmed[1]["content"].startswith("[tool result")
    sent = prepare_messages(trimmed, "openai")
    assert all("is_error" not in message for message in sent)
    assert raw[0]["is_error"]
    session = await ConversationSession.objects.acreate(user=user, engine_type="openai")
    await engine.append_tool_exchange(session, calls)
    sent = await sync_to_async(engine.reconstruct_messages)(session)
    assert sent[1]["content"].startswith("ERROR /repo/file")
    assert "is_error" not in sent[1]
    history = await sync_to_async(engine.history_rows)(session)
    digest = ConversationRenderer(detail="digest").render_messages(
        [message for _, message in history]
    )
    assert "tool_result #1 ERROR" in digest
    assert "ERROR /repo/file" in digest
