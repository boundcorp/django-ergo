"""Budget large tool-result text, preserving ids, images, errors and short results.

The default budget is 20% of the engine context window. The latest three
large results always survive. An explicitly configured legacy count takes
precedence. Only request copies change; stored history remains complete.
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
        "superseded, call the tool again if you need it]"
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


def trim_tool_results(  # noqa: C901, PLR0913, PLR0912
    messages: list[dict],
    *,
    keep: int | None = None,
    min_chars: int = STUB_MIN_CHARS,
    budget_tokens: int | None = None,
    protect_latest: int = 3,
    stats: dict | None = None,
) -> list[dict]:
    """Return a request copy, recording the stub count in ``stats`` if supplied.

    ``keep`` or the legacy setting applies the count rule when non-None.
    Otherwise use ``budget_tokens`` (40k by default); protected results count
    toward the budget even when they exceed it.
    """
    if stats is not None:
        stats["stubbed_results"] = 0
    if keep is None:
        keep = api_settings.TOOL_RESULTS_IN_CONTEXT
    if keep is not None and keep < 0:
        return messages
    if budget_tokens is None:
        budget_tokens = api_settings.TOOL_RESULTS_TOKENS
    if budget_tokens is None:
        budget_tokens = 40_000
    large = [
        (i, j, result)
        for i, j, result in _results(messages)
        if not result.get("is_error")
        and sum(len(t) for t in _text_parts(result.get("content"))) > min_chars
    ]
    if keep is not None:
        old = large[: max(len(large) - keep, 0)]
    else:
        old = []
        used = 0
        for index, item in enumerate(reversed(large)):
            size = (sum(len(t) for t in _text_parts(item[2].get("content"))) + 3) // 4
            if index >= protect_latest and used > budget_tokens:
                old.append(item)
            used += size
    if stats is not None:
        stats["stubbed_results"] = len(old)
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
