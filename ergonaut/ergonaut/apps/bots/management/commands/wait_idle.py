"""Wait until no bot turn is running, so a restart doesn't cut one off.

    ergonaut manage wait_idle && systemctl restart ergonaut   # or your run script

Exits 0 once no session holds its turn lock and no worker-tracked call is in
progress, or 1 after ``--timeout`` seconds (default 30 minutes).
``--quiet-for`` makes it wait until it has seen no running turn for that many
seconds in a row, so a chat that answers in a few quick turns isn't caught
between two of them.
"""

from __future__ import annotations

import time

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


class Command(BaseCommand):
    help = "Wait until no bot turn is running (run before restarting Ergonaut)."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=30 * 60)
        parser.add_argument("--quiet-for", type=float, default=20)
        parser.add_argument("--poll", type=float, default=5)

    def handle(self, *args, timeout, quiet_for, poll, **options):
        deadline = time.monotonic() + timeout
        quiet_since = None
        last = None
        while True:
            busy = running_sessions()
            now = time.monotonic()
            if busy:
                quiet_since = None
                if busy != last:
                    self.stdout.write(f"waiting for {len(busy)} running turn(s): {', '.join(busy)}")
                last = busy
            else:
                quiet_since = quiet_since or now
                if now - quiet_since >= quiet_for:
                    self.stdout.write("idle")
                    return
            if now >= deadline:
                self.stderr.write(f"still busy after {timeout:.0f}s: {', '.join(busy)}")
                raise SystemExit(1)
            time.sleep(poll)
