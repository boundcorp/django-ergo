from django.conf import settings
from prometheus_client import Counter, Histogram

METRIC_PREFIX = settings.TELEMETRY_NAMESPACE

http_requests_total = Counter(
    f"{METRIC_PREFIX}_http_requests_total",
    "Total HTTP requests handled by Django.",
    labelnames=("host", "method", "route", "status_code"),
)

http_request_duration_seconds = Histogram(
    f"{METRIC_PREFIX}_http_request_duration_seconds",
    "HTTP latency until the response is returned, excluding streaming iteration.",
    labelnames=("host", "method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")),
)

http_request_db_queries = Histogram(
    f"{METRIC_PREFIX}_http_request_db_queries",
    "Database executions before the response is returned (not streaming iteration).",
    labelnames=("host", "method", "route"),
    buckets=(0, 1, 2, 5, 10, 20, 50, 100, 250, 500, float("inf")),
)

http_request_db_duration_seconds = Histogram(
    f"{METRIC_PREFIX}_http_request_db_duration_seconds",
    "Database execution time before the response is returned.",
    labelnames=("host", "method", "route"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, float("inf")),
)

http_request_phase_duration_seconds = Histogram(
    f"{METRIC_PREFIX}_http_request_phase_duration_seconds",
    "Named request phase time before the response is returned.",
    labelnames=("host", "method", "route", "phase"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, float("inf")),
)

http_response_size_bytes = Histogram(
    f"{METRIC_PREFIX}_http_response_size_bytes",
    "Buffered response body size; streaming responses are not inspected.",
    labelnames=("host", "method", "route"),
    buckets=(0, 512, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, float("inf")),
)

celery_task_started_total = Counter(
    f"{METRIC_PREFIX}_celery_task_started_total",
    "Total Celery tasks started.",
    labelnames=("task_name",),
)

celery_task_succeeded_total = Counter(
    f"{METRIC_PREFIX}_celery_task_succeeded_total",
    "Total Celery tasks completed successfully.",
    labelnames=("task_name",),
)

celery_task_failures_total = Counter(
    f"{METRIC_PREFIX}_celery_task_failures_total",
    "Total Celery tasks that raised an exception.",
    labelnames=("task_name",),
)

celery_task_latency_seconds = Histogram(
    f"{METRIC_PREFIX}_celery_task_latency_seconds",
    "Celery task runtime in seconds.",
    labelnames=("task_name",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, float("inf")),
)
