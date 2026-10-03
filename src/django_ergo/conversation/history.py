"""Normalized message history over database sessions and CLI transcripts on disk.

Every source (a ``ConversationSession``, a Claude Code JSONL transcript, a
Codex CLI rollout) is read into ``HistoryMessage`` records with the same
shape:

- ``source_id``: stable id of the source, e.g. ``session:<uuid>``
- ``line``: position in the source (DB message sequence, or JSONL line
  number), usable for paging and for "show me around line N"
- ``timestamp``: when the message was written, when known
- ``role``: ``user``, ``assistant``, ``tool`` or ``system``
- ``blocks``: normalized content blocks (``text``, ``thinking``,
  ``tool_use``, ``tool_result``, ``attachment``, ``context``)

``render_message`` turns a record into one line of text at a granularity:

- ``conversation``: what the user and the assistant said to each other
- ``reasoning``: adds thinking and short tool-call/tool-result summaries
- ``full``: adds tool inputs and results verbatim, plus injected context
"""

from __future__ import annotations

import json
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django_ergo.conversation.models import ConversationSession

SUMMARY_ARG_CHARS = 50
RESULT_PREVIEW_CHARS = 120

# User text injected by CLIs rather than typed by a person.
_CONTEXT_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<system-reminder>",
    "# AGENTS.md instructions",
)


class Granularity(str, Enum):
    CONVERSATION = "conversation"
    REASONING = "reasoning"
    FULL = "full"

    @classmethod
    def parse(cls, value: str | Granularity | None) -> Granularity:
        if value is None or value == "":
            return cls.CONVERSATION
        try:
            return cls(value)
        except ValueError:
            names = ", ".join(g.value for g in cls)
            msg = f"Unknown granularity {value!r}; use one of: {names}"
            raise ValueError(msg) from None


@dataclass
class HistoryMessage:
    source_id: str
    line: int
    role: str
    blocks: list[dict]
    timestamp: datetime | None = None

    def text(self) -> str:
        return "\n".join(b["text"] for b in self.blocks if b["type"] == "text")

    def searchable_text(self) -> str:
        """All content, at full detail, for search."""
        return render_body(self, Granularity.FULL)


@dataclass
class SourceInfo:
    source_id: str
    title: str
    kind: str
    message_count: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    metadata: dict = field(default_factory=dict)


class MessageSource(ABC):
    """A readable conversation. Subclasses load messages once and cache them."""

    kind: str = "source"

    def __init__(self):
        self._messages: list[HistoryMessage] | None = None

    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @property
    def title(self) -> str:
        return self.source_id

    @abstractmethod
    def load(self) -> list[HistoryMessage]:
        """Read all messages, ordered by line."""

    def messages(self) -> list[HistoryMessage]:
        if self._messages is None:
            self._messages = self.load()
        return self._messages

    def refresh(self) -> None:
        self._messages = None

    def info(self) -> SourceInfo:
        messages = self.messages()
        stamps = [m.timestamp for m in messages if m.timestamp]
        return SourceInfo(
            source_id=self.source_id,
            title=self.title,
            kind=self.kind,
            message_count=len(messages),
            first_timestamp=min(stamps) if stamps else None,
            last_timestamp=max(stamps) if stamps else None,
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _summarize_args(args: Any) -> str:
    if not isinstance(args, dict):
        text = str(args)
        return text if len(text) <= SUMMARY_ARG_CHARS else text[:47] + "..."
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > SUMMARY_ARG_CHARS:
            text = text[: SUMMARY_ARG_CHARS - 3] + "..."
        parts.append(f'{key}="{text}"')
    return ", ".join(parts)


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def _render_block(block: dict, granularity: Granularity) -> str | None:  # noqa: PLR0911
    kind = block["type"]
    detailed = granularity != Granularity.CONVERSATION
    full = granularity == Granularity.FULL
    if kind == "text":
        return block["text"]
    if kind == "attachment":
        return block["label"]
    if kind == "thinking" and detailed and block["text"].strip():
        return f"<thinking>{block['text']}</thinking>"
    if kind == "tool_use" and detailed:
        if full:
            args = json.dumps(block.get("input"), default=str)
            return f"[tool_call {block['name']} id={block.get('id', '')}] {args}"
        return f"[tool_call {block['name']}({_summarize_args(block.get('input'))})]"
    if kind == "tool_result" and detailed:
        name = block.get("name") or "tool"
        error = " ERROR" if block.get("is_error") else ""
        text = _result_text(block.get("content"))
        if full:
            return f"[tool_result {name}{error}] {text}"
        lines = len(text.strip().splitlines()) or 1
        preview = text.strip().replace("\n", " ")[:RESULT_PREVIEW_CHARS]
        return f"[tool_result {name}{error}: {lines} lines] {preview}"
    if kind == "context" and full:
        return f"[context] {block['text']}"
    return None


def render_body(message: HistoryMessage, granularity: Granularity) -> str:
    parts = [
        rendered
        for block in message.blocks
        if (rendered := _render_block(block, granularity)) is not None
    ]
    return "\n".join(parts)


def is_visible(message: HistoryMessage, granularity: Granularity) -> bool:
    return bool(render_body(message, granularity).strip())


def render_message(
    message: HistoryMessage,
    granularity: Granularity = Granularity.CONVERSATION,
    *,
    include_source: bool = True,
) -> str:
    """One message as text, headed with the keys needed to page from it."""
    stamp = (
        message.timestamp.isoformat(timespec="seconds") if message.timestamp else "-"
    )
    source = f"{message.source_id} " if include_source else ""
    header = f"[{source}L{message.line} {stamp} {message.role.upper()}]"
    return f"{header} {render_body(message, granularity)}"


def render_messages(
    messages: Iterable[HistoryMessage],
    granularity: Granularity = Granularity.CONVERSATION,
    *,
    include_source: bool = True,
) -> str:
    return "\n".join(
        render_message(m, granularity, include_source=include_source)
        for m in messages
        if is_visible(m, granularity)
    )


# ---------------------------------------------------------------------------
# Block normalization helpers
# ---------------------------------------------------------------------------


def as_aware(value: datetime | None) -> datetime | None:
    """Make a timestamp timezone-aware so DB and file timestamps compare.

    Naive values (DB rows when USE_TZ is False) are read in the default
    time zone.
    """
    if value is None or value.tzinfo is not None:
        return value
    from django.utils import timezone

    return timezone.make_aware(value, timezone.get_default_timezone())


def parse_timestamp(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_aware(parsed)


def db_timestamp(value: datetime | None) -> datetime | None:
    """Convert an aware timestamp to what a DateTimeField expects under USE_TZ."""
    from django.conf import settings
    from django.utils import timezone

    if value is None or settings.USE_TZ:
        return value
    return timezone.make_naive(value, timezone.get_default_timezone())


def _user_text_block(text: str) -> dict:
    stripped = text.lstrip()
    if stripped.startswith(_CONTEXT_PREFIXES):
        return {"type": "context", "text": text}
    return {"type": "text", "text": text}


def _claude_blocks(content: Any, role: str) -> list[dict]:
    """Normalize Anthropic-style content (string or block list)."""
    if isinstance(content, str):
        return [_user_text_block(content) if role == "user" else _text(content)]
    blocks = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            text = block.get("text", "")
            blocks.append(_user_text_block(text) if role == "user" else _text(text))
        elif kind == "thinking":
            blocks.append({"type": "thinking", "text": block.get("thinking", "")})
        elif kind == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input"),
                }
            )
        elif kind == "tool_result":
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": block.get("content", ""),
                    "is_error": bool(block.get("is_error")),
                }
            )
        elif kind in ("image", "document"):
            blocks.append({"type": "attachment", "label": f"[{kind} attachment]"})
    return blocks


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _name_tool_results(messages: list[HistoryMessage]) -> list[HistoryMessage]:
    """Copy each tool call's name onto its result, for readable summaries.

    Messages that hold only tool results get role "tool" whatever the
    provider called them, so every source reads the same.
    """
    names = {}
    for message in messages:
        if message.blocks and all(b["type"] == "tool_result" for b in message.blocks):
            message.role = "tool"
        for block in message.blocks:
            if block["type"] == "tool_use":
                names[block.get("id")] = block.get("name")
            elif block["type"] == "tool_result" and not block.get("name"):
                block["name"] = names.get(block.get("tool_use_id"), "")
    return messages


# ---------------------------------------------------------------------------
# Database sessions
# ---------------------------------------------------------------------------


class SessionSource(MessageSource):
    """A ConversationSession read from the database (Claude or OpenAI rows).

    ``first_line`` and ``before_line`` read only the messages in that range
    of sequences, for paging a long session.
    """

    kind = "session"

    def __init__(
        self,
        session: ConversationSession,
        *,
        first_line: int | None = None,
        before_line: int | None = None,
    ):
        super().__init__()
        self.session = session
        self.first_line = first_line
        self.before_line = before_line

    def _window(self, rows):
        if self.first_line is not None:
            rows = rows.filter(sequence__gte=self.first_line)
        if self.before_line is not None:
            rows = rows.filter(sequence__lt=self.before_line)
        return rows

    @property
    def source_id(self) -> str:
        return f"session:{self.session.pk}"

    @property
    def title(self) -> str:
        meta = self.session.metadata or {}
        return (
            meta.get("title")
            or meta.get("slug")
            or f"{self.session.engine_type} session"
        )

    def load(self) -> list[HistoryMessage]:
        from django_ergo.conversation.attachments import attachments_by_sequence
        from django_ergo.conversation.attachments import describe

        attachments = attachments_by_sequence(self.session)
        messages = self._claude_rows() or self._openai_rows()
        for message in messages:
            for row in attachments.get(message.line, []):
                message.blocks.insert(
                    0,
                    {
                        "type": "attachment",
                        "label": describe(row),
                        "id": str(row.id),
                        "kind": row.kind,
                        "media_type": row.media_type,
                    },
                )
        return _name_tool_results(messages)

    def _claude_rows(self) -> list[HistoryMessage]:
        from django_ergo.conversation.engines.claude_api import claude_message_dict

        rows = self._window(self.session.claude_messages).prefetch_related(
            "content_blocks"
        )
        return [
            HistoryMessage(
                source_id=self.source_id,
                line=row.sequence,
                role=row.role,
                blocks=_claude_blocks(claude_message_dict(row)["content"], row.role),
                timestamp=as_aware(row.created_at),
            )
            for row in rows
        ]

    def _openai_rows(self) -> list[HistoryMessage]:
        messages = []
        for row in self._window(self.session.openai_messages.all()):
            blocks: list[dict] = []
            if row.role == "system":
                blocks.append({"type": "context", "text": row.content or ""})
            elif row.role == "tool":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": row.tool_call_id or "",
                        "content": (
                            [
                                {"type": "text", "text": row.content or ""},
                                *row.images,
                            ]
                            if row.images
                            else row.content or ""
                        ),
                        "is_error": False,
                    }
                )
            elif row.content:
                blocks.append(
                    _user_text_block(row.content)
                    if row.role == "user"
                    else _text(row.content)
                )
            for call in row.tool_calls or []:
                function = call.get("function", {})
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = function.get("arguments")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": args,
                    }
                )
            messages.append(
                HistoryMessage(
                    source_id=self.source_id,
                    line=row.sequence,
                    role=row.role,
                    blocks=blocks,
                    timestamp=as_aware(row.created_at),
                )
            )
        return messages


# ---------------------------------------------------------------------------
# Transcripts on disk
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[tuple[int, dict]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append((line_no, record))
    return records


class FileSource(MessageSource):
    prefix = "file"

    def __init__(self, path: str | Path):
        super().__init__()
        self.path = Path(path)

    @property
    def source_id(self) -> str:
        return f"{self.prefix}:{self.path.stem}"

    @property
    def title(self) -> str:
        return str(self.path)

    def info(self) -> SourceInfo:
        info = super().info()
        info.metadata["path"] = str(self.path)
        return info


class ClaudeCodeSource(FileSource):
    """A Claude Code session transcript (~/.claude/projects/<project>/<id>.jsonl).

    Lines are JSONL line numbers. Claude Code writes one line per content
    block, so one API response can span several lines.
    """

    kind = "claude_code"
    prefix = "claude"

    def load(self) -> list[HistoryMessage]:
        messages = []
        for line_no, record in _read_jsonl(self.path):
            role = record.get("type")
            if role not in ("user", "assistant") or record.get("isMeta"):
                continue
            message = record.get("message") or {}
            blocks = _claude_blocks(message.get("content", ""), role)
            if not blocks:
                continue
            messages.append(
                HistoryMessage(
                    source_id=self.source_id,
                    line=line_no,
                    role=role,
                    blocks=blocks,
                    timestamp=parse_timestamp(record.get("timestamp")),
                )
            )
        return _name_tool_results(messages)


def codex_item(record: dict) -> dict | None:
    """Return the response item in a Codex rollout line (new or legacy format)."""
    if record.get("type") == "response_item":
        return record.get("payload") or None
    if record.get("type") in (
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "local_shell_call",
    ):
        return record
    return None


def codex_blocks(item: dict) -> tuple[str, list[dict]] | None:  # noqa: C901, PLR0912
    kind = item.get("type")
    if kind == "message":
        role = item.get("role", "assistant")
        if role == "developer":
            role = "system"
        blocks = []
        for part in item.get("content") or []:
            text = part.get("text")
            if text is None:
                continue
            if role == "user":
                blocks.append(_user_text_block(text))
            elif role == "system":
                blocks.append({"type": "context", "text": text})
            else:
                blocks.append(_text(text))
        return role, blocks
    if kind == "reasoning":
        summary = "\n".join(
            part.get("text", "") for part in item.get("summary") or []
        ).strip()
        if not summary:
            return None
        return "assistant", [{"type": "thinking", "text": summary}]
    if kind in ("function_call", "custom_tool_call", "local_shell_call"):
        raw = item.get("arguments", item.get("input", item.get("action")))
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            args = raw
        return "assistant", [
            {
                "type": "tool_use",
                "id": item.get("call_id") or item.get("id", ""),
                "name": item.get("name") or kind.removesuffix("_call"),
                "input": args,
            }
        ]
    if kind in ("function_call_output", "custom_tool_call_output"):
        output = item.get("output")
        if isinstance(output, dict):
            output = output.get("content", output)
        return "tool", [
            {
                "type": "tool_result",
                "tool_use_id": item.get("call_id", ""),
                "content": output if output is not None else "",
                "is_error": False,
            }
        ]
    return None


class CodexSource(FileSource):
    """A Codex CLI rollout (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl)."""

    kind = "codex"
    prefix = "codex"

    @property
    def source_id(self) -> str:
        stem = self.path.stem.removeprefix("rollout-")
        return f"{self.prefix}:{stem}"

    def load(self) -> list[HistoryMessage]:
        messages = []
        for line_no, record in _read_jsonl(self.path):
            item = codex_item(record)
            if item is None:
                continue
            parsed = codex_blocks(item)
            if parsed is None or not parsed[1]:
                continue
            role, blocks = parsed
            messages.append(
                HistoryMessage(
                    source_id=self.source_id,
                    line=line_no,
                    role=role,
                    blocks=blocks,
                    timestamp=parse_timestamp(record.get("timestamp")),
                )
            )
        return _name_tool_results(messages)


def detect_file_source(path: str | Path) -> FileSource | None:
    """Return the right source for a JSONL transcript, or None if unrecognized."""
    path = Path(path)
    if path.suffix != ".jsonl":
        return None
    for _, record in _read_jsonl(path)[:20]:
        if record.get("type") in (
            "session_meta",
            "response_item",
            "turn_context",
            "event_msg",
        ) or codex_item(record):
            return CodexSource(path)
        if "sessionId" in record or record.get("type") in ("user", "assistant"):
            return ClaudeCodeSource(path)
    return None


def sources_from_paths(paths: Iterable[str | Path]) -> list[FileSource]:
    """Collect transcript sources from files and directories (searched recursively)."""
    sources = []
    for raw in paths:
        path = Path(raw).expanduser()
        files = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
        for candidate in files:
            if source := detect_file_source(candidate):
                sources.append(source)  # noqa: PERF401
    return sources


def default_cli_paths() -> list[Path]:
    """Where Claude Code and Codex keep transcripts for the current user."""
    home = Path.home()
    return [
        p
        for p in (home / ".claude" / "projects", home / ".codex" / "sessions")
        if p.exists()
    ]
