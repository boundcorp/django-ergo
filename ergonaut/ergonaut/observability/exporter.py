"""A per-pod metrics port for every role, separate from the public web port.

Each long-running process (uvicorn, the Celery worker and beat parents, ``ergonaut
bots``) calls :func:`start_exporter`. When ``TELEMETRY_EXPORTER_PORT`` is set it
serves ``/metrics`` on that port from a daemon thread; Prometheus scrapes the
pods directly (a PodMonitor), and neither the Service nor the Ingress routes to
it. The first process in a container to bind the port serves it; others (a
``manage.py`` shell exec'd into the pod) skip quietly.

With ``PROMETHEUS_MULTIPROC_DIR`` set, every process writes its metrics there and
the exporter sums them, so Celery's prefork children are included. A child that
exits (``--max-tasks-per-child``) has its counters and histograms folded into
``*_archive.db`` so the directory doesn't grow without bound.
"""

import contextlib
import fcntl
import logging
import os
import threading

from django.conf import settings
from prometheus_client import REGISTRY, CollectorRegistry, start_http_server
from prometheus_client.mmap_dict import MmapedDict
from prometheus_client.multiprocess import MultiProcessCollector, mark_process_dead

logger = logging.getLogger(__name__)

_LOCK_FILE = ".compaction.lock"
_ARCHIVED_TYPES = ("counter", "histogram")
_started = threading.Lock()
_server = None
_state_collector = None


def multiprocess_dir() -> str:
    return os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")


@contextlib.contextmanager
def _dir_lock(path: str, mode: int):
    with open(os.path.join(path, _LOCK_FILE), "a") as handle:
        fcntl.flock(handle, mode)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class _LockedMultiProcessCollector(MultiProcessCollector):
    """Reads under a shared lock, so a scrape never sees a child half-archived."""

    def collect(self):
        with _dir_lock(self._path, fcntl.LOCK_SH):
            return list(super().collect())


def _state_collector_instance():
    global _state_collector  # noqa: PLW0603
    if _state_collector is None:
        from .collector import ErgoStateCollector

        _state_collector = ErgoStateCollector()
    return _state_collector


def build_registry(*, state: bool = False) -> CollectorRegistry:
    """The registry to expose: every process's metrics, plus DB state if asked."""
    if path := multiprocess_dir():
        registry = CollectorRegistry()
        _LockedMultiProcessCollector(registry, path=path)
    elif not state:
        return REGISTRY
    else:
        registry = CollectorRegistry()
        registry.register(_RegistryProxy(REGISTRY))
    if state:
        registry.register(_state_collector_instance())
    return registry


class _RegistryProxy:
    def __init__(self, registry):
        self.registry = registry

    def collect(self):
        return self.registry.collect()

    def describe(self):
        return []


def _wants_state(role: str) -> bool:
    configured = settings.TELEMETRY_STATE_COLLECTOR
    if configured in ("1", "true", "yes", "on"):
        return True
    if configured in ("0", "false", "no", "off"):
        return False
    return role == "web"


def start_exporter(role: str) -> bool:
    """Serve this container's metrics on TELEMETRY_EXPORTER_PORT. True if this process serves them."""
    global _server  # noqa: PLW0603
    port = settings.TELEMETRY_EXPORTER_PORT
    if not port or not settings.TELEMETRY_METRICS_ENABLED:
        return False
    with _started:
        if _server is not None:
            return True
        try:
            _server = start_http_server(port, registry=build_registry(state=_wants_state(role)))
        except OSError as exc:
            logger.info("Metrics port %s not served by this %s process: %s", port, role, exc)
            return False
    logger.info("Serving %s metrics on port %s", role, port)
    return True


def archive_process(pid: int, path: str | None = None) -> None:
    """Fold a dead process's counters and histograms into the archive, drop its live gauges."""
    path = path or multiprocess_dir()
    if not path:
        return
    mark_process_dead(pid, path)
    with _dir_lock(path, fcntl.LOCK_EX):
        for kind in _ARCHIVED_TYPES:
            source = os.path.join(path, f"{kind}_{pid}.db")
            if not os.path.exists(source):
                continue
            archive = os.path.join(path, f"{kind}_archive.db")
            totals: dict[str, float] = {}
            for file in (archive, source):
                if os.path.exists(file):
                    for key, value, _timestamp, _pos in MmapedDict.read_all_values_from_file(file):
                        totals[key] = totals.get(key, 0.0) + value
            staging = f"{archive}.tmp"  # not *.db, so collectors never read it
            with contextlib.suppress(FileNotFoundError):
                os.remove(staging)
            merged = MmapedDict(staging)
            try:
                for key, value in totals.items():
                    merged.write_value(key, value, 0.0)
            finally:
                merged.close()
            os.replace(staging, archive)
            os.remove(source)


def prepare_multiprocess_dir() -> None:
    """Create the directory (Prometheus' client needs it before the first metric write)."""
    if path := multiprocess_dir():
        os.makedirs(path, exist_ok=True)
