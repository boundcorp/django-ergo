"""Tests for the context builder and window chats."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.conversation.compaction import apply_native_window
from django_ergo.conversation.context import ContextBuilder
from django_ergo.conversation.context import MessageContextSource
from django_ergo.conversation.context import TextContextSource
from django_ergo.conversation.context import estimate_tokens
from django_ergo.conversation.history import HistoryMessage
from django_ergo.conversation.history import MessageSource
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.conversation.structured import resume_structured_call
from django_ergo.conversation.structured import run_structured_call
from django_ergo.conversation.window import WindowChat
from tests.test_conversation_compaction import add
from tests.test_conversation_structured import VALID_PLAN
from tests.test_conversation_structured import ApprovalToolkit
from tests.test_conversation_structured import Plan
from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_text
from tests.test_conversation_structured import claude_tool

User = get_user_model()

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class ListSource(MessageSource):
    kind = "test"

    def __init__(self, messages, source_id="test:chat"):
        super().__init__()
        self._source_id = source_id
        self._items = messages

    @property
    def source_id(self):
        return self._source_id

    def load(self):
        return self._items


def turn(line, user_text, reply, tool_output="x" * 400):
    """A user message, a tool call with a long result, and a reply: 4 lines."""
    return [
        HistoryMessage(
            "test:chat", line, "user", [{"type": "text", "text": user_text}], T0
        ),
        HistoryMessage(
            "test:chat",
            line + 1,
            "assistant",
            [{"type": "tool_use", "id": f"t{line}", "name": "lookup", "input": {}}],
            T0,
        ),
        HistoryMessage(
            "test:chat",
            line + 2,
            "tool",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": f"t{line}",
                    "name": "lookup",
                    "content": tool_output,
                }
            ],
            T0,
        ),
        HistoryMessage(
            "test:chat",
            line + 3,
            "assistant",
            [{"type": "text", "text": reply}],
            T0 + timedelta(seconds=line),
        ),
    ]


def chat_source(turns):
    messages = []
    for i in range(turns):
        messages += turn(i * 4, f"question {i}", f"answer {i}")
    return ListSource(messages)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def test_text_source_truncates_to_budget():
    source = TextContextSource("Notes", "word " * 100)
    section = source.render(budget_tokens=10)
    assert section.body.endswith("[truncated]")
    assert section.tokens <= 10
    assert not section.complete
    assert TextContextSource("Empty", "  ").render(100) is None
    assert TextContextSource("Lazy", lambda: "hi").render(100).body == "hi"


def test_message_source_uses_most_detail_that_fits():
    source = MessageContextSource(chat_source(3), min_messages=3)

    roomy = source.render(budget_tokens=5000)
    assert roomy.details == {"granularity": "reasoning", "count": 12}
    assert "[tool_call lookup()]" in roomy.body
    assert roomy.complete

    tight = source.render(budget_tokens=60)
    assert tight.details["granularity"] == "conversation"
    assert "[tool_call" not in tight.body
    message_lines = [line for line in tight.body.splitlines() if line.startswith("[L")]
    assert message_lines[-1].startswith("[L11 ")  # "answer 2" is last
    assert "granularity=reasoning or full" in tight.body


def test_message_source_pages_hint_and_limits():
    source = MessageContextSource(
        chat_source(5),
        granularity="conversation",
        max_messages=4,
    )
    section = source.render(budget_tokens=5000)
    lines = section.body.splitlines()
    assert lines[0].startswith("[L12 ")
    assert "latest 4 of 10" in section.title
    assert "ergo_chat_history_read source_id=test:chat end_line=12" in section.body
    assert not section.complete

    before = MessageContextSource(
        chat_source(5), granularity="conversation", before_line=8
    )
    assert "question 2" not in before.render(5000).body


def test_builder_splits_by_weight_and_reuses_spare_budget():
    big = MessageContextSource(chat_source(20), granularity="conversation", weight=1)
    small = TextContextSource("Notes", "short note", weight=1)
    builder = ContextBuilder(budget_tokens=200).add(big).add(small)

    built = builder.build()

    assert [s.title.split(" (")[0] for s in built.sections] == [
        "Messages from test:chat",
        "Notes",
    ]
    # The text source used ~3 of its 100 tokens; the rest went to messages.
    assert built.sections[0].tokens > 100
    assert built.tokens <= 200
    assert built.text.startswith("<context>\n## Messages from test:chat")
    assert ContextBuilder().build().text == ""


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# ---------------------------------------------------------------------------
# Native window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_native_window_keeps_only_current_turn():
    user = User.objects.create_user(username="window", password="x")
    session = ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        compaction_config={"native_history": "turn"},
    )
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "old"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "old reply"}]},
        {"role": "user", "content": [{"type": "text", "text": "new"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a"}]},
    ]
    assert apply_native_window(session, messages) == messages[2:]

    session.compaction_config = {}
    assert apply_native_window(session, messages) == messages

    # A message after tool results (steering, or one after a stopped turn)
    # continues the turn instead of starting a new one.
    session.compaction_config = {"native_history": "turn"}
    steered = [
        *messages,
        {"role": "user", "content": [{"type": "text", "text": "steer"}]},
        {"role": "user", "content": [{"type": "text", "text": "and more"}]},
    ]
    assert apply_native_window(session, steered) == steered[2:]


# ---------------------------------------------------------------------------
# WindowChat
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_window_chat_sends_recent_window_and_history_tools():
    user = await User.objects.acreate(username="window")
    engine = claude_engine(
        claude_tool("ergo_chat_history_read", {"end_line": 16, "limit": 2}),
        claude_text("We decided on the blue fridge."),
    )
    chat = await WindowChat.create(
        user=user,
        engine=engine,
        system_prompt="You are the kitchen bot.",
        recent=4,
        context_sources=[TextContextSource("Pantry", "eggs: 4")],
    )

    def seed():
        for i in range(10):
            add(chat.session, "user", f"question {i}")
            add(chat.session, "assistant", f"answer {i}")

    await sync_to_async(seed)()

    events = [e async for e in chat.send("What fridge did we pick?")]

    assert events[-1].event_type == "done"
    first, second = engine._client.calls
    system = first["system"]
    assert system.startswith("You are the kitchen bot.\n\n<context>")
    assert "latest 4 of 20" in system
    assert "[L16 " in system
    assert "question 7" not in system
    assert "ergo_chat_history_read source_id=session:" in system
    assert "## Pantry\neggs: 4" in system
    # Only the current turn is sent natively.
    assert [m["role"] for m in first["messages"]] == ["user"]
    assert first["messages"][0]["content"][0]["text"] == "What fridge did we pick?"
    assert "ergo_chat_history_read" in [t["name"] for t in first["tools"]]

    # The tool call ran against the chat's own history.
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "question 6" not in tool_result["content"]
    assert "[L14 " in tool_result["content"]
    assert "answer 7" in tool_result["content"]
    assert engine.ephemeral_context == ""

    # The next turn sees the previous one in its recent window.
    engine._client.responses = [claude_text("ok")]
    _ = [e async for e in chat.send("thanks")]
    assert "We decided on the blue fridge." in engine._client.calls[-1]["system"]


@pytest.mark.django_db(transaction=True)
async def test_window_chat_resume_switches_existing_session():
    user = await User.objects.acreate(username="window-resume")
    session = await ConversationSession.objects.acreate(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    chat = await WindowChat.resume(session, engine=claude_engine())
    await session.arefresh_from_db()
    assert session.compaction_config == {"native_history": "turn"}
    assert chat.recent == 15


def test_old_stream_chat_names_still_import():
    from django_ergo.conversation.stream import STREAM_CONFIG
    from django_ergo.conversation.stream import StreamChat
    from django_ergo.conversation.window import WINDOW_CONFIG

    assert StreamChat is WindowChat
    assert STREAM_CONFIG is WINDOW_CONFIG


# ---------------------------------------------------------------------------
# The recent-messages block leaves out what is sent natively
# ---------------------------------------------------------------------------


async def _window_session(username):
    user = await User.objects.acreate(username=username)
    session = await ConversationSession.objects.acreate(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        compaction_config={"native_history": "turn"},
    )

    def seed():
        add(session, "user", "earlier question")
        add(session, "assistant", "earlier answer")

    await sync_to_async(seed)()
    return session


def _recent_builder(session, *, incoming=True):
    """What bots.runtime and WindowChat build for a window chat."""
    return ContextBuilder(budget_tokens=4000).add(
        MessageContextSource(
            SessionSource(session),
            min_messages=10,
            max_messages=10,
            max_granularity="conversation",
            skip_native_turn=True,
            incoming=incoming,
        )
    )


@pytest.mark.django_db(transaction=True)
async def test_new_message_is_only_sent_natively():
    session = await _window_session("dup-new")
    engine = claude_engine(claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(
        spec,
        "Plan the new thing",
        session=session,
        engine=engine,
        context_builder=_recent_builder(session),
    )

    assert result.ok
    call = engine._client.calls[0]
    assert "earlier question" in call["system"]
    assert "Plan the new thing" not in call["system"]
    assert call["messages"][0]["content"][0]["text"] == "Plan the new thing"


@pytest.mark.django_db(transaction=True)
async def test_resumed_turn_is_not_repeated_in_context():
    session = await _window_session("dup-resume")
    engine = claude_engine(
        claude_tool("delete_all", {}, tool_id="d1"),
        claude_tool("submit_output", VALID_PLAN, tool_id="s1"),
    )
    spec = StructuredCallSpec(
        kind="planner", response_model=Plan, toolkits=[ApprovalToolkit()]
    )
    paused = await run_structured_call(
        spec,
        "Clean up",
        session=session,
        engine=engine,
        allow_approvals=True,
        context_builder=_recent_builder(session),
    )
    assert paused.status == "awaiting_approval"

    done = await resume_structured_call(
        spec,
        paused.call,
        {"d1": True},
        engine=engine,
        context_builder=_recent_builder(session, incoming=False),
    )

    assert done.ok
    call = engine._client.calls[1]
    assert call["messages"][0]["content"][0]["text"] == "Clean up"
    assert "earlier answer" in call["system"]
    assert "Clean up" not in call["system"]


@pytest.mark.django_db(transaction=True)
async def test_continued_turn_is_not_repeated_in_context():
    """After a turn stopped mid-tool-work, the next message continues it natively."""
    session = await _window_session("dup-continue")

    def stopped_turn():
        add(session, "user", "Look it up")
        add(session, "assistant", tool_use="t1")
        add(session, "user", tool_result="t1")

    await sync_to_async(stopped_turn)()
    engine = claude_engine(claude_tool("submit_output", VALID_PLAN))
    spec = StructuredCallSpec(kind="planner", response_model=Plan)

    result = await run_structured_call(
        spec,
        "carry on",
        session=session,
        engine=engine,
        context_builder=_recent_builder(session),
    )

    assert result.ok
    call = engine._client.calls[0]
    assert call["messages"][0]["content"][0]["text"] == "Look it up"
    assert "earlier answer" in call["system"]
    assert "Look it up" not in call["system"]
