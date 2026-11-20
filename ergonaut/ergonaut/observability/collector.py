"""Gauges and totals read from the database, cached so a scrape never runs heavy queries.

Only the web role registers this (see ``exporter``), so with two web replicas
every series appears twice: dashboards take ``max by (...)``. A failed refresh
keeps serving the last good values and reports ``..._state_up 0``.
"""

import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import Count, Sum
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

logger = logging.getLogger(__name__)

PREFIX = settings.TELEMETRY_NAMESPACE
AGENT_TOKEN_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_write": "cache_write_tokens",
    "cache_read": "cache_read_tokens",
    "reasoning": "reasoning_tokens",
}
OPEN_MESSAGE_STATUSES = ("queued", "delivered", "waiting")


def _rows(queryset):
    return list(queryset)


def _celery_queue_lengths() -> dict[str, int]:
    url = getattr(settings, "CELERY_BROKER_URL", "") or ""
    if not url.startswith(("redis://", "rediss://")):
        return {}
    import redis

    client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    try:
        return {queue: int(client.llen(queue)) for queue in settings.TELEMETRY_CELERY_QUEUES}
    finally:
        client.close()


def read_state() -> dict:
    """One snapshot of the state the gauges show (a handful of GROUP BY queries)."""
    from django_ergo.conversation import models as m

    if not connection.in_atomic_block:
        # The exporter thread keeps its own connection: drop it if it went stale.
        close_old_connections()
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '5s'")
        state = {
            "workers": _rows(
                m.Worker.objects.filter(status__in=("queued", "running"))
                .values("bot_name", "function", "status")
                .annotate(n=Count("id"))
                .order_by()
            ),
            "wakes": _rows(
                m.Wake.objects.filter(status="pending").values("bot_name").annotate(n=Count("id")).order_by()
            ),
            "recurring": _rows(m.RecurringWake.objects.values("bot_name", "status").annotate(n=Count("id")).order_by()),
            "messages": _rows(
                m.ThreadMessage.objects.filter(status__in=OPEN_MESSAGE_STATUSES)
                .values("recipient_session__bot_name", "status")
                .annotate(n=Count("id"))
                .order_by()
            ),
            "agent_usage": _rows(
                m.AgentUsage.objects.values("bot_name", "source", "agent", "model")
                .annotate(requests=Sum("requests"), **{k: Sum(f) for k, f in AGENT_TOKEN_FIELDS.items()})
                .order_by()
            ),
        }
    try:
        state["queues"] = _celery_queue_lengths()
    except Exception:  # noqa: BLE001 — the broker being down shouldn't hide the DB gauges
        logger.warning("Could not read Celery queue lengths", exc_info=True)
        state["queues"] = {}
    return state


class ErgoStateCollector:
    def __init__(self, ttl: float | None = None, reader=read_state):
        self.ttl = settings.TELEMETRY_STATE_TTL_SECONDS if ttl is None else ttl
        self.reader = reader
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._read_at = 0.0
        self._ok = False
        self._seconds = 0.0

    def _refresh(self) -> dict | None:
        with self._lock:
            if self._state is not None and time.monotonic() - self._read_at < self.ttl:
                return self._state
            start = time.monotonic()
            try:
                self._state = self.reader()
                self._ok = True
            except Exception:  # noqa: BLE001 — serve the last good snapshot
                logger.warning("Could not read Ergonaut state for metrics", exc_info=True)
                self._ok = False
            finally:
                self._read_at = time.monotonic()
                self._seconds = self._read_at - start
            return self._state

    def describe(self):
        return []  # collected lazily; don't query when the collector is registered

    def collect(self):  # noqa: C901 — one family per block
        state = self._refresh()
        yield GaugeMetricFamily(f"{PREFIX}_state_up", "1 if the last state snapshot was read.", value=int(self._ok))
        yield GaugeMetricFamily(
            f"{PREFIX}_state_read_seconds", "Time the last state snapshot took.", value=self._seconds
        )
        if state is None:
            return

        workers = GaugeMetricFamily(
            f"{PREFIX}_workers", "Workers queued or running now.", labels=("bot", "function", "status")
        )
        for row in state["workers"]:
            workers.add_metric((row["bot_name"], row["function"], row["status"]), row["n"])
        yield workers

        wakes = GaugeMetricFamily(f"{PREFIX}_wakes_pending", "One-off self-wakes not yet due.", labels=("bot",))
        for row in state["wakes"]:
            wakes.add_metric((row["bot_name"],), row["n"])
        yield wakes

        recurring = GaugeMetricFamily(
            f"{PREFIX}_recurring_wakes", "Recurring wake schedules by status.", labels=("bot", "status")
        )
        for row in state["recurring"]:
            recurring.add_metric((row["bot_name"], row["status"]), row["n"])
        yield recurring

        messages = GaugeMetricFamily(
            f"{PREFIX}_thread_messages_open",
            "Async thread messages not yet answered (queued, delivered, waiting for approval).",
            labels=("bot", "status"),
        )
        for row in state["messages"]:
            messages.add_metric((row["recipient_session__bot_name"] or "unknown", row["status"]), row["n"])
        yield messages

        tokens = CounterMetricFamily(
            f"{PREFIX}_agent_tokens",
            "Tokens coding agents (Orca, local shell) reported in their own session files.",
            labels=("bot", "source", "agent", "model", "type"),
        )
        requests = CounterMetricFamily(
            f"{PREFIX}_agent_requests",
            "Model requests coding agents reported.",
            labels=("bot", "source", "agent", "model"),
        )
        for row in state["agent_usage"]:
            key = (row["bot_name"], row["source"], row["agent"], row["model"])
            requests.add_metric(key, row["requests"] or 0)
            for token_type in AGENT_TOKEN_FIELDS:
                tokens.add_metric((*key, token_type), row[token_type] or 0)
        yield tokens
        yield requests

        queues = GaugeMetricFamily(
            f"{PREFIX}_celery_queue_length", "Messages waiting in each Celery queue.", labels=("queue",)
        )
        for queue, length in state["queues"].items():
            queues.add_metric((queue,), length)
        yield queues
