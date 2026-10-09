"""Tests for structured calls as conversation sessions."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from pydantic import BaseModel

from django_ergo.conversation import structured
from django_ergo.conversation.compaction import native_turn_start
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.structured import PreSeedCall
from django_ergo.conversation.structured import StructuredCallError
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import revise_structured_call
from django_ergo.conversation.structured import run_structured_call
from django_ergo.conversation.toolkit import Toolkit

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


@pytest.fixture
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

    result = await run_structured_call(spec, "Plan X", user=user, engine=engine)

    assert result.ok
    assert result.parsed == Plan(**VALID_PLAN)
    call = result.call
    assert call.kind == "planner"
    assert call.session_id is None
    assert call.response == VALID_PLAN
    assert call.turns_used == 2
    assert call.input_tokens == 20
    assert call.output_tokens == 10
    assert call.model_name == "claude-test"
    assert call.engine_type == "claude"
    assert toolkit.calls == [("lookup", {"q": "docs"})]
    # Standalone calls never create a session.
    assert not await ConversationSession.objects.aexists()

    first_call = engine._client.calls[0]
    assert first_call["system"] == "Plan carefully."
    tool_names = [t["name"] for t in first_call["tools"]]
    assert tool_names == ["lookup", "submit_output"]
    submit_schema = first_call["tools"][1]["input_schema"]
    assert submit_schema["required"] == ["title", "steps"]

    saved = await StructuredCall.objects.aget(pk=call.pk)
    # user, tool_use, tool_result, submit tool_use, submit result, response text
    assert [m["role"] for m in saved.transcript] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert json.loads(saved.transcript[-1]["content"][0]["text"]) == VALID_PLAN


async def test_invalid_output_is_returned_to_model(user):
    engine = claude_engine(
        claude_tool("submit_output", {"title": "Missing steps"}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    assert result.call.turns_used == 2
    error_block = result.call.transcript[2]["content"][0]
    assert error_block["is_error"] is True
    assert "failed validation" in error_block["content"]


async def test_list_sent_as_json_string_is_decoded(user):
    # Models sometimes encode a list argument as a JSON string; accept it
    # instead of spending a turn on a validation error.
    as_string = {**VALID_PLAN, "steps": json.dumps(VALID_PLAN["steps"])}
    engine = claude_engine(claude_tool("submit_output", as_string))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    assert result.call.turns_used == 1
    assert result.parsed.steps == VALID_PLAN["steps"]


def test_json_string_is_kept_for_str_fields():
    class Note(BaseModel):
        text: str
        tags: list[str] | None = None

    decoded = structured._decode_json_fields(Note, {"text": "[1]", "tags": '["a"]'})
    assert decoded == {"text": "[1]", "tags": ["a"]}
    assert (
        structured._decode_json_fields(Note, {"text": "x", "tags": "not json"})["tags"]
        == "not json"
    )


async def test_plain_text_answer_gets_correction(user):
    engine = claude_engine(
        claude_text("Here is the plan: ..."),
        claude_tool("submit_output", VALID_PLAN),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    second_call_messages = engine._client.calls[1]["messages"]
    assert "Call the submit_output tool now" in json.dumps(second_call_messages[-1])


async def test_revise_replays_standalone_transcript(user):
    engine = claude_engine(
        claude_tool("submit_output", VALID_PLAN),
        claude_tool(
            "submit_output",
            {"title": "Ship it", "steps": ["build"]},
            tool_id="toolu_2",
        ),
    )
    spec = StructuredCallSpec(
        kind="planner", system_prompt="Plan.", response_model=Plan
    )

    first = await run_structured_call(spec, "Plan", user=user, engine=engine)
    second = await revise_structured_call(
        spec, first.call, "Drop the test step", engine=engine
    )

    assert second.ok
    assert second.parsed.steps == ["build"]
    assert second.call.parent_id == first.call.id
    assert second.call.request == "Drop the test step"
    assert second.call.user_id == user.id
    replay = engine._client.calls[1]
    assert replay["system"] == "Plan."
    assert replay["messages"][0]["content"][0]["text"] == "Plan"
    assert replay["messages"][-1]["content"][0]["text"] == "Drop the test step"
    # The original is untouched; the revision carries the whole history.
    await first.call.arefresh_from_db()
    assert len(first.call.transcript) == 4
    assert len(second.call.transcript) == 8
    revisions = [c.response async for c in first.call.revisions.all()]
    assert revisions == [{"title": "Ship it", "steps": ["build"]}]


async def test_revise_refuses_another_engine(user):
    engine = claude_engine(claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)
    first = await run_structured_call(spec, "Plan", user=user, engine=engine)
    other = OpenAIAPIEngine(config={"model": "gpt-test"})
    with pytest.raises(StructuredCallError, match="can't be replayed"):
        await revise_structured_call(spec, first.call, "again", engine=other)


async def test_tool_errors_and_unknown_tools_go_back_to_model(user):
    engine = claude_engine(
        claude_tool("explode", {}),
        claude_tool("nope", {}, tool_id="toolu_2"),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_3"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[LookupToolkit()]
    )

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    transcript = result.call.transcript
    assert transcript[2]["content"][0]["content"] == "lookup backend down"
    assert transcript[4]["content"][0]["content"] == "Unknown tool: nope"


# ---------------------------------------------------------------------------
# Calls inside a conversation session
# ---------------------------------------------------------------------------


async def _chat_session(user, engine_type="claude"):
    return await ConversationSession.objects.acreate(
        user=user,
        engine_type=engine_type,
        transport_type="api",
        status="active",
        system_prompt="You are helpful.",
    )


async def test_session_mixes_chat_and_structured_turns(user):
    from django_ergo.conversation.runner import run_conversation_turn

    session = await _chat_session(user)
    engine = claude_engine(
        claude_text("Sure, what are we shipping?"),
        claude_tool("submit_output", VALID_PLAN),
        claude_text("Done, the plan has two steps."),
    )
    spec = StructuredCallSpec(
        kind="planner", system_prompt="Return a plan.", response_model=Plan
    )

    _ = [e async for e in run_conversation_turn(engine, session, "Let's plan")]
    result = await run_structured_call(spec, "Plan it", session=session, engine=engine)
    _ = [e async for e in run_conversation_turn(engine, session, "Summarize")]

    assert result.ok
    call = result.call
    assert call.session_id == session.id
    assert call.transcript == []
    assert (call.first_sequence, call.last_sequence) == (2, 5)
    structured_request = engine._client.calls[1]
    assert structured_request["system"] == "You are helpful."
    assert "Return a plan." in structured_request["messages"][2]["content"][0]["text"]
    assert structured_request["messages"][0]["content"][0]["text"] == "Let's plan"
    # The next chat turn sees the response as the structured turn's output
    # call; its stored plain-text copy isn't sent back to the model.
    after = engine._client.calls[2]
    assert after["system"] == "You are helpful."
    assert "submit_output" not in [t["name"] for t in after.get("tools", [])]
    output_call = after["messages"][-3]["content"][-1]
    assert (output_call["name"], output_call["input"]) == ("submit_output", VALID_PLAN)
    assert after["messages"][-2]["content"][0]["content"] == "Output accepted."
    assert json.dumps(VALID_PLAN, indent=2) not in json.dumps(after["messages"])
    assert engine.ephemeral_context == ""


async def test_revise_in_session_adds_a_turn(user):
    session = await _chat_session(user)
    engine = claude_engine(
        claude_tool("submit_output", VALID_PLAN),
        claude_tool("submit_output", {"title": "Ship", "steps": ["build"]}),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    first = await run_structured_call(spec, "Plan", session=session, engine=engine)
    second = await revise_structured_call(spec, first.call, "Shorter", engine=engine)

    assert second.call.session_id == session.id
    assert second.call.parent_id == first.call.id
    assert second.call.first_sequence == first.call.last_sequence + 1
    calls = [c.kind async for c in session.structured_calls.all()]
    assert calls == ["planner", "planner"]
    history = await _history(engine, session)
    assert history[-1]["content"][0]["content"] == "Output accepted."
    assert history[-3]["content"][0]["text"] == "Shorter"


# ---------------------------------------------------------------------------
# output_parser (text) mode
# ---------------------------------------------------------------------------


async def test_text_mode_parses_json_with_correction(user):
    engine = claude_engine(
        claude_text("not json"),
        claude_text('{"answer": 42}'),
    )
    spec = StructuredCallSpec(kind="answer")

    result = await run_structured_call(spec, "Q", user=user, engine=engine)

    assert result.ok
    assert result.parsed == {"answer": 42}
    assert result.call.response == {"answer": 42}
    assert "failed validation" in json.dumps(engine._client.calls[1]["messages"][-1])


async def test_custom_output_parser(user):
    engine = claude_engine(claude_text("  yes "))
    spec = StructuredCallSpec(kind="yesno", output_parser=lambda t: t.strip() == "yes")

    result = await run_structured_call(spec, "Q", user=user, engine=engine)

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

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.status == StructuredCallStatus.TURN_LIMITED
    assert result.parsed is None
    assert result.call.turns_used == 2


async def test_wrap_up_spends_the_last_turn_on_the_answer(user):
    engine = claude_engine(
        claude_tool("lookup", {"q": "a"}),
        claude_tool("lookup", {"q": "b"}, tool_id="toolu_2"),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_3"),
    )
    spec = StructuredCallSpec(
        kind="planner",
        system_prompt="Plan carefully.",
        response_model=Plan,
        toolkits=[LookupToolkit()],
        max_turns=3,
        wrap_up=True,
    )

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    assert result.parsed == Plan(**VALID_PLAN)
    calls = engine._client.calls
    tool_names = [[t["name"] for t in c["tools"]] for c in calls]
    assert "lookup" in tool_names[0]
    assert tool_names[2] == ["submit_output"]
    assert "last step" in json.dumps(calls[2]["system"])
    assert "last step" not in json.dumps(calls[0]["system"])


async def test_max_tokens_stop_fails(user):
    engine = claude_engine(claude_text("truncated", stop="max_tokens"))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.status == StructuredCallStatus.FAILED
    assert "max_tokens" in result.error


async def test_truncated_tool_call_is_not_run(user):
    # A reply cut off at max_tokens can carry a tool call with partial ({})
    # arguments; it must come back as an error, not run.
    truncated = claude_tool("lookup", {})
    truncated.stop_reason = "max_tokens"
    toolkit = LookupToolkit()
    engine = claude_engine(truncated, claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    assert toolkit.calls == []
    sent = json.dumps(engine._client.calls[1]["messages"][-1])
    assert "output token limit" in sent


async def test_plain_text_nudge_quotes_the_answer(user):
    engine = claude_engine(
        claude_text("Draft ae62abde is created and quoted."),
        claude_tool("submit_output", VALID_PLAN),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    await run_structured_call(spec, "Plan", user=user, engine=engine)

    nudge = json.dumps(engine._client.calls[1]["messages"][-1])
    assert "latest message" in nudge
    assert "Draft ae62abde is created and quoted." in nudge


async def test_empty_response_is_asked_to_carry_on_not_to_wrap_up(user):
    # Asking for a "final answer" after an empty response made the model drop
    # work it was in the middle of.
    empty = SimpleNamespace(content=[], stop_reason="end_turn", usage=_usage())
    engine = claude_engine(empty, claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    await run_structured_call(spec, "Plan", user=user, engine=engine)

    nudge = json.dumps(engine._client.calls[1]["messages"][-1])
    assert "Carry on with the latest message" in nudge
    assert "final answer" not in nudge


def test_a_message_after_an_accepted_answer_starts_a_turn():
    # The answer's plain-text copy isn't sent to the model, so the next message
    # follows the output tool's result directly; it still starts a new turn.
    claude = [
        {"role": "user", "content": [{"type": "text", "text": "Plan"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "Output accepted.",
                }
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "Shorter"}]},
    ]
    assert native_turn_start(claude) == 3
    openai = [
        {"role": "user", "content": "Plan"},
        {"role": "assistant", "tool_calls": [{"id": "t1"}, {"id": "t2"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "Output accepted."},
        {"role": "tool", "tool_call_id": "t2", "content": "42"},
        {"role": "user", "content": "Shorter"},
    ]
    assert native_turn_start(openai) == 4
    # Results of ordinary tool work still continue the turn they belong to.
    claude[2]["content"][0]["content"] = "42"
    assert native_turn_start(claude) == 0


class APIConnectionError(Exception):
    pass


class AuthenticationError(Exception):
    pass


async def test_transient_errors_are_retried_without_duplicating_history(
    user, monkeypatch
):
    monkeypatch.setattr(structured, "RETRY_DELAYS", (0, 0))
    session = await _chat_session(user)
    engine = claude_engine(
        APIConnectionError("reset"),
        claude_tool("submit_output", VALID_PLAN),
        APIConnectionError("reset"),
        claude_tool("submit_output", VALID_PLAN),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    standalone = await run_structured_call(spec, "Plan", user=user, engine=engine)
    in_session = await run_structured_call(spec, "Plan", session=session, engine=engine)

    assert standalone.ok
    assert in_session.ok
    assert len(engine._client.calls) == 4
    assert [m["role"] for m in standalone.call.transcript] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    history = await _history(engine, session)
    assert [m["role"] for m in history] == ["user", "assistant", "user"]


async def test_non_transient_error_is_recorded(user, monkeypatch):
    monkeypatch.setattr(structured, "RETRY_DELAYS", (0, 0))
    engine = claude_engine(AuthenticationError("bad key"))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.status == StructuredCallStatus.FAILED
    assert result.call.error_category == "auth"
    assert "bad key" in result.error
    assert len(engine._client.calls) == 1


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

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    sent = engine._client.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    [seeded] = sent[1]["content"]
    assert re.fullmatch(r"preseed_[0-9a-f]{8}_0", seeded.pop("id"))
    assert seeded == {"type": "tool_use", "name": "get_ticket", "input": {"id": 7}}
    assert sent[2]["content"][0]["content"] == "{'ticket': 7}"


async def test_openai_engine_round_trip(user):
    engine = OpenAIAPIEngine(config={"model": "gpt-test"})
    engine._client = FakeOpenAIClient(openai_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(
        kind="planner", system_prompt="Plan.", response_model=Plan
    )

    result = await run_structured_call(spec, "Plan", user=user, engine=engine)

    assert result.ok
    assert result.call.engine_type == "openai"
    call = engine._client.calls[0]
    assert call["messages"][0] == {
        "role": "system",
        "content": "Plan.",
    }
    tool = call["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "submit_output"
    assert tool["function"]["parameters"]["required"] == ["title", "steps"]
    assert [m["role"] for m in result.call.transcript] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert result.call.input_tokens == 7


def test_engine_spec_gets_max_tokens_and_rows_hold_no_config():
    spec = StructuredCallSpec(kind="planner", response_model=Plan, max_tokens=99)
    engine = structured._engine(
        spec, EngineSpec("claude", "api", {"model": "m", "api_key": "sk-secret"})
    )
    assert engine.max_tokens == 99
    stored = {f.name for f in StructuredCall._meta.fields}
    assert not {"config", "engine_config"} & stored


async def _history(engine, session):
    from asgiref.sync import sync_to_async

    return await sync_to_async(engine.reconstruct_messages)(session)


class ApprovalToolkit(Toolkit):
    def __init__(self):
        self.ran = []

    def has_tool(self, tool_name):
        return tool_name == "delete_all"

    def requires_approval(self, tool_name):
        return True

    def execute_tool(self, tool_name, arguments):
        self.ran.append(arguments)
        return "deleted"

    def get_tools_schema(self, adapter):
        return [{"name": "delete_all", "input_schema": {"type": "object"}}]

    def render_overview(self):
        return ""


async def test_approval_pauses_and_resumes_standalone_call(user):
    toolkit = ApprovalToolkit()
    engine = claude_engine(
        claude_tool("delete_all", {"scope": "tmp"}, tool_id="d1"),
        claude_tool("submit_output", VALID_PLAN, tool_id="s1"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])

    paused = await run_structured_call(
        spec, "Clean up", user=user, engine=engine, allow_approvals=True
    )

    assert paused.status == StructuredCallStatus.AWAITING_APPROVAL
    assert [a.tool_name for a in paused.approvals] == ["delete_all"]
    assert toolkit.ran == []
    saved = await StructuredCall.objects.aget(pk=paused.call.pk)
    assert saved.metadata["pending_approvals"][0]["id"] == "d1"

    done = await structured.resume_structured_call(
        spec, saved, {"d1": True}, engine=engine
    )

    assert done.ok
    assert toolkit.ran == [{"scope": "tmp"}]
    assert "pending_approvals" not in done.call.metadata
    sent = engine._client.calls[1]["messages"][-1]["content"][0]
    assert sent == {
        "type": "tool_result",
        "tool_use_id": "d1",
        "content": "deleted",
        "is_error": False,
    }
    with pytest.raises(StructuredCallError, match="not waiting"):
        await structured.resume_structured_call(spec, done.call, {}, engine=engine)


async def test_approval_tools_are_refused_without_allow_approvals(user):
    toolkit = ApprovalToolkit()
    engine = claude_engine(
        claude_tool("delete_all", {}, tool_id="d1"),
        claude_tool("submit_output", VALID_PLAN, tool_id="s1"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])

    result = await run_structured_call(spec, "Clean up", user=user, engine=engine)

    assert result.ok
    assert toolkit.ran == []
    refused = result.call.transcript[2]["content"][0]
    assert refused["is_error"]
    assert "requires approval" in refused["content"]


async def test_openai_calls_record_cache_reasoning_and_per_request_cost(user):
    response = openai_tool("submit_output", VALID_PLAN)
    response.usage = SimpleNamespace(
        prompt_tokens=1_000_000,
        completion_tokens=100_000,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=600_000, cache_write_tokens=0
        ),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=40_000),
    )
    engine = OpenAIAPIEngine(config={"model": "gpt-6-luna"})
    engine._client = FakeOpenAIClient(response)
    spec = StructuredCallSpec(
        kind="planner", system_prompt="Plan.", response_model=Plan
    )

    call = (await run_structured_call(spec, "Plan", user=user, engine=engine)).call

    assert (
        call.input_tokens,
        call.cache_read_input_tokens,
        call.output_tokens,
        call.reasoning_tokens,
    ) == (
        400_000,
        600_000,
        100_000,
        40_000,
    )
    # gpt-6-luna: 400K input $0.04, 600K cached $0.006, 100K output $0.05; the 1M-token
    # prompt is past the 272K long-context line: 2x input and cache, 1.5x output.
    assert float(call.cost_usd) == pytest.approx(0.04 * 2 + 0.006 * 2 + 0.05 * 1.5)
    assert call.metadata["cost_parts"]["cache_read"] == pytest.approx(0.012)


# ---------------------------------------------------------------------------
# Turn control: steering and stopping between steps
# ---------------------------------------------------------------------------


class ScriptedControl:
    """Hands out one TurnSignal per check, then empty ones."""

    def __init__(self, *signals):
        self.signals = list(signals)
        self.checks = 0

    async def check(self):
        self.checks += 1
        return self.signals.pop(0) if self.signals else structured.TurnSignal()


async def test_steering_message_reaches_the_next_step(user):
    toolkit = LookupToolkit()
    engine = claude_engine(
        claude_tool("lookup", {"q": "docs"}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])
    control = ScriptedControl(
        structured.TurnSignal(),  # before the first call
        structured.TurnSignal(messages=[structured.SteeringMessage("Only two steps")]),
    )

    result = await run_structured_call(
        spec, "Plan X", user=user, engine=engine, control=control
    )

    assert result.ok
    assert control.checks == 2
    second = engine._client.calls[1]["messages"]
    assert second[-2]["content"][0]["type"] == "tool_result"
    assert second[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "Only two steps"}],
    }
    # The steering message stays in the call's history.
    assert "Only two steps" in json.dumps(result.call.transcript)


async def test_stop_ends_the_call_at_the_next_step(user):
    toolkit = LookupToolkit()
    engine = claude_engine(
        claude_tool("lookup", {"q": "docs"}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])
    control = ScriptedControl(structured.TurnSignal(), structured.TurnSignal(stop=True))

    result = await run_structured_call(
        spec, "Plan X", user=user, engine=engine, control=control
    )

    assert result.status == StructuredCallStatus.STOPPED
    assert not result.ok
    assert result.error == "Stopped by the user"
    # The tool that was running finished; no further model call was made.
    assert toolkit.calls == [("lookup", {"q": "docs"})]
    assert len(engine._client.calls) == 1
    saved = await StructuredCall.objects.aget(pk=result.call.pk)
    assert saved.status == "stopped"
    assert saved.turns_used == 1
    assert saved.transcript[-1]["content"][0]["type"] == "tool_result"


async def test_stop_works_after_an_approval(user):
    toolkit = ApprovalToolkit()
    engine = claude_engine(claude_tool("delete_all", {}, tool_id="d1"))
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])
    paused = await run_structured_call(
        spec, "Clean up", user=user, engine=engine, allow_approvals=True
    )

    done = await structured.resume_structured_call(
        spec,
        paused.call,
        {"d1": True},
        engine=engine,
        control=ScriptedControl(structured.TurnSignal(stop=True)),
    )

    assert done.status == StructuredCallStatus.STOPPED
    assert toolkit.ran == [{}]
    assert len(engine._client.calls) == 1


async def test_steering_in_a_window_session_keeps_the_turns_tool_work(user):
    session = await _chat_session(user)
    session.compaction_config = {"native_history": "turn"}
    await session.asave()
    engine = claude_engine(
        claude_tool("lookup", {"q": "docs"}),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[LookupToolkit()]
    )
    control = ScriptedControl(
        structured.TurnSignal(),
        structured.TurnSignal(messages=[structured.SteeringMessage("Shorter")]),
    )

    result = await run_structured_call(
        spec, "Plan X", session=session, engine=engine, control=control
    )

    assert result.ok
    sent = engine._client.calls[1]["messages"]
    assert sent[0]["content"][0]["text"] == "Plan X"
    assert sent[-1]["content"][0]["text"] == "Shorter"


async def test_a_session_moves_between_engines_with_its_history(user):
    session = await _chat_session(user, engine_type="openai")
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[LookupToolkit()]
    )
    openai = OpenAIAPIEngine(config={"model": "gpt-test"})
    openai._client = FakeOpenAIClient(
        openai_tool("lookup", {"q": "docs"}),
        openai_tool("submit_output", VALID_PLAN, call_id="call_2"),
        openai_tool("submit_output", VALID_PLAN, call_id="call_3"),
    )
    claude = claude_engine(
        claude_tool("lookup", {"q": "more"}, tool_id="toolu_1"),
        claude_tool("submit_output", VALID_PLAN, tool_id="toolu_2"),
    )

    for engine, text in ((openai, "Plan"), (claude, "Again"), (openai, "Once more")):
        result = await run_structured_call(spec, text, session=session, engine=engine)
        assert result.ok
        await session.arefresh_from_db()
        assert session.engine_type == engine.engine_type

    # Claude gets OpenAI's tool calls as tool_use blocks, each answered.
    sent = claude._client.calls[0]
    assert sent["system"] == "You are helpful."
    calls = [
        b for m in sent["messages"] for b in m["content"] if b["type"] == "tool_use"
    ]
    results = [
        b["tool_use_id"]
        for m in sent["messages"]
        if m["role"] == "user"
        for b in m["content"]
        if b["type"] == "tool_result"
    ]
    assert [c["id"] for c in calls] == ["call_1", "call_2"]
    assert results == ["call_1", "call_2"]
    assert calls[0]["input"] == {"q": "docs"}

    # And OpenAI gets Claude's back as tool_calls with tool messages.
    messages = openai._client.calls[2]["messages"]
    assert messages[0] == {"role": "system", "content": "You are helpful."}
    called = [c["id"] for m in messages for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert called == answered == ["call_1", "call_2", "toolu_1", "toolu_2"]
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "Plan",
        "Again",
        "Once more",
    ]
