"""Deprecated: window chats used to be called stream chats.

Import ``WindowChat`` and ``WINDOW_CONFIG`` from ``conversation.window``
instead. These aliases remain for backward compatibility.
"""

from django_ergo.conversation.window import DEFAULT_BUDGET_TOKENS
from django_ergo.conversation.window import DEFAULT_RECENT
from django_ergo.conversation.window import WINDOW_CONFIG
from django_ergo.conversation.window import WindowChat

StreamChat = WindowChat
STREAM_CONFIG = WINDOW_CONFIG

__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_RECENT",
    "STREAM_CONFIG",
    "StreamChat",
]
