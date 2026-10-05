"""Bot turns and thread messages as Celery tasks, and live "session changed" notices.

The web app queues a turn (a message, or an approval answer) and returns; a
worker runs it and the browser follows along over SSE. Without a broker Celery
runs tasks eagerly, so the turn runs inside the request, as before.

Turns for one session never overlap: each takes the session's turn lock (Redis
when ``REDIS_URL`` is set, else in-process). Messages go through the session's
inbox (``ergonaut:turn:<session>:inbox``): ``queue_message`` pushes one and
queues ``run_turn``. The turn holding the lock takes everything in the inbox
as its message, and at each step of the turn (after a tool call's results are
in) takes anything newer as a steering message the model sees on its next
step. Whatever arrives after a turn's last step starts the next turn, under
the same lock. A ``run_turn`` that finds the lock taken just ends, since the
holder will pick its message up; each holder checks the inbox again after
releasing the lock, so nothing is left behind. An approval answer that finds
the lock taken retries.

Stop sets ``ergonaut:turn:<session>:stop``; the running turn ends at its next
step with status ``stopped``. An interrupt sets it and then queues a message,
which runs as the next turn.

``@bot_task`` work runs on its own ``bot_tasks`` queue. Every write to a session's
messages or calls publishes its id on ``ergonaut:sessions``, which wakes the SSE
streams watching it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid

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
# A stop nobody acted on (no turn reached a step) is forgotten after this long.
STOP_TTL_SECONDS = 10 * 60
# Messages left in an inbox nobody drained (no worker running) expire after this long.
INBOX_TTL_SECONDS = 24 * 60 * 60


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


# -- stop flag and inbox -------------------------------------------------------
#
# Without Redis these live in this process, like the in-process turn lock.

_local_stops: dict[str, float] = {}  # session id -> when the stop expires (monotonic)
_local_inboxes: dict[str, list[str]] = {}
_local_guard = threading.Lock()


def _stop_key(session_id) -> str:
    return f"ergonaut:turn:{session_id}:stop"


def _inbox_key(session_id) -> str:
    return f"ergonaut:turn:{session_id}:inbox"


def turn_running(session_id) -> bool:
    """Whether a turn (or a thread message's delivery) holds the session's lock now."""
    client = redis_client()
    if client is None:
        from django_ergo.bots import messaging

        lock = messaging._session_locks.get(str(session_id))
        return lock is not None and lock.locked()
    return bool(client.exists(f"ergonaut:turn:{session_id}"))


def request_stop(session_id) -> bool:
    """Ask the running turn to stop at its next step. Returns whether one was running."""
    if not turn_running(session_id):
        return False
    client = redis_client()
    if client is None:
        with _local_guard:
            _local_stops[str(session_id)] = time.monotonic() + STOP_TTL_SECONDS
    else:
        client.set(_stop_key(session_id), "1", ex=STOP_TTL_SECONDS)
    return True


def stop_requested(session_id) -> bool:
    client = redis_client()
    if client is None:
        with _local_guard:
            return _local_stops.get(str(session_id), 0) > time.monotonic()
    return bool(client.exists(_stop_key(session_id)))


def clear_stop(session_id) -> None:
    client = redis_client()
    if client is None:
        with _local_guard:
            _local_stops.pop(str(session_id), None)
    else:
        client.delete(_stop_key(session_id))


def push_message(session_id, text: str, attachment_ids: list[str] | None = None) -> str:
    """Queue a message for the session's turn. Returns its id (to unsend it)."""
    item_id = uuid.uuid4().hex
    item = json.dumps({"id": item_id, "text": text, "attachment_ids": [str(a) for a in attachment_ids or []]})
    client = redis_client()
    if client is None:
        with _local_guard:
            _local_inboxes.setdefault(str(session_id), []).append(item)
        return item_id
    with client.pipeline() as pipe:
        pipe.rpush(_inbox_key(session_id), item)
        pipe.expire(_inbox_key(session_id), INBOX_TTL_SECONDS)
        pipe.execute()
    return item_id


def _inbox_raw(session_id) -> list:
    client = redis_client()
    if client is None:
        with _local_guard:
            return list(_local_inboxes.get(str(session_id), []))
    return client.lrange(_inbox_key(session_id), 0, -1)


def peek_inbox(session_id) -> list[dict]:
    """Messages waiting for the model, oldest first, without taking them."""
    return [json.loads(item) for item in _inbox_raw(session_id)]


def unsend(session_id, item_id: str) -> dict | None:
    """Take one waiting message back before the model sees it. Returns it, or None if a
    turn already took it (or there's no such message)."""
    for raw in _inbox_raw(session_id):
        item = json.loads(raw)
        if item.get("id") != item_id:
            continue
        client = redis_client()
        if client is None:
            with _local_guard:
                waiting = _local_inboxes.get(str(session_id), [])
                if raw not in waiting:
                    return None
                waiting.remove(raw)
        elif not client.lrem(_inbox_key(session_id), 1, raw):
            return None  # drained between our read and the removal
        notify(session_id)
        return item
    return None


def drain_inbox(session_id) -> list[dict]:
    """Take every message waiting in the session's inbox, oldest first."""
    client = redis_client()
    if client is None:
        with _local_guard:
            raw = _local_inboxes.pop(str(session_id), [])
    else:
        with client.pipeline() as pipe:
            pipe.lrange(_inbox_key(session_id), 0, -1)
            pipe.delete(_inbox_key(session_id))
            raw = pipe.execute()[0]
    return [json.loads(item) for item in raw]


def inbox_waiting(session_id) -> bool:
    client = redis_client()
    if client is None:
        with _local_guard:
            return bool(_local_inboxes.get(str(session_id)))
    return bool(client.llen(_inbox_key(session_id)))


class InboxControl:
    """Turn control for a user's turn: stops on the stop flag, steers with the inbox.

    Uploads sent with steering messages are kept in ``uploads`` for the caller to
    remove once the turn has stored copies of them.
    """

    # Before a steering message in a turn another bot (or a finished worker) started, so the
    # model knows it's from the user, not from whoever sent the request.
    DELEGATED_NOTE = "[The user sent this while you were working on the request above. Follow it.]"

    def __init__(self, session, *, delegated: bool = False):
        self.session = session
        self.delegated = delegated
        self.uploads: list = []

    def finish(self) -> None:
        """Remove uploads the turn has stored copies of."""
        remove_uploads(self.uploads)
        self.uploads = []

    async def check(self):
        from asgiref.sync import sync_to_async

        return await sync_to_async(self._check, thread_sensitive=True)()

    def _check(self):
        from django_ergo.conversation.structured import SteeringMessage, TurnSignal

        session_id = str(self.session.id)
        if stop_requested(session_id):
            # Leave the inbox alone: an interrupt's message starts the next turn.
            return TurnSignal(stop=True)
        items = drain_inbox(session_id)
        if not items:
            return TurnSignal()
        text, attachment_ids = combine(items)
        attachments, uploads = take_uploads(self.session, attachment_ids)
        self.uploads.extend(uploads)
        notify(session_id)
        if self.delegated:
            text = f"{self.DELEGATED_NOTE}\n\n{text}"
        return TurnSignal(messages=[SteeringMessage(text, attachments or None)])


def combine(items: list[dict]) -> tuple[str, list[str]]:
    """Messages queued together, as one message and its files."""
    texts = [item["text"] for item in items if item.get("text")]
    attachment_ids = [a for item in items for a in item.get("attachment_ids") or []]
    return "\n\n".join(texts) or "(see the attached files)", attachment_ids


def remove_uploads(rows) -> None:
    for row in rows:
        row.file.delete(save=False)
        row.delete()


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
    """Answer the session's inbox, or answer the approval the user saw (then the inbox).

    ``approval_ids`` are the tool calls the user was shown; if the turn now
    waits on anything else (a double click after it moved on), nothing runs.
    A ``message`` passed in (a task queued before the inbox) joins the inbox.
    """
    from django_ergo.conversation.models import ConversationSession

    if message is not None:
        push_message(session_id, message, attachment_ids)
    session = ConversationSession.objects.select_related("user").get(id=session_id)
    if approve is None:
        with session_lock(session_id, wait=False) as locked:
            if not locked:
                return  # the running turn takes the message
            answer_inbox(session)
    else:
        eager = getattr(self.request, "is_eager", False)
        with session_lock(session_id, wait=eager) as locked:
            if not locked:
                # Another turn of this session is running; try again shortly.
                raise self.retry(countdown=3)
            answer_approval(session, approve, approval_ids)
            answer_inbox(session)
    queue_waiting(session_id)


def loaded_bot(session):
    from django_ergo.bots import webhooks

    registry = webhooks.get_registry()
    if registry is None or session.bot_name not in registry:
        msg = f"The {session.bot_name} bot isn't loaded"
        raise LookupError(msg)
    return registry.get(session.bot_name)


def answer_approval(session, approve: bool, approval_ids: list[str] | None) -> None:
    session_id = str(session.id)
    clear_stop(session_id)
    try:
        bot = loaded_bot(session)
        pending = async_to_sync(bot.pending_call)(session)
        waiting = sorted(a["id"] for a in (pending.metadata or {}).get("pending_approvals", [])) if pending else []
        if pending is not None and (approval_ids is None or sorted(approval_ids) == waiting):
            control = InboxControl(session)
            async_to_sync(bot.resume)(session, approve, control=control)
            remove_uploads(control.uploads)
    except Exception as exc:
        logger.exception("Turn for session %s failed", session_id)
        record_failure(session, None, exc)
    finally:
        notify(session_id)


def answer_inbox(session) -> None:
    """Run turns, holding the lock, until the inbox is empty."""
    session_id = str(session.id)
    while True:
        clear_stop(session_id)  # a stop meant for an earlier turn
        items = drain_inbox(session_id)
        if not items:
            return
        message, attachment_ids = combine(items)
        try:
            bot = loaded_bot(session)
            attachments, uploads = take_uploads(session, attachment_ids)
            control = InboxControl(session)
            async_to_sync(bot.ask)(session, message, attachments=attachments or None, control=control)
            remove_uploads([*uploads, *control.uploads])
        except Exception as exc:
            logger.exception("Turn for session %s failed", session_id)
            record_failure(session, message, exc)
        finally:
            notify(session_id)


def queue_waiting(session_id) -> None:
    """After releasing a session's lock: a message that arrived as the holder finished
    gets a turn of its own."""
    if inbox_waiting(session_id):
        run_turn.delay(str(session_id))


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
    approve: bool | None = None,
    approval_ids: list[str] | None = None,
) -> bool:
    """Queue a turn. Returns True when a worker will run it, False when it already ran."""
    from django.conf import settings

    result = run_turn.delay(str(session_id), None, approve, [], approval_ids)
    notify(session_id)
    eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
    # Eager, a message for a turn running in another request waits for that turn.
    return (not eager and result is not None) or (eager and turn_running(session_id))


def queue_message(session_id, text: str, attachment_ids: list[str] | None = None, *, interrupt: bool = False) -> bool:
    """Send a message: it steers the running turn, or starts one. ``interrupt`` stops the
    running turn first, so the message starts the next one. Returns as queue_turn does."""
    if interrupt:
        request_stop(session_id)
    push_message(session_id, text, attachment_ids)
    return queue_turn(session_id)


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
    if locked:
        queue_waiting(recipient_id)
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
                if locked:
                    queue_waiting(recipient_id)
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


@shared_task(name="ergonaut.refresh_pull_requests", ignore_result=True)
def refresh_pull_requests(max_age_seconds: int = 120) -> int:
    """Read the live state of recorded pull requests that may still change (not merged or
    closed, last checked over ``max_age_seconds`` ago), so cards and chips stay current."""
    from datetime import datetime

    from django.utils import timezone
    from django_ergo.conversation.links import pull_requests_to_refresh, refresh_pull_request

    stale = timezone.now() - timezone.timedelta(seconds=max_age_seconds)
    refreshed = 0
    for row in pull_requests_to_refresh()[:50]:
        checked = (row.metadata or {}).get("checked_at")
        if checked and datetime.fromisoformat(checked) > stale:
            continue
        before = dict(row.metadata or {})
        if refresh_pull_request(row):
            refreshed += 1
            if {k: before.get(k) for k in ("title", "state", "checks")} != {
                k: row.metadata.get(k) for k in ("title", "state", "checks")
            }:
                notify(row.session_id)
    return refreshed


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


@shared_task(name="ergonaut.auto_upgrade", ignore_result=True)
def auto_upgrade() -> str:
    """Upgrade to a newer GitHub release if there is one and nothing is running
    (beat runs this every ``ERGONAUT_AUTO_UPGRADE_SECONDS``; see ergonaut/upgrades).
    A busy instance is checked again next time instead of holding the worker: each
    check waits ``ERGONAUT_AUTO_UPGRADE_WAIT_SECONDS`` (default 300) for a 20-second
    gap with no turn running."""
    import os

    from ergonaut import upgrades

    wait = float(os.environ.get("ERGONAUT_AUTO_UPGRADE_WAIT_SECONDS") or 300)
    result = upgrades.run(wait_timeout=wait, quiet_for=20)
    logger.info("auto upgrade: %s", result)
    return result


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


# A call still in progress, untouched this long, whose session holds no turn lock,
# belongs to a turn that died (a killed worker or shell, a restart).
DEAD_TURN_MINUTES = 10
# When someone presses stop and no turn holds the lock, a call this old is dead: a
# live turn's lock outlives its holder by at most LOCK_TTL_SECONDS.
STOPPED_DEAD_SECONDS = 2 * LOCK_TTL_SECONDS
DEAD_TURN_ERROR = "The turn stopped without finishing (its worker or process exited)."


def fail_dead_calls(calls) -> int:
    """Fail the given in-progress calls whose session holds no turn lock; needs Redis."""
    from django.utils import timezone
    from django_ergo.conversation.models import StructuredCall

    if redis_client() is None:
        return 0
    recovered = 0
    for call in calls.only("id", "session_id")[:100]:
        if turn_running(call.session_id):
            continue
        updated = StructuredCall.objects.filter(id=call.id, status="in_progress").update(
            status="failed", error=DEAD_TURN_ERROR, updated_at=timezone.now()
        )
        if updated:
            recovered += 1
            notify(call.session_id)
            queue_waiting(call.session_id)
    return recovered


@shared_task(name="ergonaut.recover_dead_turns", ignore_result=True)
def recover_dead_turns() -> int:
    """Fail in-progress calls whose turn died, so their chats stop showing busy and
    queued messages go out. Beat runs this; it needs Redis, where every turn's lock
    lives (and expires a minute after its holder dies)."""
    from django.utils import timezone
    from django_ergo.conversation.models import StructuredCall

    old = timezone.now() - timezone.timedelta(minutes=DEAD_TURN_MINUTES)
    return fail_dead_calls(
        StructuredCall.objects.filter(status="in_progress", updated_at__lt=old, session__isnull=False)
    )


def recover_stopped_session(session_id) -> int:
    """Stop pressed with no turn running: fail the session's dead calls now, instead of
    leaving the chat on "Stopping" until recover_dead_turns gets to them."""
    from django.utils import timezone
    from django_ergo.conversation.models import StructuredCall

    old = timezone.now() - timezone.timedelta(seconds=STOPPED_DEAD_SECONDS)
    return fail_dead_calls(
        StructuredCall.objects.filter(status="in_progress", updated_at__lt=old, session_id=session_id)
    )
