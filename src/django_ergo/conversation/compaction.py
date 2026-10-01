"""Conversation compaction: replace older messages in model context with a summary.

A session's ``compaction_mode`` decides when to compact:

- ``time``: the session sat idle longer than ``idle_seconds`` before a new
  message. Everything so far is folded (``keep_recent`` defaults to 0).
- ``context_size``: the last model call's prompt plus output exceeded
  ``max_context_tokens``. Older messages are folded, keeping ``keep_recent``.
- ``stream``: a rolling window. Once more than ``keep_recent + batch``
  messages sit past the last summary, everything but the latest
  ``keep_recent`` is folded into a new summary.

Compaction never deletes messages. ``ConversationCompaction`` records the
summary and the sequence it covers, and engines substitute it when they
rebuild context. History tools still see every message.

Each summary folds in the previous one, so only the newest compaction is
used. The cut always falls before a user message that starts a turn, so tool
calls are never separated from their results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from django.utils import timezone

from django_ergo.conversation.renderer import ConversationRenderer

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from datetime import datetime

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.models import ConversationCompaction
    from django_ergo.conversation.models import ConversationSession

    Summarizer = Callable[[str, str], Awaitable[str]]

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "time": {"idle_seconds": 3600, "keep_recent": 0},
    "context_size": {"max_context_tokens": 100_000, "keep_recent": 6},
    "stream": {"keep_recent": 15, "batch": 10},
}

SUMMARY_SYSTEM = """\
You maintain a running summary of a conversation so it can continue without \
the full transcript. Given the previous summary (possibly empty) and the \
newer messages, write an updated summary that keeps:
- what the user wants and any decisions or constraints they gave
- facts, names, numbers, file paths and identifiers that may be needed later
- what was done (including what tools found) and what is still open

Drop pleasantries, repetition and tool output detail. Write plain prose or \
short lists. Do not address the user."""


@dataclass(frozen=True)
class CompactionDecision:
    reason: str
    keep_recent: int


def compaction_config(session: ConversationSession) -> dict:
    """Return the session's compaction parameters merged over the mode defaults."""
    defaults = DEFAULT_CONFIG.get(session.compaction_mode, {})
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


async def decide_compaction(
    session: ConversationSession,
    *,
    now: datetime | None = None,
) -> CompactionDecision | None:
    """Decide whether the session should be compacted before its next message."""
    from django_ergo.conversation.models import CompactionMode

    mode = session.compaction_mode
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

    if mode == CompactionMode.STREAM:
        count = await rows.acount()
        if count > config["keep_recent"] + config["batch"]:
            return CompactionDecision(
                reason=f"{count} messages past the last summary",
                keep_recent=config["keep_recent"],
            )
        return None

    msg = f"Unknown compaction mode: {mode}"
    raise ValueError(msg)


def engine_summarizer(engine: Engine) -> Summarizer:
    """Default summarizer: one stateless generate() call on the session's engine."""

    async def summarize(previous_summary: str, transcript: str) -> str:
        prompt = (
            f"Previous summary:\n{previous_summary or '(none)'}\n\n"
            f"Newer messages:\n{transcript}"
        )
        response = await engine.generate(prompt=prompt, system=SUMMARY_SYSTEM)
        return (response.text or "").strip()

    return summarize


async def compact_session(  # noqa: PLR0913
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
    summarize = summarizer or engine_summarizer(engine)
    summary = await summarize(current.summary if current else "", transcript)
    if not summary:
        log.warning("compaction of session %s produced an empty summary", session.pk)
        return None

    return await ConversationCompaction.objects.acreate(
        session=session,
        mode=session.compaction_mode,
        reason=reason[:255],
        from_sequence=folded[0][0].sequence,
        upto_sequence=folded[-1][0].sequence,
        message_count=len(folded),
        summary=summary,
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
