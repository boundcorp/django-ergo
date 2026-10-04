"""Move a chat to another engine by converting its stored history.

A session's messages live in its engine's tables (``OpenAIMessage`` or
``ClaudeMessage`` with content blocks). ``switch_engine`` rewrites them in the
other engine's format, so a chat can change to a model on another engine and
keep its history:

- OpenAI to Claude: system rows are dropped (Claude takes the system prompt
  per call), an assistant's ``tool_calls`` become ``tool_use`` blocks, and each
  ``tool`` row becomes a user message holding one ``tool_result``.
- Claude to OpenAI: a system row goes first when the session has a system
  prompt, ``tool_use`` blocks become ``tool_calls``, each ``tool_result`` becomes
  a ``tool`` row, and thinking (which OpenAI can't take back) is dropped.

A tool call with no stored result (a turn that died) gets an error result, as
both APIs refuse a call without one. Rows are renumbered, and the sequence
numbers other rows point at (attachments, compactions, structured calls) move
with them. Runs the ORM: call it from sync code, while no turn is running.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field

from django.db import transaction

from django_ergo.conversation.images import is_ref

MISSING_RESULT = "(No result was recorded for this tool call.)"
# Claude refuses an empty text block; a user message with only files had none.
EMPTY_USER_TEXT = "(see the attached files)"
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "model_name",
)


@dataclass
class Turn:
    """One message in engine-neutral form; ``source`` is the sequence it came from."""

    role: str  # system, user, assistant or tool
    source: int
    text: str = ""
    calls: list[dict] = field(default_factory=list)  # {"id", "name", "input"}
    call_id: str = ""  # tool: the call it answers
    images: list[dict] = field(default_factory=list)  # tool: image references
    is_error: bool = False
    usage: dict = field(default_factory=dict)


def switch_engine(session, engine_type: str, transport_type: str = "api") -> bool:
    """Convert ``session``'s history to ``engine_type``. Returns whether it changed."""
    if session.engine_type == engine_type:
        if transport_type and session.transport_type != transport_type:
            session.transport_type = transport_type
            session.save(update_fields=["transport_type", "updated_at"])
        return False
    if engine_type not in ("openai", "claude"):
        msg = f"Can't move a chat to the {engine_type!r} engine"
        raise ValueError(msg)
    with transaction.atomic():
        turns = _read(session)
        if engine_type == "openai":
            turns = _with_system_first(session, turns)
        turns = _answer_unanswered_calls(turns)
        spans = _write(session, engine_type, turns)
        _delete(session)
        _renumber(session, spans)
        session.engine_type = engine_type
        session.transport_type = transport_type or session.transport_type
        session.session_id = ""
        session.save(
            update_fields=["engine_type", "transport_type", "session_id", "updated_at"]
        )
    return True


# -- reading -------------------------------------------------------------------


def _read(session) -> list[Turn]:
    if session.engine_type == "openai":
        return [t for row in session.openai_messages.all() for t in _from_openai(row)]
    rows = session.claude_messages.prefetch_related("content_blocks")
    return [t for row in rows for t in _from_claude(row)]


def _usage(row) -> dict:
    return {name: getattr(row, name, None) for name in USAGE_FIELDS}


def _from_openai(row) -> list[Turn]:
    if row.role == "tool":
        return [
            Turn(
                "tool",
                row.sequence,
                text=row.content or "",
                call_id=row.tool_call_id or "",
                images=list(row.images or []),
            )
        ]
    calls = []
    for call in row.tool_calls or []:
        function = call.get("function") or {}
        arguments = function.get("arguments") or "{}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            args = {"arguments": arguments}
        calls.append(
            {
                "id": call.get("id") or "",
                "name": function.get("name") or "",
                "input": args if isinstance(args, dict) else {"value": args},
            }
        )
    text = row.content or ""
    if row.role == "assistant" and not (text or calls):
        return []  # nothing either API would take back
    usage = _usage(row) if row.role == "assistant" else {}
    return [Turn(row.role, row.sequence, text=text, calls=calls, usage=usage)]


def _from_claude(row) -> list[Turn]:
    texts, calls, results = [], [], []
    for block in row.content_blocks.all():
        if block.block_type == "text" and block.text:
            texts.append(block.text)
        elif block.block_type == "tool_use":
            calls.append(
                {
                    "id": block.tool_use_id or "",
                    "name": block.tool_name or "",
                    "input": block.tool_input or {},
                }
            )
        elif block.block_type == "tool_result":
            text, images = _split_result(block.tool_result_content)
            results.append(
                Turn(
                    "tool",
                    row.sequence,
                    text=text,
                    call_id=block.tool_result_for or "",
                    images=images,
                    is_error=block.is_error,
                )
            )
        # Thinking stays behind: the other engine can't take it back.
    turns = list(
        results
    )  # results answer the previous assistant message: they go first
    text = "\n\n".join(texts)
    if row.role == "assistant":
        if text or calls:
            turns.append(
                Turn(
                    "assistant", row.sequence, text=text, calls=calls, usage=_usage(row)
                )
            )
    elif text:
        turns.append(Turn("user", row.sequence, text=text))
    return turns


def _split_result(content) -> tuple[str, list[dict]]:
    if not isinstance(content, list):
        return ("" if content is None else str(content)), []
    texts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    images = [b for b in content if is_ref(b)]
    return "\n".join(t for t in texts if t), images


def _with_system_first(session, turns: list[Turn]) -> list[Turn]:
    """OpenAI keeps the system prompt as the first row (see its start_session)."""
    from django_ergo.conversation.engine import session_system_prompt

    turns = [t for t in turns if t.role != "system"]
    system = session_system_prompt(session)
    return [Turn("system", -1, text=system), *turns] if system else turns


def _answer_unanswered_calls(turns: list[Turn]) -> list[Turn]:
    """Give every tool call a result right after its message, as both APIs require."""
    out: list[Turn] = []
    pending: list[dict] = []
    answered: set[str] = set()

    def close():
        for call in pending:
            if call["id"] not in answered:
                out.append(
                    Turn(
                        "tool",
                        out[-1].source,
                        text=MISSING_RESULT,
                        call_id=call["id"],
                        is_error=True,
                    )
                )
        pending.clear()

    for turn in turns:
        if turn.role == "tool":
            answered.add(turn.call_id)
            out.append(turn)
            continue
        close()
        out.append(turn)
        if turn.role == "assistant" and turn.calls:
            pending = list(turn.calls)
            answered = set()
    close()
    return out


# -- writing -------------------------------------------------------------------


def _write(session, engine_type: str, turns: list[Turn]) -> dict[int, tuple[int, int]]:
    """Store ``turns``; returns each source sequence's (first, last) new sequence."""
    spans: dict[int, tuple[int, int]] = {}
    write = _write_openai if engine_type == "openai" else _write_claude
    for seq, turn in enumerate(
        turns if engine_type == "openai" else [t for t in turns if t.role != "system"]
    ):
        write(session, seq, turn)
        if turn.source >= 0:
            first, _ = spans.get(turn.source, (seq, seq))
            spans[turn.source] = (first, seq)
    return spans


def _write_openai(session, seq: int, turn: Turn) -> None:
    from django_ergo.conversation.models import OpenAIMessage

    calls = [
        {
            "id": c["id"],
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["input"])},
        }
        for c in turn.calls
    ]
    usage = {k: v for k, v in turn.usage.items() if v is not None}
    OpenAIMessage.objects.create(
        session=session,
        role=turn.role,
        content=turn.text if turn.text or not calls else None,
        tool_calls=calls or None,
        tool_call_id=turn.call_id or None,
        images=turn.images or None,
        sequence=seq,
        **usage,
    )


def _write_claude(session, seq: int, turn: Turn) -> None:
    from django_ergo.conversation.images import result_content
    from django_ergo.conversation.models import ClaudeContentBlock
    from django_ergo.conversation.models import ClaudeMessage

    if turn.role == "assistant":
        usage = {k: v for k, v in turn.usage.items() if v is not None}
        message = ClaudeMessage.objects.create(
            session=session,
            role="assistant",
            sequence=seq,
            stop_reason="tool_use" if turn.calls else "end_turn",
            **usage,
        )
        blocks = [{"block_type": "text", "text": turn.text}] if turn.text else []
        blocks += [
            {
                "block_type": "tool_use",
                "tool_use_id": c["id"],
                "tool_name": c["name"],
                "tool_input": c["input"],
            }
            for c in turn.calls
        ]
    elif turn.role == "tool":
        message = ClaudeMessage.objects.create(
            session=session, role="user", sequence=seq
        )
        content = result_content(turn.text, turn.images) if turn.images else turn.text
        blocks = [
            {
                "block_type": "tool_result",
                "tool_result_for": turn.call_id,
                "tool_result_content": content,
                "is_error": turn.is_error,
            }
        ]
    else:
        message = ClaudeMessage.objects.create(
            session=session, role="user", sequence=seq
        )
        blocks = [{"block_type": "text", "text": turn.text or EMPTY_USER_TEXT}]
    ClaudeContentBlock.objects.bulk_create(
        ClaudeContentBlock(message=message, sequence=i, **block)
        for i, block in enumerate(blocks)
    )


def _delete(session) -> None:
    """Drop the old engine's rows (the new ones are written in the other table)."""
    if session.engine_type == "openai":
        session.openai_messages.all().delete()
    else:
        session.claude_messages.all().delete()


# -- sequence numbers elsewhere ------------------------------------------------


def _renumber(session, spans: dict[int, tuple[int, int]]) -> None:
    """Move the sequence numbers that point into the history to the new rows."""
    sources = sorted(spans)

    def first_at_or_after(old):
        later = [spans[s][0] for s in sources if s >= old]
        return min(later) if later else None

    def last_at_or_before(old):
        earlier = [spans[s][1] for s in sources if s <= old]
        return max(earlier) if earlier else -1

    for row in session.attachments.exclude(message_sequence__isnull=True):
        new = spans.get(
            row.message_sequence, (first_at_or_after(row.message_sequence),)
        )[0]
        row.message_sequence = new
        row.save(update_fields=["message_sequence"])
    for row in session.compactions.all():
        row.from_sequence = first_at_or_after(row.from_sequence) or 0
        row.upto_sequence = last_at_or_before(row.upto_sequence)
        row.save(update_fields=["from_sequence", "upto_sequence"])
    for row in session.structured_calls.exclude(
        first_sequence__isnull=True, last_sequence__isnull=True
    ):
        if row.first_sequence is not None:
            row.first_sequence = first_at_or_after(row.first_sequence)
        if row.last_sequence is not None:
            row.last_sequence = last_at_or_before(row.last_sequence)
        row.save(update_fields=["first_sequence", "last_sequence"])
