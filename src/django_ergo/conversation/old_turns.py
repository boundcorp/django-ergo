"""Shorter copies of older turns' long messages, for one model call.

A chat that passes work between threads (a bot's main chat, say) fills its
history with prose: reports and replies relayed from other chats, and the
briefs it sends them. Every model call re-sends all of it, though once a
few turns have passed the model rarely needs more than the gist, and the
full text stays in the other chat and in this chat's history.

Each model call sends the newest ``DJANGO_ERGO["OLD_TURNS_IN_FULL"]`` turns
(default 3) as stored. In older turns:

- a message from another chat (its row has a provenance origin, so a
  report, reply or forward; never the user's own words) longer than
  ``DJANGO_ERGO["OLD_MESSAGE_CHARS"]`` (default 1,500) keeps its start and
  ends with a note naming the chat that has the rest;
- a tool call's long string arguments (a brief to ``ergo_thread_send``,
  say) are cut the same way.

In every finished turn, a tool call argument that the turn's final
assistant text repeats word for word (the reply a chat turn gives through
``send_reply`` and then stores again as plain text) is replaced by a short
note, so the reply is sent once.

Only what is sent changes: stored history keeps every message in full, and
tool call ids are untouched. ``None`` in either setting turns this off.
"""

from __future__ import annotations

import itertools
import json
import logging

from django_ergo.settings import api_settings

log = logging.getLogger(__name__)

# Tool arguments at most this long are never replaced as repeats.
REPEAT_MIN_CHARS = 200
REPEAT_NOTE = "[the same text as this turn's final reply, below]"


def _is_turn_start(message: dict) -> bool:
    from django_ergo.conversation.compaction import _is_turn_start

    return _is_turn_start(message)


def _cut(text: str, max_chars: int, where: str) -> str:
    return (
        f"{text[:max_chars].rstrip()}\n[… {len(text) - max_chars:,} more chars "
        f"trimmed from context; {where}.]"
    )


def _texts(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    return []


def _with_texts(message: dict, change) -> dict:
    """A copy of ``message`` with ``change(text)`` applied to each text part."""
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": change(content)}
    return {
        **message,
        "content": [
            {**block, "text": change(str(block.get("text", "")))}
            if isinstance(block, dict) and block.get("type") == "text"
            else block
            for block in content
        ],
    }


def _map_strings(value, change):
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        return {k: _map_strings(v, change) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_strings(v, change) for v in value]
    return value


def _with_tool_args(message: dict, change) -> dict:
    """A copy of an assistant ``message`` with ``change`` applied to every
    string in its tool calls' arguments (Claude tool_use or OpenAI tool_calls)."""
    out = dict(message)
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                calls.append(call)
                continue
            arguments = _map_strings(arguments, change)
            calls.append(
                {**call, "function": {**function, "arguments": json.dumps(arguments)}}
            )
        out["tool_calls"] = calls
    content = message.get("content")
    if isinstance(content, list):
        out["content"] = [
            {**block, "input": _map_strings(block.get("input") or {}, change)}
            if isinstance(block, dict) and block.get("type") == "tool_use"
            else block
            for block in content
        ]
    return out


def _has_tool_calls(message: dict) -> bool:
    if message.get("tool_calls"):
        return True
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def _origin(row) -> str:
    origin = (getattr(row, "provenance", None) or {}).get("origin") or {}
    if not origin:
        return ""
    label = origin.get("label") or "another chat"
    thread = origin.get("session_id")
    return f"{label} (thread {thread})" if thread else label


def trim_old_turns(  # noqa: C901, PLR0912
    messages: list[dict],
    rows: dict[int, object] | None = None,
    *,
    keep_turns: int | None = None,
    max_chars: int | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """Shorten older turns' long messages and drop repeated replies, for one call.

    ``rows`` maps ``id(message)`` to its stored row, for each message's
    provenance. Works on Claude and OpenAI messages alike. The input list
    and its messages are not changed. ``stats``, when given, gets
    ``trimmed_messages``: how many messages were changed.
    """
    if stats is not None:
        stats["trimmed_messages"] = 0
    if keep_turns is None:
        keep_turns = api_settings.OLD_TURNS_IN_FULL
    if max_chars is None:
        max_chars = api_settings.OLD_MESSAGE_CHARS
    rows = rows or {}
    starts = [i for i, m in enumerate(messages) if _is_turn_start(m)]
    out = list(messages)

    # A finished turn's final assistant text, and the tool calls before it.
    bounds = [*starts, len(messages)]
    for start, end in itertools.pairwise(bounds):
        last = out[end - 1] if end > start else None
        if not last or last.get("role") != "assistant" or _has_tool_calls(last):
            continue
        final = "\n".join(_texts(last))
        if not final.strip():
            continue

        def drop_repeat(text: str, final=final) -> str:
            stripped = text.strip()
            if len(stripped) > REPEAT_MIN_CHARS and stripped in final:
                return REPEAT_NOTE
            return text

        for i in range(start, end - 1):
            if out[i].get("role") == "assistant" and _has_tool_calls(out[i]):
                out[i] = _with_tool_args(out[i], drop_repeat)

    if keep_turns is not None and keep_turns >= 0 and max_chars is not None:
        # Messages before the oldest turn kept in full are old.
        if keep_turns == 0:
            cutoff = len(messages)
        elif keep_turns < len(starts):
            cutoff = starts[-keep_turns]
        else:
            cutoff = 0
        for i in range(cutoff):
            message = out[i]
            if message.get("role") == "assistant" and _has_tool_calls(message):
                out[i] = _with_tool_args(
                    message,
                    lambda text: (
                        _cut(text, max_chars, "the full text is in this chat's history")
                        if len(text) > max_chars
                        else text
                    ),
                )
                continue
            origin = _origin(rows.get(id(messages[i])))
            if message.get("role") != "user" or not origin:
                continue
            if any(len(t) > max_chars for t in _texts(message)):
                out[i] = _with_texts(
                    message,
                    lambda text, origin=origin: (
                        _cut(text, max_chars, f"the full message is in {origin}")
                        if len(text) > max_chars
                        else text
                    ),
                )

    changed = sum(1 for a, b in zip(messages, out, strict=True) if a != b)
    if stats is not None:
        stats["trimmed_messages"] = changed
    if not changed:
        return messages
    log.debug("trimmed %d older messages", changed)
    return out
