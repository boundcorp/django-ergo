"""A thread message that carries a budget runs under it, through the real queue and turn runner
(django_ergo.bots.messaging -> Bot.ask -> conversation.structured); one that doesn't runs as ever."""

import textwrap
import time

import pytest
from django_ergo.bots import messaging, webhooks
from django_ergo.conversation.budget import STOPPED_PREFIX, TurnBudget, attach
from django_ergo.conversation.models import ConversationSession, ThreadMessage, Worker

from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.apps.bots.tests.test_api import cook, use_bots  # noqa: F401

TOOLS = """
import time
from django_ergo.bots import bot_task, bot_tool

@bot_task
def make_report():
    return "report"

@bot_tool
def pantry_count(item: str) -> int:
    return 4

@bot_tool
def slow_count(item: str) -> int:
    time.sleep(0.3)
    return 4

@bot_tool(takes_context=True)
def start_report(ctx) -> str:
    ctx.workers.start("make_report", title="Report")
    return "started"

@bot_tool(requires_approval=True)
def order(item: str) -> str:
    return f"ordered {item}"
"""

# The bot's tools are always loaded in its main chat, so they are offered as they would be live.
BOT = """
name: kitchen
description: Runs the kitchen
engine: {type: claude}
orchestration: false
tools: [tools/pantry.py]
chats:
  main:
    skills: [pantry]
"""

BUDGET = {
    "max_turns": 5,
    "max_input_tokens": 100_000,
    "max_output_tokens": 4_000,
    "deadline_seconds": 30,
    "allowed_tools": ["pantry_count"],
}


@pytest.fixture
def bot_folder(tmp_path):
    folder = tmp_path / "kitchen"
    (folder / "tools").mkdir(parents=True)
    (folder / "agents.md").write_text("You run the kitchen.")
    (folder / "tools" / "pantry.py").write_text(textwrap.dedent(TOOLS))
    (folder / "bot.yaml").write_text(textwrap.dedent(BOT))
    return folder


@pytest.fixture
def session(cook):  # noqa: F811
    return ConversationSession.objects.create(user=cook, bot_name="kitchen", metadata={"bot_role": "main"})


@pytest.fixture(autouse=True)
def held_messages(settings):
    """Messages are recorded, not delivered: a test runs only the turns it starts."""
    held = []
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "THREAD_MESSAGE_RUNNER": held.append}
    return held


def deliver(session, metadata=None, **limits):
    """Send a request, as a voice delegation does (with a budget unless ``metadata`` says
    otherwise), and run its turn through the queue."""
    budget = TurnBudget.from_dict({**BUDGET, **limits, "expires_at": time.time() + 60})
    metadata = attach({}, budget) if metadata is None else metadata
    message = messaging.send(None, session, "What is in the pantry?", metadata=metadata)
    messaging.deliver(str(message.id), registry=webhooks.get_registry())
    message.refresh_from_db()
    return message


def counts(n, **usage):
    """``n`` model steps that each call the allowed tool (distinct calls, as a model makes them)."""
    return [tool_call("pantry_count", {"item": "x"}, **usage) for _ in range(n)]


def tools_offered(client, request=0):
    return [t["name"] for t in client.calls[request]["tools"]]


def tool_results(session):
    return [
        block.tool_result_content
        for message in session.messages.all()
        for block in message.content_blocks.all()
        if block.block_type == "tool_result"
    ]


@pytest.mark.django_db(transaction=True)
def test_a_budgeted_request_runs_its_allowed_tools_and_answers(session, use_bots):  # noqa: F811
    client = use_bots(tool_call("pantry_count", {"item": "eggs"}), say("Four eggs."))

    message = deliver(session)

    assert (message.status, message.reply_text, message.error) == ("answered", "Four eggs.", "")
    assert tools_offered(client) == ["pantry_count", "send_reply"]  # not order, not the worker tools
    assert all(options["max_retries"] == 0 for options in client.options)
    call = session.structured_calls.get()
    assert call.status == "completed"
    used = call.metadata["budget"]["used"]
    assert (used["model_turns"], used["input_tokens"], used["output_tokens"]) == (2, 20, 10)
    assert call.metadata["budget"]["stopped_by"] == ""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("limit", "limits", "script"),
    [
        ("max_turns", {"max_turns": 2}, lambda: counts(3)),
        (
            "output_tokens",
            {"max_output_tokens": 12},
            lambda: [
                tool_call("pantry_count", {"item": "x"}, output_tokens=5),
                tool_call("pantry_count", {"item": "x"}, output_tokens=7),
                say("never asked"),
            ],
        ),
        (
            "input_tokens",
            {"max_input_tokens": 8_000},
            lambda: [*counts(1, input_tokens=7_500), say("never asked")],
        ),
        (
            "deadline",
            {"deadline_seconds": 0.1, "allowed_tools": ["slow_count"]},
            lambda: [tool_call("slow_count", {"item": "x"}), say("never asked")],
        ),
    ],
)
def test_each_limit_stops_the_turn_and_fails_the_message_truthfully(session, use_bots, limit, limits, script):  # noqa: F811
    responses = script()
    client = use_bots(*responses)

    message = deliver(session, **limits)

    call = session.structured_calls.get()
    assert call.metadata["budget"]["stopped_by"] == limit
    assert (call.status, call.error_category) == ("failed", "budget")
    # The message fails with the reason, not "(no reply: ...)" as if the model had answered.
    assert (message.status, message.reply_text) == ("failed", "")
    assert message.error == call.error
    assert message.error.startswith(STOPPED_PREFIX) and f"({limit})" in message.error
    assert "no final answer" in message.error
    assert len(client.calls) < len(responses)  # the request after the limit was never sent
    assert ThreadMessage.objects.count() == 1  # nothing was sent back or on to anyone


@pytest.mark.django_db(transaction=True)
def test_a_tool_outside_the_allowlist_is_refused(session, use_bots):  # noqa: F811
    client = use_bots(
        tool_call("order", {"item": "milk"}),
        tool_call("ergo_worker_start", {"task": "make_report", "title": "Report"}),
        say("I could not."),
    )

    message = deliver(session)

    assert message.status == "answered"
    assert "ergo_worker_start" not in tools_offered(client)
    results = tool_results(session)
    assert len(results) == 3  # two refusals, then the reply's acceptance
    assert all("not available in this budgeted request" in r for r in results[:2])
    assert "ordered" not in " ".join(results)  # order() never ran
    assert session.structured_calls.get().metadata["budget"]["refused_tools"] == ["ergo_worker_start", "order"]
    assert not Worker.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_an_allowed_tool_that_tries_to_start_a_worker_is_refused(session, use_bots):  # noqa: F811
    use_bots(tool_call("start_report", {}), say("Could not start it."))

    message = deliver(session, allowed_tools=["start_report"])

    assert message.status == "answered"
    assert not Worker.objects.exists()  # nothing was queued for any worker
    assert session.structured_calls.get().metadata["budget"]["refused_actions"] == ["start a worker"]
    assert "can't start a worker" in tool_results(session)[0]


@pytest.mark.django_db(transaction=True)
def test_a_budgeted_request_never_waits_for_approval(session, use_bots):  # noqa: F811
    use_bots(tool_call("order", {"item": "milk"}), say("Not ordered."))

    message = deliver(session, allowed_tools=["order"])

    assert message.status == "answered"  # not "waiting": nobody could resume it under its budget
    assert session.structured_calls.get().status == "completed"
    results = tool_results(session)
    assert "requires approval, which this call cannot request" in results[0]
    assert "ordered" not in " ".join(results)


@pytest.mark.django_db(transaction=True)
def test_a_budget_that_is_invalid_or_expired_is_never_run_unbudgeted(session, use_bots):  # noqa: F811
    client = use_bots(say("must not run"))

    invalid = deliver(session, metadata={"turn_budget": {**BUDGET, "unlimited": True}})
    assert invalid.status == "failed" and invalid.error.startswith("Not run:")
    assert "invalid" in invalid.error

    expired = TurnBudget.from_dict({**BUDGET, "expires_at": time.time() - 1})
    late = messaging.send(None, session, "Too late", metadata=attach({}, expired))
    messaging.deliver(str(late.id), registry=webhooks.get_registry())
    late.refresh_from_db()
    assert late.status == "failed" and late.error.startswith("Not run:")
    assert "waited too long" in late.error
    assert client.calls == [] and not session.structured_calls.exists()


@pytest.mark.django_db(transaction=True)
def test_a_request_without_a_budget_runs_as_it_always_did(session, use_bots):  # noqa: F811
    client = use_bots(*counts(7), tool_call("order", {"item": "milk"}))

    message = deliver(session, metadata={})

    # Seven tool steps run (more than the budget above allows), then the approval gate stops the turn.
    assert len(client.calls) == 8
    assert client.options == []  # no per-request client options: SDK retries and timeouts as configured
    assert {"pantry_count", "order", "ergo_worker_start", "ergo_chat_history_read"} <= set(tools_offered(client))
    call = session.structured_calls.get()
    assert call.status == "awaiting_approval"  # the confirmation flow is untouched
    assert message.status == "waiting"
    assert "budget" not in call.metadata and call.error_category == ""
