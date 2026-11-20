import os
import time

from celery.signals import (
    beat_init,
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    worker_init,
    worker_process_shutdown,
)

from .exporter import archive_process, start_exporter
from .metrics import (
    celery_task_failures_total,
    celery_task_latency_seconds,
    celery_task_published_total,
    celery_task_retries_total,
    celery_task_started_total,
    celery_task_succeeded_total,
)

_TASK_START_TIMES = {}


def _task_name(task):
    return getattr(task, "name", None) or "unknown"


@task_prerun.connect
def record_task_start(task_id=None, task=None, **kwargs):
    task_name = _task_name(task)
    _TASK_START_TIMES[task_id] = time.perf_counter()
    celery_task_started_total.labels(task_name=task_name).inc()


@task_postrun.connect
def record_task_completion(task_id=None, task=None, state=None, **kwargs):
    task_name = _task_name(task)
    started_at = _TASK_START_TIMES.pop(task_id, None)
    if started_at is not None:
        celery_task_latency_seconds.labels(task_name=task_name).observe(time.perf_counter() - started_at)
    if state == "SUCCESS":
        celery_task_succeeded_total.labels(task_name=task_name).inc()


@task_failure.connect
def record_task_failure(task_id=None, sender=None, **kwargs):
    task_name = _task_name(sender)
    celery_task_failures_total.labels(task_name=task_name).inc()


@task_retry.connect
def record_task_retry(sender=None, **kwargs):
    celery_task_retries_total.labels(task_name=_task_name(sender)).inc()


@before_task_publish.connect
def record_task_published(sender=None, **kwargs):
    # sender is the task name here, not the task.
    celery_task_published_total.labels(task_name=str(sender or "unknown")).inc()


@worker_init.connect
def serve_worker_metrics(**kwargs):
    # The worker's parent process: prefork children write to the shared directory.
    start_exporter("celery")


@beat_init.connect
def serve_beat_metrics(**kwargs):
    start_exporter("beat")


@worker_process_shutdown.connect
def archive_child_metrics(**kwargs):
    try:
        archive_process(os.getpid())
    except Exception:  # noqa: BLE001 — never block a child's exit on metrics
        pass
