"""Context builder: assemble model context from weighted sources within a token budget.

A ``ContextBuilder`` holds sources, each with a weight. ``build()`` splits the
token budget by weight, asks each source to render itself within its share,
hands unused budget to sources that ran out of room, and joins the sections.

Message sources choose granularity and message count on their own: they try
the most detailed granularity allowed and step down until at least
``min_messages`` of the latest messages fit, then include as many more as the
budget allows. Each rendered message carries its source id, line number and
timestamp, and each section ends with the history-tool call that reads
further back, so the model can page with ``MessageHistoryToolkit``.

    builder = ContextBuilder(budget_tokens=8000)
    builder.add(MessageContextSource(SessionSource(session), weight=3, min_messages=15))
    builder.add(TextContextSource("Kitchen notes", kb_notes, weight=1))
    context = builder.build()
    context.text  # put this in front of the model
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from django_ergo.conversation.compaction import native_turn_start
from django_ergo.conversation.history import Granularity
from django_ergo.conversation.history import is_visible
from django_ergo.conversation.history import render_message

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.conversation.history import HistoryMessage
    from django_ergo.conversation.history import MessageSource

CHARS_PER_TOKEN = 4
_DETAIL_ORDER = [Granularity.FULL, Granularity.REASONING, Granularity.CONVERSATION]


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass
class ContextSection:
    title: str
    body: str
    tokens: int
    complete: bool = True  # False when the source had more than fit
    details: dict = field(default_factory=dict)

    def render(self) -> str:
        return f"## {self.title}\n{self.body}"


class ContextSource(ABC):
    weight: float = 1.0

    @abstractmethod
    def render(self, budget_tokens: int) -> ContextSection | None:
        """Render within budget_tokens, or return None if there is nothing to show."""


class TextContextSource(ContextSource):
    """Static or computed text, such as KB results or a profile. Trimmed to fit."""

    def __init__(
        self,
        title: str,
        text: str | Callable[[], str],
        *,
        weight: float = 1.0,
    ):
        self.title = title
        self.text = text
        self.weight = weight

    def render(self, budget_tokens: int) -> ContextSection | None:
        text = self.text() if callable(self.text) else self.text
        if not text or not text.strip():
            return None
        tokens = estimate_tokens(text)
        if tokens <= budget_tokens:
            return ContextSection(self.title, text, tokens)
        cut = max(budget_tokens * CHARS_PER_TOKEN - len(" [truncated]"), 0)
        body = text[:cut].rstrip() + " [truncated]"
        return ContextSection(self.title, body, estimate_tokens(body), complete=False)


class MessageContextSource(ContextSource):
    """The latest messages of a conversation, at the richest granularity that fits.

    Args:
        min_messages: messages to keep even if that means less detail.
        max_messages: never include more than this many.
        max_granularity: the most detail the builder may choose.
        granularity: fix the granularity instead of choosing.
        before_line: only messages before this line.
        skip_native_turn: leave out the messages a session with
            ``native_history="turn"`` already sends natively (the current
            turn, or a turn the next message continues), so they aren't in
            the model's context twice.
        incoming: with ``skip_native_turn``, whether a new user message is
            about to start the turn (True when building for a new message,
            False when resuming a turn that is already stored).
    """

    def __init__(  # noqa: PLR0913
        self,
        source: MessageSource,
        *,
        weight: float = 1.0,
        title: str = "",
        min_messages: int = 1,
        max_messages: int | None = None,
        max_granularity: Granularity | str = Granularity.REASONING,
        granularity: Granularity | str | None = None,
        before_line: int | None = None,
        skip_native_turn: bool = False,
        incoming: bool = True,
    ):
        self.source = source
        self.weight = weight
        self.title = title or f"Messages from {source.source_id}"
        self.min_messages = min_messages
        self.max_messages = max_messages
        self.max_granularity = Granularity.parse(max_granularity)
        self.granularity = Granularity.parse(granularity) if granularity else None
        self.before_line = before_line
        self.skip_native_turn = skip_native_turn
        self.incoming = incoming

    def _candidates(self) -> list[Granularity]:
        if self.granularity:
            return [self.granularity]
        start = _DETAIL_ORDER.index(self.max_granularity)
        return _DETAIL_ORDER[start:]

    def _messages(self) -> list[HistoryMessage]:
        messages = self.source.messages()
        if self.before_line is not None:
            messages = [m for m in messages if m.line < self.before_line]
        if self.skip_native_turn:
            start = native_turn_start(
                [_api_shape(m) for m in messages], incoming=self.incoming
            )
            messages = messages[: start or 0]
        return messages

    def render(self, budget_tokens: int) -> ContextSection | None:
        messages = self._messages()
        if not messages:
            return None
        candidates = self._candidates()
        chosen = None
        for granularity in candidates:
            lines = self._fit(messages, granularity, budget_tokens)
            enough = len(lines) >= min(
                self.min_messages, self._visible(messages, granularity)
            )
            if enough or granularity == candidates[-1]:
                chosen = (granularity, lines)
                break
        granularity, lines = chosen
        if not lines:
            return None
        lines.reverse()
        visible_total = self._visible(messages, granularity)
        first_line = lines[0][0]
        complete = len(lines) >= visible_total and not any(
            "… [truncated" in text for _, text in lines
        )
        body_lines = [text for _, text in lines]
        if not complete:
            body_lines.append(
                f"(Earlier messages: ergo_chat_history_read source_id={self.source.source_id} "
                f"end_line={first_line})"
            )
        if granularity != Granularity.FULL:
            body_lines.append(
                "(More detail: ergo_chat_history_around or ergo_chat_history_read with "
                "granularity=reasoning or full)"
            )
        body = "\n".join(body_lines)
        title = (
            f"{self.title} (latest {len(lines)} of {visible_total}, "
            f"granularity: {granularity.value})"
        )
        return ContextSection(
            title,
            body,
            estimate_tokens(body) + estimate_tokens(title),
            complete=complete,
            details={"granularity": granularity.value, "count": len(lines)},
        )

    @staticmethod
    def _visible(messages: list[HistoryMessage], granularity: Granularity) -> int:
        return sum(1 for m in messages if is_visible(m, granularity))

    def _fit(
        self,
        messages: list[HistoryMessage],
        granularity: Granularity,
        budget_tokens: int,
    ) -> list[tuple[int, str]]:
        """Newest-first (line, text) pairs that fit in the budget."""
        selected: list[tuple[int, str]] = []
        used = 0
        for message in reversed(messages):
            if not is_visible(message, granularity):
                continue
            if self.max_messages is not None and len(selected) >= self.max_messages:
                break
            text = render_message(message, granularity, include_source=False)
            cost = estimate_tokens(text) + 1
            if used + cost > budget_tokens:
                if not selected and budget_tokens > 0:
                    suffix = "… [truncated, read it with ergo_chat_history_read]"
                    size = max(0, budget_tokens * CHARS_PER_TOKEN - len(suffix))
                    selected.append(
                        (
                            message.line,
                            (text[:size] + suffix)[: budget_tokens * CHARS_PER_TOKEN],
                        )
                    )
                break
            selected.append((message.line, text))
            used += cost
        return selected


def _api_shape(message: HistoryMessage) -> dict:
    """Enough of an API message for native_turn_start to read."""
    blocks = [
        {**b, "type": "text"} if b.get("type") == "context" else b
        for b in message.blocks
    ]
    return {"role": message.role, "content": blocks}


@dataclass
class BuiltContext:
    sections: list[ContextSection]
    budget_tokens: int

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.sections)

    @property
    def text(self) -> str:
        if not self.sections:
            return ""
        body = "\n\n".join(s.render() for s in self.sections)
        return f"<context>\n{body}\n</context>"


class ContextBuilder:
    def __init__(self, budget_tokens: int = 8000):
        self.budget_tokens = budget_tokens
        self.sources: list[ContextSource] = []

    def add(self, source: ContextSource) -> ContextBuilder:
        self.sources.append(source)
        return self

    def _shares(self, sources: list[ContextSource], budget: int) -> list[int]:
        total = sum(max(s.weight, 0) for s in sources) or 1
        return [int(budget * max(s.weight, 0) / total) for s in sources]

    def build(self) -> BuiltContext:
        shares = self._shares(self.sources, self.budget_tokens)
        sections: list[ContextSection | None] = [
            source.render(share)
            for source, share in zip(self.sources, shares, strict=True)
        ]
        # Hand budget left over by small sources to those that ran out of room.
        spare = self.budget_tokens - sum(s.tokens for s in sections if s)
        hungry = [
            i for i, section in enumerate(sections) if section and not section.complete
        ]
        if spare > 0 and hungry:
            extra = self._shares([self.sources[i] for i in hungry], spare)
            for index, bonus in zip(hungry, extra, strict=True):
                sections[index] = (
                    self.sources[index].render(shares[index] + bonus) or sections[index]
                )
        return BuiltContext(
            sections=[s for s in sections if s], budget_tokens=self.budget_tokens
        )
