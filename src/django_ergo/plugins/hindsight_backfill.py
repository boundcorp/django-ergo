"""Backfill past chat turns into Hindsight, shaped exactly like live retain.

Read-only on the Ergonaut database. ``turn_items`` yields the same item the
``hindsight_memory`` plugin sends after a turn (``User:``/``Assistant:`` text,
the same filters and tags), with three differences that make a replay safe:

- ``document_id`` is ``ergonaut-turn-<call id>``, the id live retain uses, so
  replaying a turn (or a turn that was also retained live) replaces one
  document instead of duplicating it.
- ``timestamp`` is the turn's original time, not now.
- ``metadata.source_link`` links to the message (``<host>/s/<chat>#m-<line>``).

Tool calls, tool output and reasoning are never read (only ``text`` blocks),
and turns containing something that looks like a secret are dropped whole.
Nothing here calls Hindsight; ``estimate`` only counts.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.models import StructuredCall
from django_ergo.plugins.hindsight_memory import CHAT_CONTEXT
from django_ergo.plugins.hindsight_memory import CONFIG_READ_TOOLS
from django_ergo.plugins.hindsight_memory import chat_tags
from django_ergo.plugins.hindsight_memory import clean_assistant_text
from django_ergo.plugins.hindsight_memory import has_decision
from django_ergo.plugins.hindsight_memory import machine_turn

if TYPE_CHECKING:
    from datetime import datetime

# Hindsight splits a document into chunks of at most this many characters
# (HINDSIGHT_API_RETAIN_CHUNK_SIZE, default 3000) and makes one extraction call each.
CHUNK_CHARS = 3000
CHARS_PER_TOKEN = 4.0

_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk|pk|rk|fw|xox[abprs]|gh[pousr]|glpat|AKIA|AIza|ya29|eyJ)[-_A-Za-z0-9.]{16,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"
    r"|(?i:(?:api[_-]?key|secret|token|passw(?:or)?d|authorization)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,})"
    r"|\b[A-Fa-f0-9]{40,}\b"
)


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


@dataclass
class Turn:
    call_id: str
    line: int
    at: datetime
    content: str
    skipped: str = ""

    @property
    def document_id(self) -> str:
        return f"ergonaut-turn-{self.call_id}"

    @property
    def chunks(self) -> int:
        return max(1, -(-len(self.content) // CHUNK_CHARS))


def _tools(session, call) -> set[str]:
    from django_ergo.conversation.models import MessageBlock

    return set(
        MessageBlock.objects.filter(
            message__session=session,
            message__sequence__gte=call.first_sequence,
            message__sequence__lte=call.last_sequence,
            block_type="tool_use",
        ).values_list("tool_name", flat=True)
    )


def turns(session, *, telegram_users=()):
    """Every completed turn of a session as a ``Turn`` (``skipped`` says why not)."""
    calls = StructuredCall.objects.filter(
        session=session, kind="chat_reply", first_sequence__isnull=False
    ).order_by("first_sequence")
    for call in calls:

        def skip(reason, call=call):
            return Turn(str(call.pk), call.first_sequence, call.created_at, "", reason)

        if call.status != "completed":
            yield skip("not_completed")
            continue
        if reason := machine_turn(call.metadata, {str(u) for u in telegram_users}):
            yield skip(reason)
            continue
        history = SessionSource(
            session,
            first_line=call.first_sequence,
            before_line=(call.last_sequence or call.first_sequence) + 1,
            include_attribution=False,
        ).load()
        user = next((m for m in history if m.role == "user" and m.text().strip()), None)
        replies = [m for m in history if m.role == "assistant" and m.text().strip()]
        if user is None or not replies:
            yield skip("empty")
            continue
        message = user.text().strip()
        tools = _tools(session, call)
        if tools and tools <= CONFIG_READ_TOOLS and not has_decision(message):
            yield skip("config_only")
            continue
        reply = clean_assistant_text(replies[-1].text())
        content = f"User: {message}" + (f"\nAssistant: {reply}" if reply else "")
        if looks_secret(content):
            yield skip("secret")
            continue
        yield Turn(
            str(call.pk),
            call.first_sequence,
            user.timestamp or call.created_at,
            content,
        )


def build_item(bot: str, session, turn: Turn, *, host: str) -> dict:
    """The retain item for a turn: what live retain would have sent, with its
    original timestamp and a link back to the message."""
    tags = chat_tags(bot, session, "chat")
    metadata = {tag.split(":", 1)[0]: tag.split(":", 1)[1] for tag in tags}
    metadata["backfill"] = "true"
    metadata["source_link"] = f"https://{host}/s/{session.id}#m-{turn.line}"
    return {
        "content": turn.content,
        "timestamp": turn.at.isoformat(),
        "context": CHAT_CONTEXT.format(bot=bot),
        "tags": tags,
        "metadata": metadata,
        "document_id": turn.document_id,
    }


def estimate(items: list[Turn]) -> dict:
    """Counts only: documents, chunks (= extraction calls), chars and tokens."""
    kept = [t for t in items if not t.skipped]
    chars = sum(len(t.content) for t in kept)
    return {
        "documents": len(kept),
        "chunks": sum(t.chunks for t in kept),
        "chars": chars,
        "tokens": round(chars / CHARS_PER_TOKEN),
        "skipped": dict(Counter(t.skipped for t in items if t.skipped)),
    }
