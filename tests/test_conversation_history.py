"""Tests for normalized message history, the history toolkit, and the Codex importer."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.conversation.history import ClaudeCodeSource
from django_ergo.conversation.history import CodexSource
from django_ergo.conversation.history import Granularity
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.history import render_messages
from django_ergo.conversation.history import sources_from_paths
from django_ergo.conversation.history_search_toolkit import MessageHistoryToolkit
from django_ergo.conversation.importers import ImportService
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import OpenAIMessage

User = get_user_model()

CLAUDE_LINES = [
    {"type": "summary", "summary": "ignored"},
    {
        "type": "user",
        "sessionId": "abc",
        "timestamp": "2026-09-01T10:00:00Z",
        "message": {"role": "user", "content": "How many eggs are left?"},
    },
    {
        "type": "assistant",
        "sessionId": "abc",
        "timestamp": "2026-09-01T10:00:05Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Check the pantry tool."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "pantry_lookup",
                    "input": {"item": "eggs"},
                },
            ],
        },
    },
    {
        "type": "user",
        "sessionId": "abc",
        "timestamp": "2026-09-01T10:00:06Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "eggs: 4"}
            ],
        },
    },
    {
        "type": "assistant",
        "sessionId": "abc",
        "timestamp": "2026-09-01T10:00:08Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "You have 4 eggs."}],
        },
    },
    {
        "type": "user",
        "isMeta": True,
        "sessionId": "abc",
        "message": {"role": "user", "content": "meta noise"},
    },
    {
        "type": "user",
        "sessionId": "abc",
        "timestamp": "2026-09-02T09:00:00Z",
        "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
    },
]

CODEX_LINES = [
    {
        "timestamp": "2026-09-03T08:00:00Z",
        "type": "session_meta",
        "payload": {"id": "codex-123", "cwd": "/repo", "cli_version": "0.40"},
    },
    {
        "timestamp": "2026-09-03T08:00:01Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "<environment_context>cwd</environment_context>",
                }
            ],
        },
    },
    {
        "timestamp": "2026-09-03T08:00:02Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Fix the failing test"}],
        },
    },
    {
        "timestamp": "2026-09-03T08:00:03Z",
        "type": "response_item",
        "payload": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Run pytest first."}],
        },
    },
    {
        "timestamp": "2026-09-03T08:00:04Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "shell",
            "arguments": json.dumps({"command": ["pytest", "-x"]}),
            "call_id": "c1",
        },
    },
    {"timestamp": "2026-09-03T08:00:05Z", "type": "event_msg", "payload": {}},
    {
        "timestamp": "2026-09-03T08:00:06Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"output": "1 failed"}),
        },
    },
    {
        "timestamp": "2026-09-03T08:00:09Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Fixed test_eggs."}],
        },
    },
]


def write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def claude_file(tmp_path):
    return write_jsonl(tmp_path / "abc.jsonl", CLAUDE_LINES)


@pytest.fixture
def codex_file(tmp_path):
    folder = tmp_path / "sessions" / "2026" / "09" / "03"
    folder.mkdir(parents=True)
    return write_jsonl(folder / "rollout-2026-09-03-codex-123.jsonl", CODEX_LINES)


# ---------------------------------------------------------------------------
# Sources and granularity
# ---------------------------------------------------------------------------


def test_claude_code_source_lines_and_granularity(claude_file):
    source = ClaudeCodeSource(claude_file)
    messages = source.messages()

    assert source.source_id == "claude:abc"
    assert [m.line for m in messages] == [1, 2, 3, 4, 6]
    assert messages[0].timestamp == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    conversation = render_messages(messages, Granularity.CONVERSATION)
    assert conversation.splitlines() == [
        "[claude:abc L1 2026-09-01T10:00:00+00:00 USER] How many eggs are left?",
        "[claude:abc L4 2026-09-01T10:00:08+00:00 ASSISTANT] You have 4 eggs.",
    ]

    reasoning = render_messages(messages, Granularity.REASONING)
    assert "<thinking>Check the pantry tool.</thinking>" in reasoning
    assert '[tool_call pantry_lookup(item="eggs")]' in reasoning
    assert "[tool_result pantry_lookup: 1 lines] eggs: 4" in reasoning
    assert "/clear" not in reasoning

    full = render_messages(messages, Granularity.FULL)
    assert '[tool_call pantry_lookup id=t1] {"item": "eggs"}' in full
    assert "[tool_result pantry_lookup] eggs: 4" in full
    assert "[context] <command-name>/clear</command-name>" in full


def test_codex_source(codex_file):
    source = CodexSource(codex_file)
    messages = source.messages()

    assert source.source_id == "codex:2026-09-03-codex-123"
    conversation = render_messages(messages, Granularity.CONVERSATION)
    assert "Fix the failing test" in conversation
    assert "Fixed test_eggs." in conversation
    assert "environment_context" not in conversation
    reasoning = render_messages(messages, Granularity.REASONING)
    assert "<thinking>Run pytest first.</thinking>" in reasoning
    assert "[tool_call shell(" in reasoning
    assert "[tool_result shell: 1 lines]" in reasoning


def test_sources_from_paths_detects_formats(tmp_path, claude_file, codex_file):
    (tmp_path / "notes.jsonl").write_text('{"hello": 1}\n')
    sources = sources_from_paths([tmp_path])
    kinds = sorted((s.kind, s.source_id) for s in sources)
    assert kinds == [
        ("claude_code", "claude:abc"),
        ("codex", "codex:2026-09-03-codex-123"),
    ]


@pytest.mark.django_db
def test_session_source_reads_db_rows_and_attachments(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    user = User.objects.create_user(username="hist", password="x")
    session = ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    first = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=first, block_type="text", sequence=0, text="what is this"
    )
    ConversationAttachment.objects.create(
        session=session,
        message_sequence=0,
        kind="image",
        media_type="image/png",
        filename="fridge.png",
    )
    reply = ClaudeMessage.objects.create(session=session, role="assistant", sequence=1)
    ClaudeContentBlock.objects.create(
        message=reply, block_type="text", sequence=0, text="a fridge"
    )

    source = SessionSource(session)
    text = render_messages(source.messages(), Granularity.CONVERSATION)

    assert f"[session:{session.pk} L0 " in text
    assert "[image attachment: fridge.png (image/png)]\nwhat is this" in text
    assert text.endswith("ASSISTANT] a fridge")


@pytest.mark.django_db
def test_session_source_reads_openai_rows():
    user = User.objects.create_user(username="hist-oa", password="x")
    session = ConversationSession.objects.create(
        user=user, engine_type="openai", transport_type="api", status="active"
    )
    OpenAIMessage.objects.create(
        session=session, role="system", content="Be brief", sequence=0
    )
    OpenAIMessage.objects.create(session=session, role="user", content="hi", sequence=1)
    OpenAIMessage.objects.create(
        session=session,
        role="assistant",
        content=None,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }
        ],
        sequence=2,
    )
    OpenAIMessage.objects.create(
        session=session, role="tool", content="found", tool_call_id="c1", sequence=3
    )

    messages = SessionSource(session).messages()
    reasoning = render_messages(messages, Granularity.REASONING, include_source=False)

    assert reasoning.splitlines()[0].endswith("USER] hi")
    assert '[tool_call lookup(q="x")]' in reasoning
    assert "[tool_result lookup: 1 lines] found" in reasoning
    assert "Be brief" in render_messages(messages, Granularity.FULL)


# ---------------------------------------------------------------------------
# Toolkit
# ---------------------------------------------------------------------------


@pytest.fixture
def toolkit(claude_file, codex_file):
    return MessageHistoryToolkit(
        [ClaudeCodeSource(claude_file), CodexSource(codex_file)]
    )


def test_toolkit_lists_sources(toolkit):
    listing = toolkit.execute_tool("ergo_chat_history_sources", {})
    assert "claude:abc (claude_code)" in listing
    assert (
        "5 messages, 2026-09-01T10:00:00+00:00 to 2026-09-02T09:00:00+00:00" in listing
    )
    assert "history_* tools" in toolkit.render_overview()


def test_toolkit_requires_source_when_ambiguous(toolkit):
    with pytest.raises(ValueError, match="source_id is required"):
        toolkit.execute_tool("ergo_chat_history_tail", {})
    with pytest.raises(ValueError, match="Unknown source"):
        toolkit.execute_tool("ergo_chat_history_tail", {"source_id": "nope"})


def test_read_pages_forward_and_backward(claude_file):
    toolkit = MessageHistoryToolkit([ClaudeCodeSource(claude_file)])

    page = toolkit.execute_tool(
        "ergo_chat_history_read", {"limit": 2, "granularity": "reasoning"}
    )
    assert page.splitlines()[0].startswith("[L1 ")
    assert page.splitlines()[-1] == "More: ergo_chat_history_read start_line=3"

    page2 = toolkit.execute_tool(
        "ergo_chat_history_read", {"start_line": 3, "granularity": "reasoning"}
    )
    assert "[L3 " in page2
    assert "More:" not in page2

    back = toolkit.execute_tool("ergo_chat_history_read", {"end_line": 4, "limit": 1})
    assert back.splitlines() == [
        "[L1 2026-09-01T10:00:00+00:00 USER] How many eggs are left?"
    ]


def test_tail_and_around(claude_file):
    toolkit = MessageHistoryToolkit([ClaudeCodeSource(claude_file)])

    tail = toolkit.execute_tool("ergo_chat_history_tail", {"limit": 1})
    assert tail.splitlines() == [
        "[L4 2026-09-01T10:00:08+00:00 ASSISTANT] You have 4 eggs.",
        "Earlier: ergo_chat_history_read end_line=4",
    ]

    around = toolkit.execute_tool(
        "ergo_chat_history_around",
        {"line": 3, "before": 1, "after": 0, "granularity": "full"},
    )
    lines = around.splitlines()
    assert lines[0].startswith("[L2 ")
    assert lines[-1].startswith("[L3 ")


def test_by_date_across_sources(toolkit):
    result = toolkit.execute_tool(
        "ergo_chat_history_by_date",
        {"since": "2026-09-01", "until": "2026-09-03", "limit": 2},
    )
    lines = result.splitlines()
    assert lines[0].startswith("[claude:abc L1 ")
    assert lines[1].startswith("[claude:abc L4 ")
    assert lines[2] == "More: ergo_chat_history_by_date since=2026-09-03T08:00:02+00:00"

    later = toolkit.execute_tool(
        "ergo_chat_history_by_date", {"since": "2026-09-03T08:00:02Z"}
    )
    assert "Fix the failing test" in later
    assert "4 eggs" not in later


def test_search_finds_tool_content_and_filters(toolkit):
    hits = toolkit.execute_tool("ergo_chat_history_search", {"query": "EGGS 4"})
    assert "[claude:abc L4 " in hits
    assert "[claude:abc L3 " in hits  # matched inside a tool result
    assert "ergo_chat_history_around" in hits

    scoped = toolkit.execute_tool(
        "ergo_chat_history_search",
        {"query": "failing", "source_id": "codex:2026-09-03-codex-123"},
    )
    assert "Fix the failing test" in scoped
    assert (
        toolkit.execute_tool(
            "ergo_chat_history_search", {"query": "pantry", "since": "2026-09-03"}
        )
        == "(no matches)"
    )


def test_invalid_inputs(toolkit):
    with pytest.raises(ValueError, match="granularity"):
        toolkit.execute_tool(
            "ergo_chat_history_tail",
            {"source_id": "claude:abc", "granularity": "verbose"},
        )
    with pytest.raises(ValueError, match="Invalid date"):
        toolkit.execute_tool("ergo_chat_history_by_date", {"since": "yesterday"})
    with pytest.raises(ValueError, match="Unknown tool"):
        toolkit.execute_tool("history_nope", {})


def test_tool_schemas():
    from django_ergo.conversation.adapters import ClaudeToolAdapter

    schemas = MessageHistoryToolkit([]).get_tools_schema(ClaudeToolAdapter())
    names = [s["name"] for s in schemas]
    assert names == [
        "ergo_chat_history_sources",
        "ergo_chat_history_read",
        "ergo_chat_history_tail",
        "ergo_chat_history_around",
        "ergo_chat_history_by_date",
        "ergo_chat_history_search",
    ]
    around = schemas[3]["input_schema"]
    assert around["required"] == ["line"]


@pytest.mark.django_db
def test_live_session_source_refreshes():
    user = User.objects.create_user(username="live", password="x")
    session = ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="active"
    )
    toolkit = MessageHistoryToolkit([SessionSource(session)])
    assert toolkit.execute_tool("ergo_chat_history_tail", {}) == "(no messages)"

    message = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=message, block_type="text", sequence=0, text="new message"
    )
    assert "new message" in toolkit.execute_tool("ergo_chat_history_tail", {})


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.django_db(transaction=True)
def test_codex_import_round_trips_through_session_source(codex_file):
    user = User.objects.create_user(username="codex-import", password="x")
    session = async_to_sync(ImportService().import_auto)(_records(codex_file), user)

    assert session.metadata["imported_from"] == "codex_cli"
    assert session.metadata["cwd"] == "/repo"
    assert session.session_id == "codex-123"
    imported = SessionSource(session).messages()
    original = CodexSource(codex_file).messages()
    assert [m.line for m in imported] == [m.line for m in original]
    assert [m.timestamp for m in imported] == [m.timestamp for m in original]
    assert render_messages(
        imported, Granularity.REASONING, include_source=False
    ) == render_messages(original, Granularity.REASONING, include_source=False)


@pytest.mark.django_db(transaction=True)
def test_claude_import_keeps_original_timestamps(claude_file):
    user = User.objects.create_user(username="claude-import", password="x")
    session = async_to_sync(ImportService().import_auto)(_records(claude_file), user)

    first = SessionSource(session).messages()[0]
    assert first.line == 1
    assert first.timestamp == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
