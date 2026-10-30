"""Which chat a session is: its role, stored as ``metadata["bot_role"]``.

Every "is this the main chat / a named chat / a thread" question goes through
here. Migration 0019 renamed the stored role ``root`` to ``main``; ``root``
is still accepted as a *name* for the main chat (``MAIN_NAMES``) where a user
or a bot names a chat.
"""

from __future__ import annotations

MAIN = "main"
CHAT = "chat"  # a named chat from bot.yaml
THREAD = "thread"
# Main and named chats: never resolved, always listed.
TOP = frozenset({MAIN, CHAT})
# Names a caller may use for the main chat.
MAIN_NAMES = frozenset({MAIN, "root"})


def role(session) -> str:
    return (session.metadata or {}).get("bot_role") or ""


def is_main(session) -> bool:
    return role(session) == MAIN


def is_top(session) -> bool:
    """A top-level chat: main or a named chat (not a thread)."""
    return role(session) in TOP


def title(session) -> str:
    """The session's title, or "Main" / "Thread" when it has none."""
    meta = session.metadata or {}
    return meta.get("title") or ("Main" if is_main(session) else "Thread")
