from django.conf import settings
from prometheus_client import Counter, Gauge, Histogram

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

celery_task_retries_total = Counter(
    f"{METRIC_PREFIX}_celery_task_retries_total",
    "Total Celery task retries requested.",
    labelnames=("task_name",),
)

celery_task_published_total = Counter(
    f"{METRIC_PREFIX}_celery_task_published_total",
    "Celery tasks sent to the broker by this process (in beat: schedules fired).",
    labelnames=("task_name",),
)

# ---------------------------------------------------------------------------
# Bots: turns (structured calls), model tokens and cost, tools
#
# Labels are bounded by configuration: bot names, engine types, model IDs the
# bots are allowed to use, call kinds and tool names defined in code or bot
# folders. Never chat, thread, user or worker IDs, and never message text.
# ---------------------------------------------------------------------------

_LONG_BUCKETS = (0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, float("inf"))

llm_calls_total = Counter(
    f"{METRIC_PREFIX}_llm_calls_total",
    "Structured call runs (bot turns and standalone calls) by how they ended.",
    labelnames=("bot", "kind", "engine", "model", "status"),
)

llm_call_errors_total = Counter(
    f"{METRIC_PREFIX}_llm_call_errors_total",
    "Structured call runs that failed, by error category (api, crash, budget, refusal, ...).",
    labelnames=("bot", "engine", "model", "category"),
)

llm_call_duration_seconds = Histogram(
    f"{METRIC_PREFIX}_llm_call_duration_seconds",
    "Wall time of one structured call run, model requests and tool calls included.",
    labelnames=("bot", "kind", "status"),
    buckets=_LONG_BUCKETS,
)

llm_calls_in_progress = Gauge(
    f"{METRIC_PREFIX}_llm_calls_in_progress",
    "Structured call runs executing right now.",
    labelnames=("bot", "kind"),
    multiprocess_mode="livesum",
)

llm_requests_total = Counter(
    f"{METRIC_PREFIX}_llm_requests_total",
    "Model responses received inside structured calls.",
    labelnames=("bot", "engine", "model"),
)

llm_tokens_total = Counter(
    f"{METRIC_PREFIX}_llm_tokens_total",
    "Model tokens by type: input (uncached), output, cache_write, cache_read, reasoning (part of output).",
    labelnames=("bot", "engine", "model", "type"),
)

llm_cost_usd_total = Counter(
    f"{METRIC_PREFIX}_llm_cost_usd_total",
    "Estimated model cost in USD from list prices (unpriced models add nothing).",
    labelnames=("bot", "engine", "model"),
)

tool_calls_total = Counter(
    f"{METRIC_PREFIX}_tool_calls_total",
    "Tool calls dispatched to toolkits, by outcome (ok or error).",
    labelnames=("tool", "outcome"),
)

tool_call_duration_seconds = Histogram(
    f"{METRIC_PREFIX}_tool_call_duration_seconds",
    "Tool call runtime in seconds.",
    labelnames=("tool",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, float("inf")),
)

# ---------------------------------------------------------------------------
# Workers (Orca / local-shell coding agents, @bot_task workers) and async work
# ---------------------------------------------------------------------------

workers_started_total = Counter(
    f"{METRIC_PREFIX}_workers_started_total",
    "Workers created. function is agent:<manager>, task:<name> or <plugin>:<name>.",
    labelnames=("bot", "function", "agent", "model"),
)

workers_finished_total = Counter(
    f"{METRIC_PREFIX}_workers_finished_total",
    "Workers that reached completed, failed or cancelled.",
    labelnames=("bot", "function", "agent", "model", "status"),
)

worker_duration_seconds = Histogram(
    f"{METRIC_PREFIX}_worker_duration_seconds",
    "Worker lifetime from first start (or creation) to finish.",
    labelnames=("bot", "function", "status"),
    buckets=(10.0, 30.0, 60.0, 300.0, 600.0, 1800.0, 3600.0, 7200.0, 14400.0, 43200.0, 86400.0, float("inf")),
)

worker_polls_total = Counter(
    f"{METRIC_PREFIX}_worker_polls_total",
    "Worker steps claimed (each poll of an agent's watcher, each task step).",
    labelnames=("bot", "function"),
)

thread_messages_total = Counter(
    f"{METRIC_PREFIX}_thread_messages_total",
    "Async thread messages queued for a bot session, by source (person, bot, reply, wake, recurring_wake, worker, event).",
    labelnames=("source",),
)

wakes_scheduled_total = Counter(
    f"{METRIC_PREFIX}_wakes_scheduled_total",
    "One-off self-wakes bots scheduled.",
    labelnames=("bot",),
)

wakes_fired_total = Counter(
    f"{METRIC_PREFIX}_wakes_fired_total",
    "One-off self-wakes that fired (status fired or failed).",
    labelnames=("bot", "status"),
)

recurring_wake_occurrences_total = Counter(
    f"{METRIC_PREFIX}_recurring_wake_occurrences_total",
    "Recurring wake occurrences created (each is one wake turn queued).",
    labelnames=("bot",),
)

schedule_runs_total = Counter(
    f"{METRIC_PREFIX}_schedule_runs_total",
    "bot.yaml schedule runs claimed, by bot and schedule name.",
    labelnames=("bot", "schedule"),
)

schedule_runs_finished_total = Counter(
    f"{METRIC_PREFIX}_schedule_runs_finished_total",
    "bot.yaml schedule runs that finished, by final status.",
    labelnames=("bot", "schedule", "status"),
)
