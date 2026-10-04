"""Wait until no bot turn or worker is running, so a restart doesn't cut one off.

    ergonaut manage wait_idle && systemctl restart ergonaut   # or your run script

Exits 0 once no session holds its turn lock, no worker-tracked call is in
progress and no worker (``ergo_worker_start``, Orca) is queued or running, or 1
after ``--timeout`` seconds (default 30 minutes). ``--ignore-workers`` waits
for turns only. ``--quiet-for`` makes it wait until it has seen nothing running
for that many seconds in a row, so a chat that answers in a few quick turns
isn't caught between two of them.

``ergonaut upgrade`` uses the same gate (``wait_until_idle``).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from django.core.management.base import BaseCommand
from django.utils import timezone


def running_sessions() -> list[str]:
    """Sessions with a turn running now: a held turn lock, or (without Redis) a
    recently updated in-progress call."""
    from django_ergo.conversation.models import StructuredCall

    from ergonaut.apps.bots import tasks

    recent = timezone.now() - timezone.timedelta(minutes=tasks.DEAD_TURN_MINUTES)
    ids = {
        str(sid)
        for sid in StructuredCall.objects.filter(
            status="in_progress", updated_at__gte=recent, session__isnull=False
        ).values_list("session_id", flat=True)
    }
    client = tasks.redis_client()
    if client is not None:
        ids = {sid for sid in ids if tasks.turn_running(sid)}
        for key in client.scan_iter("ergonaut:turn:*"):
            name = key.decode() if isinstance(key, bytes) else key
            sid = name.split(":")[2]
            if name == f"ergonaut:turn:{sid}":
                ids.add(sid)
    return sorted(ids)


def active_workers() -> list[str]:
    """Workers queued or running, as ``bot: title``."""
    from django_ergo.conversation.models import Worker, WorkerStatus

    rows = Worker.objects.filter(status__in=[WorkerStatus.QUEUED, WorkerStatus.RUNNING]).values_list(
        "bot_name", "title"
    )
    return sorted(f"{bot}: {title}" for bot, title in rows)


def busy(*, workers: bool = True) -> list[str]:
    """What would be cut off by a restart now; empty when idle."""
    items = [f"turn {sid}" for sid in running_sessions()]
    if workers:
        items += [f"worker {w}" for w in active_workers()]
    return items


def wait_until_idle(
    *,
    timeout: float = 30 * 60,
    quiet_for: float = 20,
    poll: float = 5,
    workers: bool = True,
    log: Callable[[str], None] = print,
) -> bool:
    """Block until nothing has been running for ``quiet_for`` seconds; False after ``timeout``."""
    deadline = time.monotonic() + timeout
    quiet_since = None
    last = None
    while True:
        items = busy(workers=workers)
        now = time.monotonic()
        if items:
            quiet_since = None
            if items != last:
                log(f"waiting for {len(items)}: {', '.join(items)}")
            last = items
        else:
            quiet_since = quiet_since or now
            if now - quiet_since >= quiet_for:
                return True
        if now >= deadline:
            log(f"still busy after {timeout:.0f}s: {', '.join(items)}")
            return False
        time.sleep(poll)


class Command(BaseCommand):
    help = "Wait until no bot turn or worker is running (run before restarting Ergonaut)."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=30 * 60)
        parser.add_argument("--quiet-for", type=float, default=20)
        parser.add_argument("--poll", type=float, default=5)
        parser.add_argument("--ignore-workers", action="store_true", help="wait for turns only")

    def handle(self, *args, timeout, quiet_for, poll, ignore_workers, **options):
        idle = wait_until_idle(
            timeout=timeout,
            quiet_for=quiet_for,
            poll=poll,
            workers=not ignore_workers,
            log=self.stdout.write,
        )
        if not idle:
            raise SystemExit(1)
        self.stdout.write("idle")
