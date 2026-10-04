"""Conversation compaction: replace older messages in model context with a summary.

A session's ``compaction_mode`` decides when to compact:

- ``time``: the session sat idle longer than ``idle_seconds`` before a new
  message. Everything so far is folded (``keep_recent`` defaults to 0).
- ``context_size``: the last model call's prompt plus output exceeded
  ``max_context_tokens``. Older messages are folded, keeping ``keep_recent``.
- ``rolling``: summaries roll up in batches. Once more than
  ``keep_recent + batch`` messages sit past the last summary and the last
  model call's prompt reached ``min_tokens``, everything but the latest
  ``keep_recent`` is folded into a new summary. (Formerly named ``stream``;
  that value is still accepted and read as ``rolling``.)

Every compaction rewrites the start of the prompt, so the next call pays for
a fresh prompt-cache write instead of cheap cached reads. ``min_tokens`` keeps
rolling compaction from firing on small contexts, where that costs more than
it saves (tool-heavy turns add many messages but few tokens).

Compaction never deletes messages. ``ConversationCompaction`` records the
summary and the sequence it covers, and engines substitute it when they
rebuild context. History tools still see every message.

Each summary is a structured call (kind ``compaction``) that returns a
``CompactionSummary``. The compaction row links to that call, so token use,
failures and retries are on record. Each summary folds in the previous one,
so only the newest compaction is used. The cut always falls before a user message that starts a turn, so tool
calls are never separated from their results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.utils import timezone
from pydantic import BaseModel
from pydantic import Field

from django_ergo.conversation.renderer import ConversationRenderer

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from datetime import datetime

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.models import ConversationCompaction
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.structured import StructuredCallResult

    # Returns summary text, or a structured call result whose parsed value
    # has render() (see CompactionSummary).
    Summarizer = Callable[[str, str], Awaitable["str | StructuredCallResult"]]

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "time": {"idle_seconds": 3600, "keep_recent": 0},
    "context_size": {"max_context_tokens": 100_000, "keep_recent": 6},
    "rolling": {"keep_recent": 15, "batch": 10, "min_tokens": 80_000},
}
DEFAULT_CONFIG["stream"] = DEFAULT_CONFIG["rolling"]  # deprecated alias

SUMMARY_SYSTEM = """\
You maintain a running summary of a conversation so it can continue without \
the full transcript. Given the previous summary (possibly empty) and the \
newer messages, write an updated summary that keeps:
- what the user wants and any decisions or constraints they gave
- facts, names, numbers, file paths and identifiers that may be needed later
- what was done (including what tools found) and what is still open

Drop pleasantries, repetition and tool output detail. Write plain prose or \
short lists. Do not address the user."""


class CompactionSummary(BaseModel):
    """What a compaction call returns."""

    summary: str = Field(description="The running summary, in prose or short lists")
    decisions: list[str] = Field(
        default_factory=list, description="Decisions and constraints the user gave"
    )
    open_items: list[str] = Field(
        default_factory=list, description="Questions or work still open"
    )

    def render(self) -> str:
        parts = [self.summary.strip()]
        if self.decisions:
            parts.append("Decisions:\n" + "\n".join(f"- {d}" for d in self.decisions))
        if self.open_items:
            parts.append("Open:\n" + "\n".join(f"- {o}" for o in self.open_items))
        return "\n\n".join(p for p in parts if p)


@dataclass(frozen=True)
class CompactionDecision:
    reason: str
    keep_recent: int


def compaction_config(session: ConversationSession) -> dict:
    """Return the session's compaction parameters merged over the mode defaults."""
    from django_ergo.conversation.models import normalize_compaction_mode

    defaults = DEFAULT_CONFIG.get(
        normalize_compaction_mode(session.compaction_mode), {}
    )
    return {**defaults, **(session.compaction_config or {})}


def latest_compaction(session) -> ConversationCompaction | None:
    """Return the compaction engines should apply, or None.

    Sessions with compaction turned off always get full history.
    """
    from django_ergo.conversation.models import CompactionMode
    from django_ergo.conversation.models import ConversationSession

    if not isinstance(session, ConversationSession):
        return None
    if session.compaction_mode == CompactionMode.NONE:
        return None
    return session.compactions.order_by("-upto_sequence").first()


def render_summary_message(compaction: ConversationCompaction) -> str:
    return (
        "<conversation-summary>\n"
        f"Messages up to #{compaction.upto_sequence} of this conversation were "
        "compacted into this summary. The full messages are still stored and "
        "can be read with history tools.\n\n"
        f"{compaction.summary}\n"
        "</conversation-summary>"
    )


def apply_native_window(session, messages: list[dict]) -> list[dict]:
    """Trim engine context to the current turn when the session asks for it.

    With ``compaction_config["native_history"] == "turn"`` the engine only
    replays messages from the latest user turn onward (plus system
    messages). Earlier history reaches the model through a context builder
    and history tools instead; see ``conversation.window.WindowChat``.

    A user message that follows tool results (a steering message, or the next
    message after a stopped turn) continues that turn rather than starting one,
    so the model keeps the tool work it was in the middle of.
    """
    from django_ergo.conversation.models import ConversationSession

    if not isinstance(session, ConversationSession):
        return messages
    if (session.compaction_config or {}).get("native_history") != "turn":
        return messages
    system = [m for m in messages if m.get("role") == "system"]
    start = native_turn_start(messages) or 0
    return system + [m for m in messages[start:] if m.get("role") != "system"]


def native_turn_start(messages: list[dict], *, incoming: bool = False) -> int | None:
    """Index in ``messages`` where the per-turn native window starts.

    None when no message starts a turn (the whole history is the turn).
    With ``incoming``, a new user message is about to be added: the window
    starts there (``len(messages)``) unless it continues a turn left in the
    middle of tool work. Context builders use this to leave out what the
    engine already sends natively.
    """
    rest = [(i, m) for i, m in enumerate(messages) if m.get("role") != "system"]
    if incoming:
        rest.append((len(messages), {"role": "user", "content": [{"type": "text"}]}))
    starts = []
    continuing = False
    for k, (i, message) in enumerate(rest):
        if _is_turn_start(message):
            previous = rest[k - 1][1] if k else None
            continuing = previous is not None and (
                _is_tool_results(previous) or continuing
            )
            if not continuing:
                starts.append(i)
        elif message.get("role") != "user":
            continuing = False
    return starts[-1] if starts else None


def _is_tool_results(message: dict) -> bool:
    """True for tool results: an OpenAI tool message, or a Claude user message of them."""
    if message.get("role") == "tool":
        return True
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and any(block.get("type") == "tool_result" for block in content)
    )


def _message_rows(session: ConversationSession):
    if session.engine_type == "openai":
        return session.openai_messages
    return session.claude_messages


def _is_turn_start(message: dict) -> bool:
    """True for a user message carrying text (not tool results)."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(block.get("type") == "text" for block in content)
    return False


def _prompt_tokens(row) -> int:
    return sum(
        getattr(row, name, None) or 0
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


async def decide_compaction(  # noqa: C901, PLR0911
    session: ConversationSession,
    *,
    now: datetime | None = None,
) -> CompactionDecision | None:
    """Decide whether the session should be compacted before its next message."""
    from django_ergo.conversation.models import CompactionMode
    from django_ergo.conversation.models import normalize_compaction_mode

    mode = normalize_compaction_mode(session.compaction_mode)
    if mode == CompactionMode.NONE:
        return None
    config = compaction_config(session)
    current = await sync_to_async(latest_compaction)(session)
    rows = _message_rows(session).exclude(role="system")
    if current:
        rows = rows.filter(sequence__gt=current.upto_sequence)

    if mode == CompactionMode.TIME:
        last = await rows.order_by("-sequence").afirst()
        if last is None:
            return None
        idle = (now or timezone.now()) - last.created_at
        if idle > timedelta(seconds=config["idle_seconds"]):
            return CompactionDecision(
                reason=f"idle for {int(idle.total_seconds())}s",
                keep_recent=config["keep_recent"],
            )
        return None

    if mode == CompactionMode.CONTEXT_SIZE:
        last = await rows.filter(role="assistant").order_by("-sequence").afirst()
        if last is None:
            return None
        size = _prompt_tokens(last)
        if size > config["max_context_tokens"]:
            return CompactionDecision(
                reason=f"context reached {size} tokens",
                keep_recent=config["keep_recent"],
            )
        return None

    if mode == CompactionMode.ROLLING:
        count = await rows.acount()
        if count <= config["keep_recent"] + config["batch"]:
            return None
        size = 0
        if config["min_tokens"]:
            last = await rows.filter(role="assistant").order_by("-sequence").afirst()
            size = _prompt_tokens(last) if last else 0
            if size < config["min_tokens"]:
                return None
        return CompactionDecision(
            reason=f"{count} messages past the last summary"
            + (f", context {size} tokens" if size else ""),
            keep_recent=config["keep_recent"],
        )

    msg = f"Unknown compaction mode: {mode}"
    raise ValueError(msg)


def structured_summarizer(engine: Engine, session: ConversationSession) -> Summarizer:
    """Default summarizer: a standalone structured call on the session's engine."""

    async def summarize(previous_summary: str, transcript: str):
        from django_ergo.conversation.structured import StructuredCallSpec
        from django_ergo.conversation.structured import run_structured_call

        spec = StructuredCallSpec(
            kind="compaction",
            system_prompt=SUMMARY_SYSTEM,
            response_model=CompactionSummary,
            max_turns=3,
        )
        prompt = (
            f"Previous summary:\n{previous_summary or '(none)'}\n\n"
            f"Newer messages:\n{transcript}"
        )
        return await run_structured_call(
            spec,
            prompt,
            user=await sync_to_async(lambda: session.user)(),
            engine=engine,
            metadata={"compacted_session": str(session.pk)},
        )

    return summarize


async def compact_session(
    session: ConversationSession,
    engine: Engine,
    *,
    keep_recent: int,
    reason: str = "manual",
    summarizer: Summarizer | None = None,
) -> ConversationCompaction | None:
    """Fold all but the latest ``keep_recent`` messages into a new summary.

    Returns None when there is nothing to fold.
    """
    from django_ergo.conversation.models import ConversationCompaction
    from django_ergo.conversation.models import normalize_compaction_mode

    current = await sync_to_async(latest_compaction)(session)
    after = current.upto_sequence if current else None
    rows = await sync_to_async(engine.history_rows)(session, after)
    rows = [(row, message) for row, message in rows if message["role"] != "system"]

    target = max(len(rows) - keep_recent, 0)
    # Move the cut back to the nearest turn start so the kept tail opens on
    # a user message and no tool result is cut off from its call.
    boundary = target
    while 0 < boundary < len(rows) and not _is_turn_start(rows[boundary][1]):
        boundary -= 1
    if boundary <= 0:
        return None

    folded = rows[:boundary]
    transcript = ConversationRenderer(detail="skeleton").render_messages(
        [message for _, message in folded]
    )
    summarize = summarizer or structured_summarizer(engine, session)
    outcome = await summarize(current.summary if current else "", transcript)
    call = None
    if isinstance(outcome, str):
        summary = outcome.strip()
    else:
        call = outcome.call
        if not outcome.ok:
            log.warning(
                "compaction of session %s failed: %s", session.pk, outcome.error
            )
            return None
        parsed = outcome.parsed
        summary = parsed.render() if hasattr(parsed, "render") else str(parsed)
    if not summary:
        log.warning("compaction of session %s produced an empty summary", session.pk)
        return None

    return await ConversationCompaction.objects.acreate(
        session=session,
        mode=normalize_compaction_mode(session.compaction_mode),
        reason=reason[:255],
        from_sequence=folded[0][0].sequence,
        upto_sequence=folded[-1][0].sequence,
        message_count=len(folded),
        summary=summary,
        structured_call=call,
    )


async def maybe_compact(
    session: ConversationSession,
    engine: Engine,
    *,
    now: datetime | None = None,
    summarizer: Summarizer | None = None,
) -> ConversationCompaction | None:
    """Compact the session if its policy says so. Never raises.

    Called before each turn. A failed summary leaves the session uncompacted
    rather than blocking the turn.
    """
    from django_ergo.conversation.models import ConversationSession

    if not isinstance(session, ConversationSession):
        return None
    try:
        decision = await decide_compaction(session, now=now)
        if decision is None:
            return None
        return await compact_session(
            session,
            engine,
            keep_recent=decision.keep_recent,
            reason=decision.reason,
            summarizer=summarizer,
        )
    except Exception:
        log.exception("compaction failed for session %s", session.pk)
        return None
