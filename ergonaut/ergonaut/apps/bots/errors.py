"""Turn failures in plain words, so the chat can say what went wrong and what to do."""

from __future__ import annotations

import re

# (kind, title, hint, patterns): the first kind whose pattern matches the error wins.
KINDS = [
    (
        "credits",
        "Out of API credits",
        "Add credits with the model provider, then resume.",
        r"insufficient_quota|exceeded your current quota|credit balance|billing|payment required|\b402\b",
    ),
    (
        "auth",
        "The API key was rejected",
        "Check the provider key in Ergonaut's environment, then resume.",
        r"invalid[_ ]api[_ ]key|incorrect api key|authentication|unauthorized|\b401\b|permission denied|\b403\b",
    ),
    (
        "context",
        "The chat is too long for the model",
        "Resume to let compaction shorten it, or start a new thread.",
        r"context[_ ]length|maximum context|too many tokens|prompt is too long",
    ),
    (
        "rate_limit",
        "Rate limited by the model provider",
        "Wait a minute, then resume.",
        r"rate[_ ]limit|\b429\b|too many requests",
    ),
    (
        "provider",
        "The model provider didn't answer",
        "It's usually brief; resume to try again.",
        r"timeout|timed out|connection|overloaded|\b5\d\d\b|server error|service unavailable",
    ),
    (
        "worker",
        "The turn stopped without finishing",
        "Its worker exited (often a restart); resume to carry on.",
        r"stopped without finishing|crashed",
    ),
]
_COMPILED = [(kind, title, hint, re.compile(pattern, re.IGNORECASE)) for kind, title, hint, pattern in KINDS]
MAX_DETAIL = 300


def describe_error(error: str) -> dict:
    """``{"kind", "title", "hint"}`` for a failed call's error text."""
    for kind, title, hint, pattern in _COMPILED:
        if pattern.search(error or ""):
            return {"kind": kind, "title": title, "hint": hint}
    first = (error or "The turn failed").strip().splitlines()[0]
    if len(first) > MAX_DETAIL:
        first = first[: MAX_DETAIL - 1] + "…"
    return {"kind": "other", "title": first, "hint": "Resume to try again."}
