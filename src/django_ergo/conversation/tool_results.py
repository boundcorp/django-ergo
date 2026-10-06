"""How many large tool results each model call carries in full.

A long turn calls tools step after step, and every model call re-sends the
results of every earlier step. A tool that returns a big dump (a design tree,
a file listing, a page of rows) makes each later step pay for all the earlier
dumps again.

Each model call carries the newest ``DJANGO_ERGO["TOOL_RESULTS_IN_CONTEXT"]``
large tool results in full (default 3), and keeps more of the newest while
all the results kept add up to at most ``DJANGO_ERGO["TOOL_RESULTS_CHARS_IN_CONTEXT"]``
characters (default 40,000). A turn that reads ten small files keeps them
all; a turn that pulls several big dumps keeps only the newest few. Older
large results become a short stub naming the tool and its size::

    [penpot_tree result, 180 lines, 9,412 chars; trimmed from context to save space. Note what you need; call the tool again only if you still need detail.]

Only what is sent changes. Stored history keeps every result in full, so
history tools and later readers still see them, and the model can run the
tool again. A result counts as large when its text is longer than
``STUB_MIN_CHARS``; shorter results and errors always stay as they are, and
don't count toward the limit. Tool call ids are untouched, so every
tool_use still has its tool_result (Claude) and every tool call its tool
message (OpenAI). Images inside a stubbed result stay, so the image window
in ``conversation.images`` decides about them as before.

An engine's ``tool_results_in_context`` (set per bot with
``tool_results_in_context`` in bot.yaml) overrides the setting. ``None``
in the setting turns trimming off.
"""

from __future__ import annotations

import logging

from django_ergo.settings import api_settings

log = logging.getLogger(__name__)

# Results with at most this much text are never stubbed.
STUB_MIN_CHARS = 500


def _text_parts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    return []


def _tool_names(messages: list[dict]) -> dict[str, str]:
    """tool call id -> tool name, from both engines' assistant messages."""
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:  # OpenAI
            function = call.get("function") or {}
            if call.get("id"):
                names[call["id"]] = function.get("name", "")
        content = message.get("content")
        if isinstance(content, list):  # Claude
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    names[block.get("id", "")] = block.get("name", "")
    return names


def _results(messages: list[dict]):
    """(message index, block index or None, result) for each tool result, oldest first.

    ``result`` is the dict holding ``content``: a Claude tool_result block
    or an OpenAI tool message.
    """
    for i, message in enumerate(messages):
        if message.get("role") == "tool":
            yield i, None, message
            continue
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    yield i, j, block


def _call_id(result: dict) -> str:
    return result.get("tool_use_id") or result.get("tool_call_id") or ""


def stub_text(name: str, text: str) -> str:
    lines = text.count("\n") + 1 if text else 0
    return (
        f"[{name or 'tool'} result, {lines:,} lines, {len(text):,} chars; "
        "trimmed from context to save space. Note what you need; call the tool "
        "again only if you still need detail.]"
    )


def _stubbed_content(content, stub: str):
    """The content with its text replaced by ``stub``; other parts (images) kept."""
    if not isinstance(content, list):
        return stub
    rest = [
        block
        for block in content
        if not (isinstance(block, dict) and block.get("type") == "text")
    ]
    return [{"type": "text", "text": stub}, *rest]


def _count_kept(sizes: list[int], keep: int, max_chars: int) -> int:
    """How many of the newest results stay: at least ``keep``, more while they fit."""
    kept, total = 0, 0
    for size in reversed(sizes):
        if kept >= keep and total + size > max_chars:
            break
        kept += 1
        total += size
    return kept


def trim_tool_results(
    messages: list[dict],
    *,
    keep: int | None = None,
    max_chars: int | None = None,
    min_chars: int = STUB_MIN_CHARS,
) -> list[dict]:
    """Stub older large tool results, for one model call.

    The newest ``keep`` large results always stay. Older ones stay too while
    the kept results add up to at most ``max_chars`` characters; from the
    first that doesn't fit, it and everything older is stubbed.

    ``keep`` defaults to ``DJANGO_ERGO["TOOL_RESULTS_IN_CONTEXT"]``; when that
    is ``None`` (or ``keep`` is negative) nothing changes. ``max_chars``
    defaults to ``DJANGO_ERGO["TOOL_RESULTS_CHARS_IN_CONTEXT"]``; 0 or
    ``None`` keeps only ``keep``. Works on Claude and OpenAI messages alike.
    The input list and its messages are not changed.
    """
    if keep is None:
        keep = api_settings.TOOL_RESULTS_IN_CONTEXT
    if keep is None or keep < 0:
        return messages
    if max_chars is None:
        max_chars = api_settings.TOOL_RESULTS_CHARS_IN_CONTEXT or 0
    large = []
    for i, j, result in _results(messages):
        size = sum(len(t) for t in _text_parts(result.get("content")))
        if not result.get("is_error") and size > min_chars:
            large.append((i, j, result, size))
    kept = _count_kept([size for *_, size in large], keep, max_chars)
    old = [(i, j, result) for i, j, result, _ in large[: len(large) - kept]]
    if not old:
        return messages
    names = _tool_names(messages)
    out = list(messages)
    for i, j, result in old:
        text = "\n".join(_text_parts(result.get("content")))
        stub = stub_text(names.get(_call_id(result), ""), text)
        trimmed = {**result, "content": _stubbed_content(result.get("content"), stub)}
        if j is None:
            out[i] = trimmed
            continue
        if out[i] is messages[i]:
            out[i] = {**messages[i], "content": list(messages[i]["content"])}
        out[i]["content"][j] = trimmed
    log.debug("stubbed %d of %d large tool results", len(old), len(large))
    return out
