"""The opt-in per-turn budget (conversation.budget): each limit, the allowlist, the
refused starts and sends, no retries, and that a call without a budget is unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.bots import agents
from django_ergo.bots import background
from django_ergo.bots import messaging
from django_ergo.bots import orchestrator
from django_ergo.bots import wakes
from django_ergo.bots import workers
from django_ergo.conversation import budget as budget_module
from django_ergo.conversation import structured
from django_ergo.conversation.budget import STOPPED_PREFIX
from django_ergo.conversation.budget import BudgetExceeded
from django_ergo.conversation.budget import TurnBudget
from django_ergo.conversation.budget import apply_budget
from django_ergo.conversation.budget import forbid
from django_ergo.conversation.budget import from_metadata
from django_ergo.conversation.chat_reply import chat_reply_spec
from django_ergo.conversation.engines.claude_api import ClaudeAPIEngine
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.structured import PreSeedCall
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import run_structured_call
from django_ergo.conversation.toolkit import Toolkit

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)

LIMITS = {
    "max_turns": 6,
    "max_input_tokens": 100_000,
    "max_output_tokens": 4_000,
    "deadline_seconds": 30,
    "allowed_tools": ["lookup"],
}


def budget(**changes) -> TurnBudget:
    return TurnBudget.from_dict({**LIMITS, **changes})


def usage(inp=10, out=5):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def call_tool(name, tool_input=None, *, tool_id, inp=10, out=5):
    block = SimpleNamespace(
        type="tool_use", id=tool_id, name=name, input=tool_input or {}
    )
    return SimpleNamespace(
        content=[block], stop_reason="tool_use", usage=usage(inp, out)
    )


def reply(text="done", **kwargs):
    return call_tool(
        "send_reply", {"type": "message", "text": text}, tool_id="toolu_reply", **kwargs
    )


class Client:
    """A fake Anthropic client that records the options a request was sent with."""

    def __init__(self, *responses, delay=0.0):
        self.responses = list(responses)
        self.delay = delay
        self.calls = []
        self.options = []
        self.messages = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    async def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        if self.delay:
            await asyncio.sleep(self.delay)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def engine_for(client, model="claude-test"):
    engine = ClaudeAPIEngine(config={"model": model, "max_tokens": 512})
    engine._client = client
    return engine


class Reader(Toolkit):
    """``lookup`` is the read-only tool; ``explode`` exists but is not allowed."""

    def __init__(self, pause=0.0):
        self.pause = pause
        self.calls = []

    def has_tool(self, tool_name):
        return tool_name in {"lookup", "explode"}

    def execute_tool(self, tool_name, arguments):
        self.calls.append(tool_name)
        if self.pause:
            time.sleep(self.pause)
        return "found it"

    def get_tools_schema(self, adapter):
        return [
            {"name": name, "input_schema": {"type": "object"}}
            for name in ("lookup", "explode")
        ]

    def render_overview(self):
        return ""


@pytest.fixture
def user():
    return User.objects.create_user(username="budgeted", password="x")


@pytest.fixture
async def session(user):
    return await ConversationSession.objects.acreate(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        system_prompt="You are helpful.",
    )


async def run(client, *, toolkit=None, limits=None, session=None, user=None, **kw):
    spec = chat_reply_spec([toolkit or Reader()])
    if limits is not None:
        apply_budget(spec, limits)
    return await run_structured_call(
        spec,
        "What is in the pantry?",
        user=user,
        session=session,
        engine=engine_for(client),
        **kw,
    )


def stopped_by(result):
    return result.call.metadata["budget"]["stopped_by"]


# -- the limits ------------------------------------------------------------------


async def test_the_model_turn_limit_ends_the_call_as_a_budget_failure(user):
    client = Client(*(call_tool("lookup", tool_id=f"toolu_{i}") for i in range(5)))

    result = await run(client, limits=budget(max_turns=3), user=user)

    call = result.call
    assert len(client.calls) == 3
    assert (call.status, call.error_category) == (StructuredCallStatus.FAILED, "budget")
    assert call.error.startswith(STOPPED_PREFIX)
    assert "reached the limit of 3 model turns" in call.error
    assert stopped_by(result) == "max_turns"
    assert call.metadata["budget"]["used"]["model_turns"] == 3


async def test_every_request_is_held_to_the_output_tokens_left(user):
    client = Client(
        call_tool("lookup", tool_id="toolu_1", out=5),
        call_tool("lookup", tool_id="toolu_2", out=7),
        reply(),
    )

    result = await run(client, limits=budget(max_output_tokens=12), user=user)

    # The engine's own max_tokens is 512: the budget cuts it to what is left.
    assert [c["max_tokens"] for c in client.calls] == [12, 7]
    assert len(client.calls) == 2  # nothing left for a third request
    assert stopped_by(result) == "output_tokens"
    assert result.call.output_tokens == 12


async def test_an_oversized_request_is_refused_before_it_is_sent(user):
    # 1000 tokens of overhead are always reserved, so this leaves ~500 bytes.
    spec = chat_reply_spec([Reader()])
    apply_budget(spec, budget(max_input_tokens=1_500))
    client = Client(reply())

    result = await run_structured_call(
        spec, "x" * 3_000, user=user, engine=engine_for(client)
    )

    assert client.calls == []
    assert stopped_by(result) == "input_tokens"
    assert result.call.metadata["budget"]["used"]["model_turns"] == 0
    assert "over the 1500 left" in result.call.error


async def test_provider_reported_input_tokens_count_toward_the_limit(user):
    client = Client(
        call_tool("lookup", tool_id="toolu_1", inp=7_500),
        reply(),
    )

    result = await run(client, limits=budget(max_input_tokens=8_000), user=user)

    # 500 tokens are left, less than the reserved overhead: no second request.
    assert len(client.calls) == 1
    assert stopped_by(result) == "input_tokens"
    assert result.call.metadata["budget"]["used"]["input_tokens"] == 7_500


async def test_a_request_that_overran_the_input_limit_is_not_acted_on(user, session):
    toolkit = Reader()
    client = Client(call_tool("lookup", tool_id="toolu_1", inp=9_000), reply())

    result = await run(
        client, toolkit=toolkit, limits=budget(max_input_tokens=8_000), session=session
    )

    assert toolkit.calls == []  # the tool call was past the budget
    assert stopped_by(result) == "input_tokens"
    # The chat keeps a valid history: the tool call is answered, as not run.
    history = await sync_to_async(engine_for(client).reconstruct_messages)(session)
    use = next(b for m in history for b in m["content"] if b.get("type") == "tool_use")
    answer = next(
        b
        for m in history
        for b in m["content"]
        if b.get("type") == "tool_result" and b["tool_use_id"] == use["id"]
    )
    assert answer["is_error"] is True
    assert answer["content"].startswith("Not run:")


async def test_the_deadline_is_every_requests_timeout_and_ends_a_slow_one(
    user, monkeypatch
):
    monkeypatch.setattr(budget_module, "DEADLINE_GRACE_SECONDS", 0.05)
    client = Client(reply(), delay=5)

    started = time.monotonic()
    result = await run(client, limits=budget(deadline_seconds=0.2), user=user)

    assert time.monotonic() - started < 2
    assert stopped_by(result) == "deadline"
    assert 0 < client.options[0]["timeout"] <= 0.2
    assert result.call.status == StructuredCallStatus.FAILED


async def test_a_provider_timeout_is_reported_as_the_deadline(user):
    class APITimeoutError(Exception):
        pass

    result = await run(Client(APITimeoutError("timed out")), limits=budget(), user=user)

    assert stopped_by(result) == "deadline"
    assert result.call.error_category == "budget"


async def test_time_spent_in_a_tool_counts_before_the_next_request(user):
    toolkit = Reader(pause=0.3)
    client = Client(call_tool("lookup", tool_id="toolu_1"), reply())

    result = await run(
        client, toolkit=toolkit, limits=budget(deadline_seconds=0.1), user=user
    )

    assert toolkit.calls == ["lookup"]
    assert len(client.calls) == 1
    assert stopped_by(result) == "deadline"


async def test_there_are_no_retries_of_any_kind(user, monkeypatch):
    class APIConnectionError(Exception):
        pass

    monkeypatch.setattr(structured, "RETRY_DELAYS", (0, 0))
    client = Client(APIConnectionError("reset"), reply())

    result = await run(client, limits=budget(), user=user)

    assert len(client.calls) == 1  # the unbudgeted loop would have sent a second
    assert result.status == StructuredCallStatus.FAILED
    assert "API call failed" in result.error
    assert client.options == [
        {"max_retries": 0, "timeout": client.options[0]["timeout"]}
    ]


async def test_a_summary_is_not_made_for_a_budgeted_turn(user, session, monkeypatch):
    summaries = []

    async def record(*args, **kwargs):
        summaries.append(args)

    monkeypatch.setattr(structured, "maybe_compact", record)

    budgeted = await run(Client(reply()), limits=budget(), session=session)
    assert (
        budgeted.ok and summaries == []
    )  # a summary is a model request outside the budget

    await run(Client(reply()), session=session)
    assert len(summaries) == 1  # the same turn without a budget still compacts as ever


# -- tools, starts and sends -----------------------------------------------------------


async def test_only_allowed_tools_are_offered_and_run(user):
    toolkit = Reader()
    client = Client(call_tool("explode", tool_id="toolu_1"), reply())

    result = await run(client, toolkit=toolkit, limits=budget(), user=user)

    assert result.ok
    assert [t["name"] for t in client.calls[0]["tools"]] == ["lookup", "send_reply"]
    assert toolkit.calls == []
    refusal = result.call.transcript[2]["content"][0]
    assert refusal["is_error"] is True
    assert "not available in this budgeted request" in refusal["content"]
    assert result.call.metadata["budget"]["refused_tools"] == ["explode"]


async def test_a_tool_that_needs_approval_is_refused_not_paused(user):
    class Order(Reader):
        def requires_approval(self, tool_name):
            return tool_name == "lookup"

    toolkit = Order()
    client = Client(call_tool("lookup", tool_id="toolu_1"), reply())

    result = await run(
        client, toolkit=toolkit, limits=budget(), user=user, allow_approvals=True
    )

    assert result.ok  # it never waited for a person
    assert toolkit.calls == []
    assert "requires approval" in result.call.transcript[2]["content"][0]["content"]


@pytest.mark.parametrize(
    ("action", "attempt"),
    [
        ("send a thread message", lambda: messaging.send(None, None, "hello")),
        (
            "send a thread message",
            lambda: orchestrator._send(None, None, "hello"),
        ),
        ("start a worker", lambda: workers.start(None, None, "task", {})),
        ("start a background task", lambda: background.BotTasks(None).start("task")),
        ("start an agent", lambda: agents.start(None, None)),
        ("schedule a wake", lambda: wakes.schedule(None, "later")),
    ],
)
async def test_an_allowed_tool_still_cannot_start_workers_agents_or_send(
    user, action, attempt
):
    class Sneaky(Reader):
        def execute_tool(self, tool_name, arguments):
            attempt()
            return "done"

    client = Client(call_tool("lookup", tool_id="toolu_1"), reply())

    result = await run(client, toolkit=Sneaky(), limits=budget(), user=user)

    assert result.ok
    assert (
        f"Refused: a budgeted request can't {action}"
        in (result.call.transcript[2]["content"][0]["content"])
    )
    assert result.call.metadata["budget"]["refused_actions"] == [action]
    assert not await ThreadMessage.objects.aexists()


def test_the_start_and_send_guards_do_nothing_without_a_budget():
    forbid("start a worker")  # no budgeted turn is running here
    with pytest.raises(AttributeError):
        workers.start(None, None, "task", {})  # reaches the real code


class Seeded(Reader):
    """A tool whose result the server fetches before the first model request."""

    def __init__(self, handler, tool="lookup"):
        super().__init__()
        self.handler, self.tool = handler, tool

    def pre_seeds(self):
        return [PreSeedCall(self.tool, {}, self.handler)]


def slow_seed(_):
    time.sleep(0.3)  # setup work that takes longer than the whole deadline


async def test_setup_work_is_under_the_budget_too(user):
    def start_a_worker(_):
        workers.start(None, None, "task", {})

    client = Client(reply())

    result = await run(
        client, toolkit=Seeded(start_a_worker), limits=budget(), user=user
    )

    assert result.ok  # a seed that raises is skipped, as ever
    assert result.call.metadata["budget"]["refused_actions"] == ["start a worker"]


async def test_the_deadline_counts_from_the_start_of_the_call_not_the_first_request(
    user,
):
    client = Client(reply())

    result = await run(
        client,
        toolkit=Seeded(slow_seed),
        limits=budget(deadline_seconds=0.1),
        user=user,
    )

    assert client.calls == []
    assert stopped_by(result) == "deadline"


async def test_a_seed_for_a_tool_that_is_not_allowed_is_not_run(user):
    ran = []
    toolkit = Seeded(lambda _: ran.append("seeded"), tool="explode")

    result = await run(Client(reply()), toolkit=toolkit, limits=budget(), user=user)

    assert result.ok and ran == []


async def test_a_nested_call_without_a_budget_stays_inside_the_budget_of_its_turn(user):
    def start_a_worker(_):
        workers.start(None, None, "task", {})

    class Nested(Reader):
        def execute_tool(self, tool_name, arguments):
            # What a tool does when it makes its own structured call that spawns a worker.
            text = SimpleNamespace(type="text", text="ok")
            done = SimpleNamespace(
                content=[text], stop_reason="end_turn", usage=usage()
            )
            spec = StructuredCallSpec(
                kind="nested", toolkits=[Seeded(start_a_worker)], output_parser=str
            )
            async_to_sync(run_structured_call)(
                spec, "go", user=user, engine=engine_for(Client(done))
            )
            return "done"

    result = await run(
        Client(call_tool("lookup", tool_id="toolu_1"), reply()),
        toolkit=Nested(),
        limits=budget(),
        user=user,
    )

    assert result.ok
    assert result.call.metadata["budget"]["refused_actions"] == ["start a worker"]


# -- engines and the budget itself ---------------------------------------------------------


async def test_an_engine_that_cannot_hold_a_budget_fails_before_any_request(user):
    class Wrapped(ClaudeAPIEngine):  # a subclass must claim support itself
        pass

    assert ClaudeAPIEngine.supports_request_limits
    assert not Wrapped.supports_request_limits
    client = Client(reply())
    engine = Wrapped(config={"model": "claude-test"})
    engine._client = client
    spec = apply_budget(chat_reply_spec([Reader()]), budget())

    result = await run_structured_call(spec, "Hi", user=user, engine=engine)

    assert client.calls == []
    assert stopped_by(result) == "engine"


async def test_the_engine_must_be_on_the_budgeted_model(user):
    client = Client(reply())

    result = await run(client, limits=budget(model="gpt-6-luna"), user=user)

    assert client.calls == []
    assert stopped_by(result) == "model"
    assert "not the budgeted gpt-6-luna" in result.call.error


async def test_the_openai_engine_is_held_to_the_same_limits(user):
    message = SimpleNamespace(content="hi", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )

    class OpenAIClient:
        def __init__(self):
            self.calls, self.options = [], []
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **options):
            self.options.append(options)
            return self

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return response

    client = OpenAIClient()
    engine = OpenAIAPIEngine(config={"model": "gpt-test", "max_tokens": 32000})
    engine._client = client
    engine.request_limits = budget_module.RequestLimits(
        max_input_tokens=50_000, max_output_tokens=900, timeout_seconds=12
    )

    await engine.complete([{"role": "user", "content": "Hi"}])

    assert client.calls[0]["max_completion_tokens"] == 900
    assert client.options == [{"max_retries": 0, "timeout": 12}]
    engine.request_limits = budget_module.RequestLimits(50, 900, 12)
    with pytest.raises(BudgetExceeded) as refused:
        await engine.complete([{"role": "user", "content": "Hi"}])
    assert refused.value.limit == "input_tokens"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {**LIMITS, "max_turns": 0},
        {**LIMITS, "max_output_tokens": True},
        {**LIMITS, "deadline_seconds": -1},
        {**LIMITS, "allowed_tools": "lookup"},
        {**LIMITS, "unlimited": True},
        {k: v for k, v in LIMITS.items() if k != "max_input_tokens"},
    ],
)
def test_a_malformed_budget_is_an_error_never_a_looser_budget(bad):
    with pytest.raises(ValueError, match=r"[Bb]udget|keys"):
        TurnBudget.from_dict(bad)
    with pytest.raises(ValueError, match=r"[Bb]udget|keys"):
        from_metadata({"turn_budget": bad})


def test_a_message_without_a_budget_has_none_and_a_valid_one_round_trips():
    assert from_metadata({}) is None
    assert from_metadata(None) is None
    sent = budget(model="m", expires_at=123.5)
    assert from_metadata({"turn_budget": sent.as_dict()}) == sent


# -- no budget, no change ------------------------------------------------------------


async def test_a_call_without_a_budget_is_unchanged(user):
    toolkit = Reader()
    client = Client(call_tool("explode", tool_id="toolu_1"), reply())

    result = await run(client, toolkit=toolkit, user=user)

    assert result.ok
    assert client.options == []  # no per-request client options
    assert [c["max_tokens"] for c in client.calls] == [512, 512]
    assert [t["name"] for t in client.calls[0]["tools"]] == [
        "lookup",
        "explode",
        "send_reply",
    ]
    assert toolkit.calls == ["explode"]  # every tool the bot has still runs
    assert "budget" not in result.call.metadata
    assert result.call.error_category == ""
