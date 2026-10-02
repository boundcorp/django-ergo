"""Bot turns and thread messages as Celery tasks, and live "session changed" notices.

The web app queues a turn (a message, or an approval answer) with
``queue_turn`` and returns; a worker runs it and the browser follows along
over SSE. Without a broker Celery runs tasks eagerly, so the turn runs inside
the request, as before.

Turns for one session never overlap: each takes the session's turn lock (Redis
when ``REDIS_URL`` is set, else in-process); a turn that finds it taken retries.
``@bot_task`` work runs on its own ``bot_tasks`` queue. Every write to a session's messages or calls
publishes its id on ``ergonaut:sessions``, which wakes the SSE streams
watching it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading

from asgiref.sync import async_to_sync
from celery import shared_task

logger = logging.getLogger(__name__)

CHANNEL = "ergonaut:sessions"
# The longest a turn waits for the session's lock.
LOCK_SECONDS = 2 * 60 * 60
# A held Redis lock lives this long and is renewed every LOCK_RENEW_SECONDS while
# its turn runs, so a lock left by a killed worker or a restart clears within a minute.
LOCK_TTL_SECONDS = 60
LOCK_RENEW_SECONDS = 20
# How long a @bot_task wait() blocks when the tool gives no timeout.
DEFAULT_TASK_WAIT_SECONDS = 600


_clients: dict = {}


def redis_client():
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    if url not in _clients:
        import redis

        _clients[url] = redis.Redis.from_url(url)
    return _clients[url]


@contextlib.contextmanager
def session_lock(session_id: str, *, wait: bool = True):
    """Hold the session's turn lock; yields whether it was acquired.

    With Redis it's shared by every process; without it, an in-process lock
    (shared with django_ergo.bots.messaging) still keeps one process's turns apart.
    """
    client = redis_client()
    if client is None:
        from django_ergo.bots import messaging

        lock = messaging._session_locks.setdefault(str(session_id), threading.Lock())
        acquired = lock.acquire(timeout=LOCK_SECONDS) if wait else lock.acquire(blocking=False)
    else:
        # Not thread-local: the renewal thread extends the lock this thread holds.
        lock = client.lock(f"ergonaut:turn:{session_id}", timeout=LOCK_TTL_SECONDS, thread_local=False)
        acquired = lock.acquire(blocking=wait, blocking_timeout=LOCK_SECONDS if wait else None)
    stop = threading.Event()
    if acquired and client is not None:
        threading.Thread(target=_keep_lock, args=(lock, stop), daemon=True).start()
    try:
        yield acquired
    finally:
        stop.set()
        if acquired:
            with contextlib.suppress(Exception):
                lock.release()


def _keep_lock(lock, stop: threading.Event) -> None:
    """Renew a held Redis turn lock until ``stop`` is set."""
    while not stop.wait(LOCK_RENEW_SECONDS):
        try:
            lock.reacquire()
        except Exception:  # noqa: BLE001 — a lost lock just stops being renewed
            logger.warning("Could not renew a session turn lock", exc_info=True)
            return


def notify(session_id) -> None:
    """Tell SSE streams that a session changed. Never raises."""
    client = redis_client()
    if client is None or not session_id:
        return
    try:
        client.publish(CHANNEL, str(session_id))
    except Exception:  # noqa: BLE001 — live updates are best-effort
        logger.debug("Could not publish a session change", exc_info=True)


def take_uploads(session, attachment_ids: list[str]) -> tuple[list, list]:
    """Session files to send with a message, as Attachments; the uploads are removed
    once the message carries copies of them."""
    from django_ergo.conversation.attachments import Attachment

    rows = list(session.attachments.filter(id__in=attachment_ids, message_sequence__isnull=True, source="upload"))
    attachments = []
    for row in rows:
        with row.file.open("rb") as handle:
            data = handle.read()
        attachments.append(
            Attachment(
                media_type=row.media_type,
                data=data,
                filename=row.filename,
                transcript=row.transcript,
                kind=row.kind,
                metadata={**(row.metadata or {}), "uploaded_as": str(row.id)},
            )
        )
    return attachments, rows


@shared_task(name="ergonaut.run_turn", ignore_result=True, bind=True, max_retries=None)
def run_turn(  # noqa: PLR0913
    self,
    session_id: str,
    message: str | None = None,
    approve: bool | None = None,
    attachment_ids: list[str] | None = None,
    approval_ids: list[str] | None = None,
) -> None:
    """Answer a message (with any uploaded files), or answer the approval the user saw.

    ``approval_ids`` are the tool calls the user was shown; if the turn now
    waits on anything else (a double click after it moved on), nothing runs.
    """
    from django_ergo.bots import webhooks
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.select_related("user").get(id=session_id)
    eager = getattr(self.request, "is_eager", False)
    with session_lock(session_id, wait=eager) as locked:
        if not locked:
            # Another turn of this session is running; try again shortly.
            raise self.retry(countdown=3)
        try:
            registry = webhooks.get_registry()
            if registry is None or session.bot_name not in registry:
                msg = f"The {session.bot_name} bot isn't loaded"
                raise LookupError(msg)
            bot = registry.get(session.bot_name)
            if message is not None:
                attachments, uploads = take_uploads(session, attachment_ids or [])
                async_to_sync(bot.ask)(session, message, attachments=attachments or None)
                for row in uploads:
                    row.file.delete(save=False)
                    row.delete()
            elif approve is not None:
                pending = async_to_sync(bot.pending_call)(session)
                waiting = (
                    sorted(a["id"] for a in (pending.metadata or {}).get("pending_approvals", [])) if pending else []
                )
                if pending is not None and (approval_ids is None or sorted(approval_ids) == waiting):
                    async_to_sync(bot.resume)(session, approve)
        except Exception as exc:
            logger.exception("Turn for session %s failed", session_id)
            record_failure(session, message, exc)
        finally:
            notify(session_id)


def record_failure(session, message: str | None, exc: Exception) -> None:
    """Make a failure before the turn's call existed visible, so the chat stops waiting."""
    from django.utils import timezone
    from django_ergo.conversation.models import StructuredCall

    recent = timezone.now() - timezone.timedelta(minutes=1)
    if session.structured_calls.filter(created_at__gte=recent, status="failed").exists():
        return  # the call itself recorded it
    StructuredCall.objects.create(
        kind="chat_reply",
        session=session,
        user_id=session.user_id,
        request=message or "",
        status="failed",
        error=f"{type(exc).__name__}: {exc}"[:2000],
    )


def queue_turn(
    session_id,
    *,
    message: str | None = None,
    approve: bool | None = None,
    attachment_ids: list[str] | None = None,
    approval_ids: list[str] | None = None,
) -> bool:
    """Queue a turn. Returns True when a worker will run it, False when it already ran."""
    from django.conf import settings

    result = run_turn.delay(str(session_id), message, approve, list(attachment_ids or []), approval_ids)
    notify(session_id)
    return not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) and result is not None


@shared_task(name="ergonaut.deliver_thread_message", ignore_result=True)
def deliver_thread_message(message_id: str) -> None:
    """Run the recipient's turn for a bot-to-bot message (see django_ergo.bots.messaging)."""
    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    recipient_id = ThreadMessage.objects.filter(id=message_id).values_list("recipient_session_id", flat=True).first()
    if recipient_id is None:
        return
    with session_lock(str(recipient_id), wait=False) as locked:
        if locked:
            messaging.deliver(message_id)
        # else the recipient is mid-turn: the message stays queued and goes out
        # when that turn ends, or on the next sweep.
    notify(recipient_id)


def queue_thread_message(message_id: str) -> None:
    """THREAD_MESSAGE_RUNNER: a worker delivers it. Without a broker, a background thread does,
    since an eager task would run inside the sender's own turn."""
    from django.conf import settings

    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        import threading

        from django.db import close_old_connections
        from django_ergo.bots import messaging

        def run():
            try:
                recipient_id = (
                    messaging.ThreadMessage.objects.filter(id=message_id)
                    .values_list("recipient_session_id", flat=True)
                    .first()
                )
                with session_lock(str(recipient_id), wait=False) as locked:
                    if locked:
                        messaging.deliver(message_id)
            finally:
                close_old_connections()

        threading.Thread(target=run, daemon=True).start()
        return
    deliver_thread_message.delay(message_id)


@shared_task(name="ergonaut.archive_idle_threads", ignore_result=True)
def archive_idle_threads() -> list[str]:
    """Archive threads idle past their bot's sessions.archive_after_days."""
    from django_ergo.bots import archival, webhooks

    registry = webhooks.get_registry()
    archived = archival.archive_idle_threads(list(registry) if registry is not None else [])
    for session_id in archived:
        notify(session_id)
    if archived:
        logger.info("Archived %d idle threads", len(archived))
    return archived


@shared_task(name="ergonaut.run_bot_task", queue="bot_tasks")
def run_bot_task(bot_name: str, task_name: str, args: list, kwargs: dict):
    """Run a bot's @bot_task on a worker (see django_ergo.bots.background)."""
    from django_ergo.bots import background

    return background.execute(bot_name, task_name, args, kwargs)


class CeleryTaskHandle:
    """A @bot_task running on a Celery worker; wait() or await it."""

    def __init__(self, result):
        self.result = result
        self.id = result.id

    def done(self) -> bool:
        return self.result.ready()

    def wait(self, timeout: float | None = None):
        # Tools run inside turn tasks, so waiting on another task is allowed here;
        # bot tasks run on their own queue and worker, so a turn can't starve them.
        return self.result.get(timeout=timeout or DEFAULT_TASK_WAIT_SECONDS, disable_sync_subtasks=False)

    def __await__(self):
        import asyncio

        return asyncio.to_thread(self.wait).__await__()


def celery_bot_task(bot_name: str, task_name: str, args: list, kwargs: dict) -> CeleryTaskHandle:
    """BOT_TASK_RUNNER: run @bot_task functions on Celery workers."""
    return CeleryTaskHandle(run_bot_task.delay(bot_name, task_name, args, kwargs))


@shared_task(name="ergonaut.pull_bot_repos", ignore_result=True)
def pull_bot_repos() -> dict[str, str]:
    """Fast-forward the bot checkouts so merged changes go live (beat runs this)."""
    from ergonaut.apps.bots.reloading import pull_all

    return pull_all()


@shared_task(name="ergonaut.redispatch_thread_messages", ignore_result=True)
def redispatch_thread_messages() -> int:
    """Send on thread messages that have waited over a minute (beat runs this)."""
    from django_ergo.bots import messaging

    return messaging.redispatch_waiting(older_than=60)


@shared_task(name="ergonaut.run_schedules", ignore_result=True)
def run_schedules() -> list[str]:
    """Send the bots' scheduled messages that are due this minute (beat runs this)."""
    from django_ergo.bots import schedules, webhooks

    registry = webhooks.get_registry()
    ran = schedules.run_due(list(registry) if registry is not None else [])
    if ran:
        logger.info("Ran schedules: %s", ", ".join(ran))
    return ran


@shared_task(name="ergonaut.run_schedule", ignore_result=True)
def run_schedule(run_id: int) -> None:
    """Carry out one schedule run's actions, in order (see django_ergo.bots.schedules)."""
    from django_ergo.bots import schedules

    schedules.run_actions(run_id)


def queue_schedule_run(run_id: int) -> None:
    """SCHEDULE_RUNNER: each due run is its own task, so slow code doesn't hold up others."""
    run_schedule.delay(run_id)


@shared_task(name="ergonaut.name_thread", ignore_result=True)
def name_thread(session_id: str, message: str) -> None:
    """Title a new thread from its first message (structured call new_thread_metadata)."""
    from django_ergo.bots import webhooks
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.select_related("user").filter(id=session_id).first()
    registry = webhooks.get_registry()
    if session is None or registry is None or session.bot_name not in registry:
        return
    try:
        found = async_to_sync(registry.get(session.bot_name).thread_metadata)(
            message, user=session.user, session=session
        )
    except Exception:
        logger.exception("Naming thread %s failed", session_id)
        return
    if found.get("title"):
        import json

        from django.db.models.expressions import RawSQL
        from django.utils import timezone

        # Merge just the title in one UPDATE, so a turn writing the metadata at the
        # same moment keeps its own keys.
        ConversationSession.objects.filter(id=session_id).update(
            metadata=RawSQL("COALESCE(metadata, '{}'::jsonb) || %s::jsonb", [json.dumps({"title": found["title"]})]),
            updated_at=timezone.now(),
        )
        notify(session_id)


def queue_thread_naming(session_id: str, message: str) -> None:
    """Name it on a worker; without a broker, in a background thread (it's a model call)."""
    from django.conf import settings

    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        from django.db import close_old_connections

        def run():
            try:
                name_thread(session_id, message)
            finally:
                close_old_connections()

        threading.Thread(target=run, daemon=True).start()
        return
    name_thread.delay(session_id, message)


@shared_task(name="ergonaut.run_worker", ignore_result=True, queue="bot_tasks")
def run_worker(worker_id: str) -> None:
    """One step of a thread's worker (see django_ergo.bots.workers)."""
    from django_ergo.bots import workers

    workers.run(worker_id)


def queue_worker(worker_id: str, delay: float) -> None:
    """WORKER_RUNNER: each step is its own task, so a worker that polls for hours never
    holds a worker process, and survives restarts (see resume_workers)."""
    from django.conf import settings

    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        from django.db import close_old_connections
        from django_ergo.bots import workers

        def run():
            try:
                workers.run(worker_id)
            finally:
                close_old_connections()

        timer = threading.Timer(delay, run)
        timer.daemon = True
        timer.start()
        return
    run_worker.apply_async((worker_id,), countdown=max(0, delay))


@shared_task(name="ergonaut.resume_workers", ignore_result=True)
def resume_workers() -> int:
    """Restart workers whose next step is long overdue (a lost task, a restart). Beat runs this."""
    from django.utils import timezone
    from django_ergo.bots import workers
    from django_ergo.conversation.models import Worker

    overdue = Worker.objects.filter(
        status__in=["queued", "running"], next_poll_at__lt=timezone.now() - timezone.timedelta(minutes=3)
    )
    count = 0
    for worker in overdue[:100]:
        workers.schedule(worker, 0)
        count += 1
    return count
