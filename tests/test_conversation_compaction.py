"""Tests for session compaction modes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from django_ergo.conversation.compaction import compact_session
from django_ergo.conversation.compaction import decide_compaction
from django_ergo.conversation.compaction import maybe_compact
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationCompaction
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import OpenAIMessage
from django_ergo.conversation.renderer import ConversationRenderer
from django_ergo.conversation.runner import run_conversation_turn

from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_text
from tests.test_conversation_structured import claude_tool

User = get_user_model()

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture()
def user():
    return User.objects.create_user(username="compaction", password="x")


def make_session(user, mode="none", config=None, engine_type="claude"):
    return ConversationSession.objects.create(
        user=user,
        engine_type=engine_type,
        transport_type="api",
        status="active",
        compaction_mode=mode,
        compaction_config=config or {},
    )


def add(session, role, text=None, *, tool_use=None, tool_result=None, **fields):
    seq = session.claude_messages.count()
    msg = ClaudeMessage.objects.create(
        session=session, role=role, sequence=seq, **fields
    )
    if text is not None:
        ClaudeContentBlock.objects.create(
            message=msg, block_type="text", sequence=0, text=text
        )
    if tool_use:
        ClaudeContentBlock.objects.create(
            message=msg,
            block_type="tool_use",
            sequence=1,
            tool_use_id=tool_use,
            tool_name="lookup",
            tool_input={},
        )
    if tool_result:
        ClaudeContentBlock.objects.create(
            message=msg,
            block_type="tool_result",
            sequence=0,
            tool_result_for=tool_result,
            tool_result_content="42",
        )
    return msg


def chat(session, turns):
    start = session.claude_messages.count() // 2
    for i in range(start, start + turns):
        add(session, "user", f"question {i}")
        add(session, "assistant", f"answer {i}")


class RecordingSummarizer:
    def __init__(self, text="SUMMARY"):
        self.text = text
        self.calls = []

    async def __call__(self, previous, transcript):
        self.calls.append((previous, transcript))
        return f"{self.text} {len(self.calls)}"


async def _context(engine, session):
    return await sync_to_async(engine.reconstruct_messages)(session)


def _texts(messages):
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(b.get("text", f"<{b['type']}>") for b in content)
    return out


# ---------------------------------------------------------------------------
# Stream mode
# ---------------------------------------------------------------------------


async def test_stream_folds_all_but_recent(user):
    session = await sync_to_async(make_session)(
        user, "stream", {"keep_recent": 2, "batch": 2}
    )
    await sync_to_async(chat)(session, 2)  # 4 messages: at threshold
    engine = claude_engine()
    summarizer = RecordingSummarizer()

    assert await maybe_compact(session, engine, summarizer=summarizer) is None

    await sync_to_async(chat)(session, 1)  # 6 messages
    compaction = await maybe_compact(session, engine, summarizer=summarizer)

    assert compaction.upto_sequence == 3
    assert compaction.from_sequence == 0
    assert compaction.message_count == 4
    assert compaction.mode == "stream"
    previous, transcript = summarizer.calls[0]
    assert previous == ""
    assert "question 0" in transcript
    assert "question 2" not in transcript

    context = await _context(engine, session)
    texts = _texts(context)
    assert "SUMMARY 1" in texts[0]
    assert "<conversation-summary>" in texts[0]
    assert texts[1:] == ["question 2", "answer 2"]

    # History tools still see every message.
    full = await sync_to_async(ConversationRenderer(detail="full").render)(session)
    assert "question 0" in full


async def test_stream_summaries_roll_forward(user):
    session = await sync_to_async(make_session)(
        user, "stream", {"keep_recent": 2, "batch": 2}
    )
    engine = claude_engine()
    summarizer = RecordingSummarizer()
    await sync_to_async(chat)(session, 3)
    await maybe_compact(session, engine, summarizer=summarizer)

    await sync_to_async(chat)(session, 2)
    second = await maybe_compact(session, engine, summarizer=summarizer)

    assert second.from_sequence == 4
    assert second.upto_sequence == 7
    previous, transcript = summarizer.calls[1]
    assert previous == "SUMMARY 1"
    assert "question 0" not in transcript
    assert "question 2" in transcript
    texts = _texts(await _context(engine, session))
    assert "SUMMARY 2" in texts[0]
    assert texts[1:] == ["question 4", "answer 4"]


async def test_cut_never_splits_tool_call_from_result(user):
    session = await sync_to_async(make_session)(user)

    def build():
        add(session, "user", "look it up")
        add(session, "assistant", tool_use="t1")
        add(session, "user", tool_result="t1")
        add(session, "assistant", "it is 42")
        add(session, "user", "thanks")
        add(session, "assistant", "welcome")

    await sync_to_async(build)()
    engine = claude_engine()

    # Keeping 3 would start the tail on a tool result; the cut moves back to
    # the start and nothing can be folded.
    assert (
        await compact_session(
            session, engine, keep_recent=3, summarizer=RecordingSummarizer()
        )
        is None
    )

    compaction = await compact_session(
        session, engine, keep_recent=2, summarizer=RecordingSummarizer()
    )
    assert compaction.upto_sequence == 3


# ---------------------------------------------------------------------------
# Time and context-size modes
# ---------------------------------------------------------------------------


async def test_time_mode_compacts_after_idle_gap(user):
    session = await sync_to_async(make_session)(user, "time", {"idle_seconds": 60})
    await sync_to_async(chat)(session, 2)
    engine = claude_engine()
    now = timezone.now()

    assert await decide_compaction(session, now=now) is None

    later = now + timedelta(minutes=5)
    decision = await decide_compaction(session, now=later)
    assert decision.keep_recent == 0
    assert "idle" in decision.reason

    compaction = await maybe_compact(
        session, engine, now=later, summarizer=RecordingSummarizer()
    )
    assert compaction.upto_sequence == 3
    context = await _context(engine, session)
    assert len(context) == 1
    assert "SUMMARY 1" in _texts(context)[0]


async def test_context_size_mode_uses_last_prompt_size(user):
    session = await sync_to_async(make_session)(
        user, "context_size", {"max_context_tokens": 1000, "keep_recent": 2}
    )

    def build():
        add(session, "user", "q0")
        add(session, "assistant", "a0", input_tokens=100, output_tokens=10)
        add(session, "user", "q1")
        add(
            session,
            "assistant",
            "a1",
            input_tokens=200,
            output_tokens=10,
            cache_read_input_tokens=900,
        )

    await sync_to_async(build)()
    decision = await decide_compaction(session)
    assert decision.keep_recent == 2
    assert "1110 tokens" in decision.reason

    compaction = await maybe_compact(
        session, claude_engine(), summarizer=RecordingSummarizer()
    )
    assert compaction.upto_sequence == 1


async def test_none_mode_ignores_stored_compactions(user):
    session = await sync_to_async(make_session)(user, "stream", {"keep_recent": 0})
    await sync_to_async(chat)(session, 1)
    engine = claude_engine()
    await compact_session(
        session, engine, keep_recent=0, summarizer=RecordingSummarizer()
    )
    assert len(await _context(engine, session)) == 1

    session.compaction_mode = "none"
    assert len(await _context(engine, session)) == 2
    assert await decide_compaction(session) is None


async def test_failed_summary_does_not_block(user):
    session = await sync_to_async(make_session)(user, "stream", {"keep_recent": 0})
    await sync_to_async(chat)(session, 1)

    async def broken(previous, transcript):
        msg = "summarizer down"
        raise RuntimeError(msg)

    assert await maybe_compact(session, claude_engine(), summarizer=broken) is None
    assert await ConversationCompaction.objects.filter(session=session).acount() == 0


# ---------------------------------------------------------------------------
# Engines and runner integration
# ---------------------------------------------------------------------------


async def test_turn_compacts_with_a_structured_call_before_sending(user):
    session = await sync_to_async(make_session)(
        user, "stream", {"keep_recent": 0, "batch": 1}
    )
    await sync_to_async(chat)(session, 1)
    engine = claude_engine(
        claude_tool(
            "submit_output",
            {
                "summary": "Rolling summary text",
                "decisions": ["use postgres"],
                "open_items": [],
            },
        ),
        claude_text("hi"),
    )

    events = [e async for e in run_conversation_turn(engine, session, "next")]

    assert [e.text for e in events if e.event_type == "text"] == ["hi"]
    summary_call, turn_call = engine._client.calls
    assert "question 0" in summary_call["messages"][0]["content"][0]["text"]
    assert "running summary" in summary_call["system"]
    sent = _texts(turn_call["messages"])
    assert "Rolling summary text" in sent[0]
    assert "- use postgres" in sent[0]
    assert sent[1:] == ["next"]

    compaction = await session.compactions.select_related("structured_call").aget()
    call = compaction.structured_call
    assert call.kind == "compaction"
    assert call.session_id is None
    assert call.metadata == {"compacted_session": str(session.pk)}
    assert call.response["decisions"] == ["use postgres"]


async def test_failed_compaction_call_leaves_session_uncompacted(user):
    session = await sync_to_async(make_session)(
        user, "stream", {"keep_recent": 0, "batch": 1}
    )
    await sync_to_async(chat)(session, 1)
    engine = claude_engine(claude_text("no tool", stop="max_tokens"), claude_text("hi"))

    _ = [e async for e in run_conversation_turn(engine, session, "next")]

    assert not await session.compactions.aexists()
    failed = await StructuredCall.objects.aget(kind="compaction")
    assert failed.status == "failed"


async def test_openai_summary_goes_after_system_message(user):
    session = await sync_to_async(make_session)(
        user, "stream", {"keep_recent": 0}, engine_type="openai"
    )

    def build():
        OpenAIMessage.objects.create(
            session=session, role="system", content="Be brief.", sequence=0
        )
        OpenAIMessage.objects.create(
            session=session, role="user", content="q0", sequence=1
        )
        OpenAIMessage.objects.create(
            session=session, role="assistant", content="a0", sequence=2
        )

    await sync_to_async(build)()
    engine = OpenAIAPIEngine(config={})
    compaction = await compact_session(
        session, engine, keep_recent=0, summarizer=RecordingSummarizer()
    )

    assert compaction.from_sequence == 1
    context = await _context(engine, session)
    assert [m["role"] for m in context] == ["system", "user"]
    assert context[0]["content"] == "Be brief."
    assert "SUMMARY 1" in context[1]["content"]
