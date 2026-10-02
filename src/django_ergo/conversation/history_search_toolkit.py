"""MessageHistoryToolkit: tools for an agent to page and search message history.

Works over any mix of sources (DB sessions, Claude Code and Codex transcripts
on disk). Scope it to one session to let a chat search its own past, or give
it many sources to search across them::

    toolkit = MessageHistoryToolkit([SessionSource(session)])
    toolkit = MessageHistoryToolkit(
        [SessionSource(s) for s in sessions]
        + sources_from_paths(default_cli_paths())
    )

Every read takes a ``granularity`` (conversation, reasoning, full) and prints
each message with its source id, line number and timestamp, which are the
keys the other tools take for paging.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from django_ergo.conversation.history import Granularity
from django_ergo.conversation.history import HistoryMessage
from django_ergo.conversation.history import MessageSource
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.history import is_visible
from django_ergo.conversation.history import render_message
from django_ergo.conversation.toolkit import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.conversation.adapters import ToolAdapter

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SNIPPET_CHARS = 300

_SOURCE = {
    "type": "string",
    "required": False,
    "description": "Source id from ergo_chat_history_sources. Optional when only one source exists.",
}
_GRANULARITY = {
    "type": "string",
    "required": False,
    "description": (
        "conversation (user and assistant messages only, default), reasoning "
        "(adds thinking and tool call summaries), or full (adds tool inputs "
        "and results verbatim)"
    ),
}
_LIMIT = {
    "type": "integer",
    "required": False,
    "description": f"Max messages to return (default {DEFAULT_LIMIT})",
}

TOOLS = [
    {
        "name": "ergo_chat_history_sources",
        "description": "List the conversations you can read, with message counts and date ranges.",
        "parameters": {},
    },
    {
        "name": "ergo_chat_history_read",
        "description": (
            "Read messages by line number. Give start_line to page forward, or "
            "only end_line to read the messages just before it."
        ),
        "parameters": {
            "source_id": _SOURCE,
            "start_line": {"type": "integer", "required": False},
            "end_line": {"type": "integer", "required": False},
            "limit": _LIMIT,
            "granularity": _GRANULARITY,
        },
    },
    {
        "name": "ergo_chat_history_tail",
        "description": "Read the latest messages of a conversation.",
        "parameters": {
            "source_id": _SOURCE,
            "limit": _LIMIT,
            "granularity": _GRANULARITY,
        },
    },
    {
        "name": "ergo_chat_history_around",
        "description": "Read the messages before and after a line, e.g. to expand a search hit.",
        "parameters": {
            "source_id": _SOURCE,
            "line": {"type": "integer", "required": True},
            "before": {"type": "integer", "required": False},
            "after": {"type": "integer", "required": False},
            "granularity": _GRANULARITY,
        },
    },
    {
        "name": "ergo_chat_history_by_date",
        "description": (
            "Read messages in a date range, oldest first, across all sources or "
            "one. Dates are ISO 8601 (2026-10-01 or 2026-10-01T14:00:00Z)."
        ),
        "parameters": {
            "since": {"type": "string", "required": False},
            "until": {"type": "string", "required": False},
            "source_id": _SOURCE,
            "limit": _LIMIT,
            "granularity": _GRANULARITY,
        },
    },
    {
        "name": "ergo_chat_history_search",
        "description": (
            "Find messages containing all the given words (case-insensitive), "
            "searching full content including tool calls. Newest first."
        ),
        "parameters": {
            "query": {"type": "string", "required": True},
            "source_id": _SOURCE,
            "since": {"type": "string", "required": False},
            "until": {"type": "string", "required": False},
            "limit": _LIMIT,
        },
    },
]
TOOL_NAMES = {tool["name"] for tool in TOOLS}


def _parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        msg = f"Invalid date {value!r}; use ISO 8601 like 2026-10-01T14:00:00Z"
        raise ValueError(msg) from None
    if end and len(value) == len("2026-10-01"):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _limit(arguments: dict, default: int = DEFAULT_LIMIT) -> int:
    value = arguments.get("limit") or default
    return max(1, min(int(value), MAX_LIMIT))


class MessageHistoryToolkit(Toolkit):
    def __init__(
        self,
        sources: list[MessageSource],
        *,
        default_granularity: Granularity | str = Granularity.CONVERSATION,
        source_loader: Callable[[], list[MessageSource]] | None = None,
    ):
        self.sources: dict[str, MessageSource] = {}
        for source in sources:
            self.add_source(source)
        self.default_granularity = Granularity.parse(default_granularity)
        # Called before each tool call to pick up sources created since,
        # e.g. new threads of a bot.
        self.source_loader = source_loader

    def _load_sources(self) -> None:
        if self.source_loader is None:
            return
        for source in self.source_loader():
            if source.source_id not in self.sources:
                self.add_source(source)

    def add_source(self, source: MessageSource) -> None:
        self.sources[source.source_id] = source

    # -- Toolkit protocol ---------------------------------------------------

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        from django_ergo.tools import ToolConfig

        return [
            adapter.to_engine_schema(
                ToolConfig(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=tool["parameters"],
                    readonly=True,
                )
            )
            for tool in TOOLS
        ]

    def render_overview(self) -> str:
        self._load_sources()
        return (
            "You can read and search earlier messages with the history_* tools. "
            "Messages are shown as [source L<line> <timestamp> ROLE]; pass those "
            "line numbers and timestamps back to page further.\n" + self._sources({})
        )

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        self._load_sources()
        handler = getattr(self, f"_{tool_name.removeprefix('ergo_chat_history_')}", None)
        if tool_name not in TOOL_NAMES or handler is None:
            msg = f"Unknown tool: {tool_name}"
            raise ValueError(msg)
        return handler(arguments or {})

    # -- helpers ------------------------------------------------------------

    def _source(self, arguments: dict) -> MessageSource:
        source_id = arguments.get("source_id")
        if not source_id:
            if len(self.sources) == 1:
                source = next(iter(self.sources.values()))
            else:
                msg = "source_id is required; call ergo_chat_history_sources to list them"
                raise ValueError(msg)
        else:
            source = self.sources.get(source_id)
            if source is None:
                msg = f"Unknown source {source_id!r}; call ergo_chat_history_sources to list them"
                raise ValueError(msg)
        if isinstance(source, SessionSource):
            source.refresh()  # live sessions keep growing
        return source

    def _all_sources(self, arguments: dict) -> list[MessageSource]:
        if arguments.get("source_id"):
            return [self._source(arguments)]
        for source in self.sources.values():
            if isinstance(source, SessionSource):
                source.refresh()
        return list(self.sources.values())

    def _granularity(self, arguments: dict) -> Granularity:
        return Granularity.parse(
            arguments.get("granularity") or self.default_granularity
        )

    def _render(
        self,
        messages: list[HistoryMessage],
        granularity: Granularity,
        *,
        include_source: bool,
    ) -> str:
        if not messages:
            return "(no messages)"
        return "\n".join(
            render_message(m, granularity, include_source=include_source)
            for m in messages
        )

    # -- tools --------------------------------------------------------------

    def _sources(self, arguments: dict) -> str:
        if not self.sources:
            return "No history sources."
        lines = []
        for source in self._all_sources({}):
            info = source.info()
            span = ""
            if info.first_timestamp:
                span = (
                    f", {info.first_timestamp.isoformat(timespec='seconds')} to "
                    f"{info.last_timestamp.isoformat(timespec='seconds')}"
                )
            lines.append(
                f"- {info.source_id} ({info.kind}): {info.title}; "
                f"{info.message_count} messages{span}"
            )
        return "History sources:\n" + "\n".join(lines)

    def _read(self, arguments: dict) -> str:
        source = self._source(arguments)
        granularity = self._granularity(arguments)
        limit = _limit(arguments)
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        visible = [m for m in source.messages() if is_visible(m, granularity)]

        if start is None and end is not None:
            before = [m for m in visible if m.line < int(end)]
            page = before[-limit:]
            notes = []
            if len(before) > len(page):
                notes.append(f"Earlier: ergo_chat_history_read end_line={page[0].line}")
        else:
            after = [
                m
                for m in visible
                if (start is None or m.line >= int(start))
                and (end is None or m.line <= int(end))
            ]
            page = after[:limit]
            notes = []
            if len(after) > len(page):
                notes.append(f"More: ergo_chat_history_read start_line={after[len(page)].line}")
        body = self._render(page, granularity, include_source=len(self.sources) > 1)
        return "\n".join([body, *notes])

    def _tail(self, arguments: dict) -> str:
        source = self._source(arguments)
        granularity = self._granularity(arguments)
        limit = _limit(arguments, default=15)
        visible = [m for m in source.messages() if is_visible(m, granularity)]
        page = visible[-limit:]
        body = self._render(page, granularity, include_source=len(self.sources) > 1)
        if len(visible) > len(page):
            body += f"\nEarlier: ergo_chat_history_read end_line={page[0].line}"
        return body

    def _around(self, arguments: dict) -> str:
        source = self._source(arguments)
        granularity = self._granularity(arguments)
        line = int(arguments["line"])
        before = int(5 if arguments.get("before") is None else arguments["before"])
        after = int(5 if arguments.get("after") is None else arguments["after"])
        messages = source.messages()
        visible = [m for m in messages if is_visible(m, granularity) or m.line == line]
        index = next((i for i, m in enumerate(visible) if m.line >= line), None)
        if index is None:
            msg = f"No message at or after line {line}"
            raise ValueError(msg)
        page = visible[max(index - before, 0) : index + after + 1]
        return self._render(page, granularity, include_source=len(self.sources) > 1)

    def _by_date(self, arguments: dict) -> str:
        granularity = self._granularity(arguments)
        since = _parse_date(arguments.get("since"))
        until = _parse_date(arguments.get("until"), end=True)
        limit = _limit(arguments)
        matches = [
            m
            for source in self._all_sources(arguments)
            for m in source.messages()
            if m.timestamp
            and (since is None or m.timestamp >= since)
            and (until is None or m.timestamp <= until)
            and is_visible(m, granularity)
        ]
        matches.sort(key=lambda m: (m.timestamp, m.source_id, m.line))
        page = matches[:limit]
        body = self._render(page, granularity, include_source=True)
        if len(matches) > len(page):
            next_since = matches[len(page)].timestamp.isoformat()
            body += f"\nMore: ergo_chat_history_by_date since={next_since}"
        return body

    def _search(self, arguments: dict) -> str:
        terms = [t.lower() for t in str(arguments["query"]).split() if t]
        if not terms:
            msg = "query is empty"
            raise ValueError(msg)
        since = _parse_date(arguments.get("since"))
        until = _parse_date(arguments.get("until"), end=True)
        limit = _limit(arguments)
        hits = []
        for source in self._all_sources(arguments):
            for message in source.messages():
                if since and (not message.timestamp or message.timestamp < since):
                    continue
                if until and (not message.timestamp or message.timestamp > until):
                    continue
                text = message.searchable_text()
                lowered = text.lower()
                if all(term in lowered for term in terms):
                    hits.append((message, text, lowered.index(terms[0])))
        if not hits:
            return "(no matches)"
        min_dt = datetime.min.replace(tzinfo=UTC)
        hits.sort(key=lambda h: (h[0].timestamp or min_dt, h[0].line), reverse=True)
        lines = []
        for message, text, position in hits[:limit]:
            start = max(position - SNIPPET_CHARS // 3, 0)
            snippet = text[start : start + SNIPPET_CHARS].replace("\n", " ")
            prefix = "..." if start else ""
            stamp = (
                message.timestamp.isoformat(timespec="seconds")
                if message.timestamp
                else "-"
            )
            lines.append(
                f"[{message.source_id} L{message.line} {stamp} "
                f"{message.role.upper()}] {prefix}{snippet}"
            )
        more = len(hits) - len(lines)
        if more > 0:
            lines.append(f"({more} more matches; narrow the query or dates)")
        lines.append("Expand a hit with ergo_chat_history_around source_id=... line=...")
        return "\n".join(lines)
