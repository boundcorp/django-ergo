"""Run work in a daemon thread that closes its database connections after.

For work that must not block the caller and has no broker to go to (eager
Celery in development, or no worker configured at all).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def start_daemon(fn: Callable, *args, name: str | None = None) -> threading.Thread:
    def run():
        from django.db import close_old_connections

        try:
            fn(*args)
        finally:
            close_old_connections()

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread
