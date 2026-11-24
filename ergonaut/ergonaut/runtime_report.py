"""Each Ergonaut process reports the code it runs, for deploy watches.

Every long-running role (web, the Celery workers, beat, ``ergonaut bots``)
starts a :class:`Reporter` that writes ``django_ergo``'s ``RuntimeReport`` rows:
this process's build revision, the roles it serves and whether it has finished
starting, refreshed every ``ERGONAUT_RUNTIME_REPORT_SECONDS`` (30) from a daemon
thread, and withdrawn on a clean exit. ``django_ergo.bots.live_watch`` decides
from those rows alone whether a revision is live.

The revision is the process's own: ``/app/BUILD_REVISION`` (written into the
image at build time, see the Dockerfile; ``ERGONAUT_BUILD_REVISION_FILE`` moves
it), else ``upgrades.current_version()`` (``ERGONAUT_VERSION``, pip's ``git+``
record, a checkout's HEAD). Nothing about what was merged or published is used.

Role names are the production workload names (``infra/prod/README.md``):
``web``, ``celery``, ``bot-tasks``, ``beat``, ``bots``. A Celery worker's roles
come from the queues it consumes (see ``worker_roles``), so ``ergonaut up``'s
single worker reports both ``celery`` and ``bot-tasks``;
``ERGONAUT_ROLE`` (comma-separated) overrides any process's roles.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_REVISION_FILE = "/app/BUILD_REVISION"

_reporter: Reporter | None = None
_lock = threading.Lock()


def build_revision() -> str:
    """The django-ergo commit this process was built from, or ''."""
    path = Path(os.environ.get("ERGONAUT_BUILD_REVISION_FILE") or DEFAULT_REVISION_FILE)
    try:
        if revision := path.read_text().strip():
            return revision.lower()
    except OSError:
        pass
    from ergonaut.upgrades import current_version

    return (current_version() or "").strip().lower()


def instance_id() -> str:
    """This process: the pod (hostname) and pid."""
    return f"{socket.gethostname()}:{os.getpid()}"


def roles_for(default: list[str]) -> list[str]:
    if override := os.environ.get("ERGONAUT_ROLE", "").strip():
        return [r.strip() for r in override.split(",") if r.strip()]
    return default


def worker_roles(queues) -> list[str]:
    """A Celery worker's roles from the queue names it consumes: the general
    queues (``celery``, ``turns``, ``ERGONAUT_TURN_QUEUE``) are ``celery``, any
    other queue is its own role with dashes (``bot_tasks`` is ``bot-tasks``,
    ``runpod_maintenance`` is ``runpod-maintenance``)."""
    general = {"celery", "turns", os.environ.get("ERGONAUT_TURN_QUEUE", "").strip() or "celery"}
    roles: list[str] = []
    for name in sorted({str(q) for q in queues or ()} or {"celery"}):
        role = "celery" if name in general else name.replace("_", "-")
        if role not in roles:
            roles.append(role)
    return sorted(roles, key=lambda r: (r != "celery", r))


def interval() -> float:
    try:
        return max(1.0, float(os.environ.get("ERGONAUT_RUNTIME_REPORT_SECONDS") or 30))
    except ValueError:
        return 30.0


class Reporter:
    """Reports one process's roles until it exits."""

    def __init__(self, roles: list[str], *, ready: bool, revision: str | None = None):
        self.roles = list(roles)
        self.ready = ready
        self.revision = build_revision() if revision is None else revision
        self.instance = instance_id()
        self.pid = os.getpid()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        """One report, from whichever thread; never raises."""
        from django.db import close_old_connections, connection
        from django_ergo.bots import live_watch

        try:
            live_watch.report(self.instance, self.roles, self.revision, ready=self.ready)
        except Exception:  # noqa: BLE001 — reporting never takes the process down
            logger.warning("Runtime report for %s failed", ",".join(self.roles), exc_info=True)
        finally:
            if threading.current_thread() is self._thread:
                connection.close()
            else:
                close_old_connections()

    def mark_ready(self) -> None:
        self.ready = True
        self.beat()

    def start(self) -> Reporter:
        self.beat()
        self._thread = threading.Thread(target=self._run, name="ergonaut-runtime-report", daemon=True)
        self._thread.start()
        atexit.register(self.stop)
        logger.info("Reporting %s at %s as %s", ",".join(self.roles), self.revision[:12] or "?", self.instance)
        return self

    def _run(self) -> None:
        while not self._stop.wait(interval()):
            self.beat()

    def stop(self) -> None:
        """Withdraw this process's reports (a clean shutdown). A forked child
        (a Celery pool process) inherits the atexit hook, never the reports."""
        if self._stop.is_set() or os.getpid() != self.pid:
            return
        self._stop.set()
        from django.db import connection
        from django_ergo.bots import live_watch

        try:
            live_watch.withdraw(self.instance)
        except Exception:  # noqa: BLE001 — shutting down anyway; the report goes stale
            logger.debug("Withdrawing runtime report failed", exc_info=True)
        finally:
            connection.close()


def start(roles: list[str], *, ready: bool) -> Reporter | None:
    """Start this process's reporter (once). Off when ``LIVE_ROLES`` is empty
    or ``ERGONAUT_RUNTIME_REPORT_SECONDS`` is 0."""
    global _reporter  # noqa: PLW0603
    from django_ergo.bots import live_watch

    if not live_watch.enabled() or os.environ.get("ERGONAUT_RUNTIME_REPORT_SECONDS", "").strip() == "0":
        return None
    with _lock:
        if _reporter is None:
            _reporter = Reporter(roles_for(roles), ready=ready).start()
        return _reporter


def mark_ready() -> None:
    if _reporter is not None:
        _reporter.mark_ready()


def stop() -> None:
    global _reporter  # noqa: PLW0603
    with _lock:
        if _reporter is not None:
            _reporter.stop()
            _reporter = None


# -- Celery: workers report once they consume, beat once it starts ------------


def _consumed_queues(consumer) -> list[str]:
    try:
        return [q.name for q in consumer.task_consumer.queues]
    except AttributeError:
        pass
    try:
        return list(consumer.app.amqp.queues.consume_from or ())
    except AttributeError:
        return []


def on_worker_ready(sender=None, **kwargs) -> None:
    start(worker_roles(_consumed_queues(sender)), ready=True)


def on_beat_init(sender=None, **kwargs) -> None:
    start(["beat"], ready=True)


def on_shutdown(**kwargs) -> None:
    stop()


def connect_celery() -> None:
    from celery.signals import beat_init, worker_ready, worker_shutdown

    worker_ready.connect(on_worker_ready, weak=False)
    worker_shutdown.connect(on_shutdown, weak=False)
    beat_init.connect(on_beat_init, weak=False)
