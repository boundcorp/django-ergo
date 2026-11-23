"""Prometheus metrics for bot turns, tools, workers and async work.

Receivers of django_ergo's signals and of ``post_save`` on its models. They only
touch in-memory metrics: no queries (related rows are read only when already
loaded), no message text, and they never raise into the code that saved.
"""

import logging

from django.db.models.signals import post_save
from django_ergo import signals
from django_ergo.conversation import models as ergo_models

from . import metrics

logger = logging.getLogger(__name__)

TOKEN_TYPES = ("input", "output", "cache_write", "cache_read", "reasoning")
WORKER_TERMINAL = frozenset(("completed", "failed", "cancelled"))
_UNKNOWN = "unknown"


def _safe(receiver):
    def wrapped(*args, **kwargs):
        try:
            receiver(*args, **kwargs)
        except Exception:  # noqa: BLE001 — metrics must never break the caller
            logger.debug("Metrics receiver %s failed", receiver.__name__, exc_info=True)

    wrapped.__name__ = receiver.__name__
    return wrapped


def _label(value) -> str:
    return str(value or "")[:100] or _UNKNOWN


def _cached(instance, field: str):
    """A related row only if it's already loaded (receivers must not query)."""
    descriptor = getattr(type(instance), field)
    return getattr(instance, field) if descriptor.is_cached(instance) else None


@_safe
def call_started(sender, call, bot_name, **kwargs):
    metrics.llm_calls_in_progress.labels(bot=_label(bot_name), kind=_label(call.kind)).inc()


@_safe
def call_finished(sender, call, bot_name, duration, tokens, cost_usd, requests, **kwargs):  # noqa: PLR0913
    bot, kind = _label(bot_name), _label(call.kind)
    engine, model, status = _label(call.engine_type), _label(call.model_name), _label(call.status)
    metrics.llm_calls_in_progress.labels(bot=bot, kind=kind).dec()
    metrics.llm_calls_total.labels(bot=bot, kind=kind, engine=engine, model=model, status=status).inc()
    metrics.llm_call_duration_seconds.labels(bot=bot, kind=kind, status=status).observe(duration)
    if call.status == ergo_models.StructuredCallStatus.FAILED:
        category = _label(call.error_category or "other")
        metrics.llm_call_errors_total.labels(bot=bot, engine=engine, model=model, category=category).inc()
    if requests:
        metrics.llm_requests_total.labels(bot=bot, engine=engine, model=model).inc(requests)
    for token_type in TOKEN_TYPES:
        if count := tokens.get(token_type):
            metrics.llm_tokens_total.labels(bot=bot, engine=engine, model=model, type=token_type).inc(count)
    if cost_usd and cost_usd > 0:
        metrics.llm_cost_usd_total.labels(bot=bot, engine=engine, model=model).inc(cost_usd)


@_safe
def tool_finished(sender, tool, is_error, duration, **kwargs):
    tool = _label(tool)
    metrics.tool_calls_total.labels(tool=tool, outcome="error" if is_error else "ok").inc()
    metrics.tool_call_duration_seconds.labels(tool=tool).observe(duration)


@_safe
def memory_event(sender, bot_name, event, outcome, reason="", duration=0.0, count=0, **kwargs):  # noqa: PLR0913
    bot = _label(bot_name)
    if event == "prefetch":
        metrics.memory_prefetch_total.labels(bot=bot, outcome=_label(outcome), reason=str(reason)[:40]).inc()
        if outcome != "skipped":
            metrics.memory_prefetch_duration_seconds.labels(bot=bot).observe(duration)
            metrics.memory_prefetch_facts.labels(bot=bot).observe(count)
    elif event == "retain":
        metrics.memory_retain_total.labels(bot=bot, outcome=_label(outcome), reason=str(reason)[:40]).inc()


def _worker_labels(worker) -> dict:
    state = worker.state if isinstance(worker.state, dict) else {}
    return {
        "bot": _label(worker.bot_name),
        "function": _label(worker.function),
        "agent": str(state.get("agent") or "")[:40],
        "model": str(state.get("model") or "")[:100],
    }


@_safe
def worker_saved(sender, instance, created, update_fields=None, **kwargs):
    labels = _worker_labels(instance)
    if created:
        metrics.workers_started_total.labels(**labels).inc()
        return
    fields = set(update_fields or ())
    if "polls" in fields:
        metrics.worker_polls_total.labels(bot=labels["bot"], function=labels["function"]).inc()
    if "completed_at" in fields and instance.status in WORKER_TERMINAL:
        metrics.workers_finished_total.labels(**labels, status=instance.status).inc()
        began = instance.started_at or instance.created_at
        if began and instance.completed_at:
            metrics.worker_duration_seconds.labels(
                bot=labels["bot"], function=labels["function"], status=instance.status
            ).observe(max(0.0, (instance.completed_at - began).total_seconds()))


def _message_source(message) -> str:
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    for key in ("wake", "recurring_wake", "worker"):
        if metadata.get(key):
            return key
    if message.in_reply_to_id:
        return "reply"
    if message.sender_session_id:
        return "bot"
    if metadata.get("source") or metadata.get("event"):
        return "event"
    return "person"


@_safe
def thread_message_saved(sender, instance, created, **kwargs):
    if created:
        metrics.thread_messages_total.labels(source=_message_source(instance)).inc()


@_safe
def wake_saved(sender, instance, created, update_fields=None, **kwargs):
    bot = _label(instance.bot_name)
    if created:
        metrics.wakes_scheduled_total.labels(bot=bot).inc()
        return
    # wakes.fire() saves exactly these fields once, after claiming the wake.
    if {"status", "thread_message"} <= set(update_fields or ()):
        metrics.wakes_fired_total.labels(bot=bot, status=_label(instance.status)).inc()


@_safe
def recurring_occurrence_saved(sender, instance, created, **kwargs):
    if created:
        schedule = _cached(instance, "schedule")
        metrics.recurring_wake_occurrences_total.labels(bot=_label(getattr(schedule, "bot_name", ""))).inc()


@_safe
def schedule_run_saved(sender, instance, created, update_fields=None, **kwargs):
    bot, schedule = _label(instance.bot_name), _label(instance.schedule)
    if created:
        metrics.schedule_runs_total.labels(bot=bot, schedule=schedule).inc()
    elif "completed_at" in set(update_fields or ()):
        metrics.schedule_runs_finished_total.labels(bot=bot, schedule=schedule, status=_label(instance.status)).inc()


def connect() -> None:
    """Wire the receivers (idempotent: each has a dispatch_uid)."""
    signals.structured_call_started.connect(call_started, dispatch_uid="ergonaut-metrics-call-started")
    signals.structured_call_finished.connect(call_finished, dispatch_uid="ergonaut-metrics-call-finished")
    signals.tool_call_finished.connect(tool_finished, dispatch_uid="ergonaut-metrics-tool")
    signals.memory_event.connect(memory_event, dispatch_uid="ergonaut-metrics-memory")
    for model, receiver in (
        (ergo_models.Worker, worker_saved),
        (ergo_models.ThreadMessage, thread_message_saved),
        (ergo_models.Wake, wake_saved),
        (ergo_models.RecurringWakeOccurrence, recurring_occurrence_saved),
        (ergo_models.ScheduleRun, schedule_run_saved),
    ):
        post_save.connect(receiver, sender=model, dispatch_uid=f"ergonaut-metrics-{model.__name__}")
