"""Tests for structured calls as conversation sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django_ergo.conversation import structured
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import SessionMode
from django_ergo.conversation.models import StructuredOutputStatus
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.structured import PreSeedCall
from django_ergo.conversation.structured import StructuredCallError
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import StructuredSession
from django_ergo.conversation.structured import run_structured_call
from django_ergo.conversation.toolkit import Toolkit
from pydantic import BaseModel

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


class Plan(BaseModel):
    title: str
    steps: list[str]


# ---------------------------------------------------------------------------
# Fake SDK clients
# ---------------------------------------------------------------------------


def _usage(inp=10, out=5, cache_create=0, cache_read=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=cache_create,
        cache_read_input_tokens=cache_read,
    )


def claude_text(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=_usage(),
    )


def claude_tool(name, tool_input, tool_id="toolu_1", text=None):
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    blocks.append(
        SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)
    )
    return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=_usage())


class FakeClaudeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def openai_tool(name, args, call_id="call_1"):
    tc = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )
    tc.model_dump = lambda: {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tc]),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )


class FakeOpenAIClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def claude_engine(*responses):
    engine = ClaudeAPIEngine(config={"model": "claude-test", "max_tokens": 512})
    engine._client = FakeClaudeClient(*responses)
    return engine


class LookupToolkit(Toolkit):
    def __init__(self):
        self.calls = []

    def has_tool(self, tool_name):
        return tool_name in {"lookup", "explode"}

    def execute_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "explode":
            msg = "lookup backend down"
            raise RuntimeError(msg)
        return f"found {arguments['q']}"

    def get_tools_schema(self, adapter):
        return [{"name": "lookup", "input_schema": {"type": "object"}}]

    def render_overview(self):
        return ""


@pytest.fixture()
def user():
    return User.objects.create_user(username="structured", password="x")


VALID_PLAN = {"title": "Ship it", "steps": ["build", "test"]}


# ---------------------------------------------------------------------------
# response_model mode
# ---------------------------------------------------------------------------


async def test_tool_then_submit_completes(user):
    toolkit = LookupToolkit()
    engine = claude_engine(
        claude_tool("lookup", {"q": "docs"}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(
        kind="planner",
        system_prompt="Plan carefully.",
        response_model=Plan,
        toolkits=[toolkit],
    )

    result = await run_structured_call(spec, user=user, message="Plan X", engine=engine)

    assert result.ok
    assert result.parsed == Plan(**VALID_PLAN)
    assert result.record.output == VALID_PLAN
    assert result.record.turns_used == 2
    assert result.record.input_tokens == 20
    assert result.record.output_tokens == 10
    assert result.record.model_name == "claude-test"
    assert toolkit.calls == [("lookup", {"q": "docs"})]

    session = result.session
    assert session.mode == SessionMode.STRUCTURED
    assert session.kind == "planner"
    first_call = engine._client.calls[0]
    assert first_call["system"] == "Plan carefully."
    tool_names = [t["name"] for t in first_call["tools"]]
    assert tool_names == ["lookup", "submit_output"]
    submit_schema = first_call["tools"][1]["input_schema"]
    assert submit_schema["required"] == ["title", "steps"]

    history = await _history(engine, session)
    # user, tool_use, tool_result, submit tool_use, submit tool_result
    assert [m["role"] for m in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert history[-1]["content"][0]["type"] == "tool_result"
    assert result.record.first_sequence == 0
    assert result.record.last_sequence == 4


async def test_invalid_output_is_returned_to_model(user):
    engine = claude_engine(
        claude_tool("submit_output", {"title": "Missing steps"}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.ok
    assert result.record.turns_used == 2
    history = await _history(engine, result.session)
    error_block = history[2]["content"][0]
    assert error_block["is_error"] is True
    assert "failed validation" in error_block["content"]


async def test_plain_text_answer_gets_correction(user):
    engine = claude_engine(
        claude_text("Here is the plan: ..."),
        claude_tool("submit_output", VALID_PLAN),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.ok
    second_call_messages = engine._client.calls[1]["messages"]
    assert "must call the submit_output tool" in json.dumps(second_call_messages[-1])


async def test_follow_up_message_produces_second_output(user):
    engine = claude_engine(
        claude_tool("submit_output", VALID_PLAN),
        claude_tool(
            "submit_output",
            {"title": "Ship it", "steps": ["build"]},
            tool_id="toolu_2",
        ),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)
    handler = StructuredSession(spec, engine=engine)

    first = await handler.start(user=user, message="Plan")
    second = await handler.send(first.session, "Drop the test step")

    assert second.ok
    assert second.record.sequence == 1
    assert second.record.request == "Drop the test step"
    assert second.parsed.steps == ["build"]
    assert second.record.first_sequence == first.record.last_sequence + 1
    follow_up_messages = engine._client.calls[1]["messages"]
    assert follow_up_messages[0]["content"][0]["text"] == "Plan"
    assert follow_up_messages[-1]["content"][0]["text"] == "Drop the test step"
    outputs = [o.output async for o in first.session.structured_outputs.all()]
    assert outputs == [VALID_PLAN, {"title": "Ship it", "steps": ["build"]}]


async def test_tool_errors_and_unknown_tools_go_back_to_model(user):
    engine = claude_engine(
        claude_tool("explode", {}),
        claude_tool("nope", {}, tool_id="toolu_2"),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_3"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[LookupToolkit()]
    )

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.ok
    history = await _history(engine, result.session)
    assert history[2]["content"][0]["content"] == "lookup backend down"
    assert history[4]["content"][0]["content"] == "Unknown tool: nope"


# ---------------------------------------------------------------------------
# output_parser (text) mode
# ---------------------------------------------------------------------------


async def test_text_mode_parses_json_with_correction(user):
    engine = claude_engine(
        claude_text("not json"),
        claude_text('{"answer": 42}'),
    )
    spec = StructuredCallSpec(kind="answer")

    result = await run_structured_call(spec, user=user, message="Q", engine=engine)

    assert result.ok
    assert result.parsed == {"answer": 42}
    assert result.record.output == {"answer": 42}
    assert "failed validation" in json.dumps(engine._client.calls[1]["messages"][-1])


async def test_custom_output_parser(user):
    engine = claude_engine(claude_text("  yes "))
    spec = StructuredCallSpec(kind="yesno", output_parser=lambda t: t.strip() == "yes")

    result = await run_structured_call(spec, user=user, message="Q", engine=engine)

    assert result.parsed is True


def test_spec_rejects_both_output_modes():
    with pytest.raises(StructuredCallError):
        StructuredCallSpec(kind="x", response_model=Plan, output_parser=str)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_turn_limit(user):
    engine = claude_engine(claude_text("nope"), claude_text("still nope"))
    spec = StructuredCallSpec(kind="planner", response_model=Plan, max_turns=2)

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.status == StructuredOutputStatus.TURN_LIMITED
    assert result.parsed is None
    assert result.record.turns_used == 2


async def test_max_tokens_stop_fails(user):
    engine = claude_engine(claude_text("truncated", stop="max_tokens"))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.status == StructuredOutputStatus.FAILED
    assert "max_tokens" in result.error


class APIConnectionError(Exception):  # noqa: N818 — mirrors the SDK class name
    pass


class AuthenticationError(Exception):  # noqa: N818
    pass


async def test_transient_errors_are_retried_without_duplicating_history(
    user, monkeypatch
):
    monkeypatch.setattr(structured, "RETRY_DELAYS", (0, 0))
    engine = claude_engine(
        APIConnectionError("reset"),
        claude_tool("submit_output", VALID_PLAN),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.ok
    assert len(engine._client.calls) == 2
    history = await _history(engine, result.session)
    assert [m["role"] for m in history] == ["user", "assistant", "user"]


async def test_non_transient_error_is_recorded(user, monkeypatch):
    monkeypatch.setattr(structured, "RETRY_DELAYS", (0, 0))
    engine = claude_engine(AuthenticationError("bad key"))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.status == StructuredOutputStatus.FAILED
    assert result.record.error_category == "auth"
    assert "bad key" in result.error
    assert len(engine._client.calls) == 1


async def test_send_rejects_chat_sessions(user):
    session = await ConversationSession.objects.acreate(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    handler = StructuredSession(StructuredCallSpec(kind="x"), engine=claude_engine())
    with pytest.raises(StructuredCallError):
        await handler.send(session, "hi")


# ---------------------------------------------------------------------------
# Pre-seeds, engines, metadata
# ---------------------------------------------------------------------------


async def test_pre_seeds_are_written_before_first_call(user):
    def boom(_):
        msg = "unavailable"
        raise RuntimeError(msg)

    engine = claude_engine(claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(
        kind="planner",
        response_model=Plan,
        pre_seeds=[
            PreSeedCall("get_ticket", {"id": 7}, lambda args: {"ticket": args["id"]}),
            PreSeedCall("broken", {}, boom),
        ],
    )

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.ok
    sent = engine._client.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [
        {
            "type": "tool_use",
            "id": "preseed_0",
            "name": "get_ticket",
            "input": {"id": 7},
        }
    ]
    assert sent[2]["content"][0]["content"] == "{'ticket': 7}"


async def test_openai_engine_round_trip(user):
    engine = OpenAIAPIEngine(config={"model": "gpt-test"})
    engine._client = FakeOpenAIClient(openai_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(
        kind="planner", system_prompt="Plan.", response_model=Plan
    )

    result = await run_structured_call(spec, user=user, message="Plan", engine=engine)

    assert result.ok
    assert result.session.engine_type == "openai"
    call = engine._client.calls[0]
    assert call["messages"][0] == {
        "role": "system",
        "content": "Plan.",
    }
    tool = call["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "submit_output"
    assert tool["function"]["parameters"]["required"] == ["title", "steps"]
    history = await _history(engine, result.session)
    assert history[-1]["role"] == "tool"
    assert result.record.input_tokens == 7


async def test_credentials_are_not_stored_in_metadata(user, monkeypatch):
    engine = claude_engine(claude_tool("submit_output", VALID_PLAN))
    handler = StructuredSession(
        StructuredCallSpec(kind="planner", response_model=Plan, max_tokens=99),
        engine=engine,
        engine_spec=EngineSpec("claude", "api", {"model": "m", "api_key": "sk-secret"}),
    )

    result = await handler.start(user=user, message="Plan", metadata={"ticket": 1})

    stored = result.session.metadata
    assert stored["ticket"] == 1
    assert stored["structured"]["engine_config"] == {"model": "m", "max_tokens": 99}
    assert stored["structured"]["response_model"].endswith(".Plan")
    assert "sk-secret" not in json.dumps(stored)


async def _history(engine, session):
    from asgiref.sync import sync_to_async

    return await sync_to_async(engine.reconstruct_messages)(session)
