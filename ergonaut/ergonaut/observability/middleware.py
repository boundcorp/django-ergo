import re
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.conf import settings
from django.db import connections

from .metrics import (
    http_request_db_duration_seconds,
    http_request_db_queries,
    http_request_duration_seconds,
    http_request_phase_duration_seconds,
    http_requests_total,
    http_response_size_bytes,
)

REQUEST_PHASES = ("session_lookup", "history", "serialization", "sidebar")
_METHODS = frozenset(("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"))
_ROUTE_PARAMETER = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


@dataclass(slots=True)
class _RequestTiming:
    queries: int = 0
    database_seconds: float = 0.0
    phases: dict[str, float] = field(default_factory=dict)
    active: bool = True

    def execute(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.queries += 1
            self.database_seconds += time.perf_counter() - start


_request_timing: ContextVar[_RequestTiming | None] = ContextVar("ergonaut_request_timing", default=None)


@contextmanager
def request_phase(name: str):
    """Accumulate an allowlisted phase in the current request, or do nothing.

    Durations are inclusive when scopes nest; repeated scopes are summed. Unknown
    names are ignored rather than allowing caller-provided data into telemetry.
    """
    timing = _request_timing.get()
    if timing is None or not timing.active or name not in REQUEST_PHASES:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        if timing.active:
            timing.phases[name] = timing.phases.get(name, 0.0) + time.perf_counter() - start


class PrometheusMetricsMiddleware:
    # Keep the DB wrappers on the same thread as synchronous views and Django's
    # thread-sensitive async ORM bridge. Django adapts this middleware for ASGI;
    # wrapping only the event-loop thread would silently miss async ORM queries.
    sync_capable = True
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.TELEMETRY_METRICS_ENABLED:
            return self.get_response(request)

        timing = _RequestTiming()
        token = _request_timing.set(timing)
        start = time.perf_counter()
        try:
            with ExitStack() as stack:
                # This initializes wrappers, not database connections, and covers
                # aliases first connected inside the view as well as open ones.
                for connection in connections.all():
                    stack.enter_context(connection.execute_wrapper(timing.execute))
                response = self.get_response(request)
            elapsed = time.perf_counter() - start
        finally:
            timing.active = False
            _request_timing.reset(token)

        # Resolver templates, never request.path/kwargs, keep identifiers out of
        # labels. Ninja's Django converters become stable {parameter} segments.
        route = getattr(getattr(request, "resolver_match", None), "route", None)
        route = _ROUTE_PARAMETER.sub(r"{\1}", route) if route else "unmatched"
        method = request.method.upper()
        labels = {"host": "all", "method": method if method in _METHODS else "OTHER", "route": route}
        status_code = response.status_code
        status = str(status_code) if 100 <= status_code <= 599 else "OTHER"

        # Retain the existing metric/label schema without trusting Host headers.
        http_requests_total.labels(**labels, status_code=status).inc()
        http_request_duration_seconds.labels(**labels).observe(elapsed)
        http_request_db_queries.labels(**labels).observe(timing.queries)
        http_request_db_duration_seconds.labels(**labels).observe(timing.database_seconds)
        if not response.streaming:
            http_response_size_bytes.labels(**labels).observe(len(response.content))

        server_timing = [
            f"total;dur={elapsed * 1000:.3f}",
            f"db;dur={timing.database_seconds * 1000:.3f}",
            f'db_queries;desc="{timing.queries}"',
        ]
        for name in REQUEST_PHASES:
            if name in timing.phases:
                seconds = timing.phases[name]
                http_request_phase_duration_seconds.labels(**labels, phase=name).observe(seconds)
                server_timing.append(f"{name};dur={seconds * 1000:.3f}")
        existing = response.get("Server-Timing")
        response["Server-Timing"] = ", ".join(([existing] if existing else []) + server_timing)
        return response
