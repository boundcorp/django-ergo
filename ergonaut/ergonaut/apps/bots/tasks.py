"""Bot turns and thread messages as Celery tasks, and live "session changed" notices.

The web app queues a turn (a message, or an approval answer) with
``queue_turn`` and returns; a worker runs it and the browser follows along
over SSE. Without a broker Celery runs tasks eagerly, so the turn runs inside
the request, as before.

Turns for one session never overlap: each takes a Redis lock on the session
(when ``REDIS_URL`` is set). Every write to a session's messages or calls
publishes its id on ``ergonaut:sessions``, which wakes the SSE streams
watching it.
"""

from __future__ import annotations

import contextlib
import logging
import os

from asgiref.sync import async_to_sync
from celery import shared_task

logger = logging.getLogger(__name__)

CHANNEL = "ergonaut:sessions"
LOCK_SECONDS = 15 * 60


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
def session_lock(session_id: str):
    client = redis_client()
    if client is None:
        yield
        return
    lock = client.lock(f"ergonaut:turn:{session_id}", timeout=LOCK_SECONDS, blocking_timeout=LOCK_SECONDS)
    acquired = lock.acquire()
    try:
        yield
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                lock.release()


def notify(session_id) -> None:
    """Tell SSE streams that a session changed. Never raises."""
    client = redis_client()
    if client is None or not session_id:
        return
    try:
        client.publish(CHANNEL, str(session_id))
    except Exception:  # noqa: BLE001 — live updates are best-effort
        logger.debug("Could not publish a session change", exc_info=True)


@shared_task(name="ergonaut.run_turn", ignore_result=True)
def run_turn(session_id: str, message: str | None = None, approve: bool | None = None) -> None:
    """Answer a message, or resume a turn paused for approval."""
    from django_ergo.bots import webhooks
    from django_ergo.conversation.models import ConversationSession

    session = ConversationSession.objects.select_related("user").get(id=session_id)
    registry = webhooks.get_registry()
    if registry is None or session.bot_name not in registry:
        logger.error("No bot %r for session %s", session.bot_name, session_id)
        return
    bot = registry.get(session.bot_name)
    with session_lock(session_id):
        if message is not None:
            async_to_sync(bot.ask)(session, message)
        elif approve is not None and async_to_sync(bot.pending_call)(session) is not None:
            async_to_sync(bot.resume)(session, approve)
    notify(session_id)


def queue_turn(session_id, *, message: str | None = None, approve: bool | None = None) -> bool:
    """Queue a turn. Returns True when a worker will run it, False when it already ran."""
    from django.conf import settings

    result = run_turn.delay(str(session_id), message, approve)
    notify(session_id)
    return not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) and result is not None


@shared_task(name="ergonaut.deliver_thread_message", ignore_result=True)
def deliver_thread_message(message_id: str) -> None:
    """Run the recipient's turn for a bot-to-bot message (see django_ergo.bots.messaging)."""
    from django_ergo.bots import messaging
    from django_ergo.conversation.models import ThreadMessage

    recipient_id = (
        ThreadMessage.objects.filter(id=message_id).values_list("recipient_session_id", flat=True).first()
    )
    if recipient_id is None:
        return
    with session_lock(str(recipient_id)):
        messaging.deliver(message_id)
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
