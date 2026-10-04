"""Archive bot threads nobody has used for a while.

A thread (never a root chat) is idle when its last turn, and the session
itself, are older than its bot's ``sessions.archive_after_days`` (default 7,
0 = never). Threads with a turn in progress or waiting for approval, or with
a thread message still being answered, are left alone. Archiving closes the
session and records ``archived_at``; a new message reopens it.

Run ``archive_idle_threads(registry)`` on a schedule (Ergonaut's Celery beat
does it hourly).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import Max
from django.db.models.functions import Greatest
from django.utils import timezone

from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import StructuredCallStatus
from django_ergo.conversation.models import ThreadMessageStatus

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from django_ergo.bots.runtime import Bot

BUSY_CALLS = [StructuredCallStatus.IN_PROGRESS, StructuredCallStatus.AWAITING_APPROVAL]
OPEN_MESSAGES = [
    ThreadMessageStatus.QUEUED,
    ThreadMessageStatus.DELIVERED,
    ThreadMessageStatus.WAITING,
]


def archive(session: ConversationSession, reason: str = "") -> None:
    session.status = "completed"
    session.metadata = {
        **(session.metadata or {}),
        "archived_at": timezone.now().isoformat(),
        **({"archived_reason": reason} if reason else {}),
    }
    session.save(update_fields=["status", "metadata", "updated_at"])


def reopen(session: ConversationSession) -> bool:
    """Make an archived thread active again. Returns whether it was archived."""
    if session.status != "completed":
        return False
    session.status = "active"
    session.metadata = {
        k: v
        for k, v in (session.metadata or {}).items()
        if k
        not in ("archived_at", "archived_reason", "resolved_by", "resolved_summary")
    }
    session.save(update_fields=["status", "metadata", "updated_at"])
    return True


def idle_threads(bot: Bot, now: datetime | None = None):
    days = bot.definition.archive_after_days
    if days <= 0:
        return ConversationSession.objects.none()
    cutoff = (now or timezone.now()) - timedelta(days=days)
    return (
        bot.sessions()
        .filter(metadata__bot_role="thread")
        .exclude(status="completed")
        .exclude(structured_calls__status__in=BUSY_CALLS)
        .exclude(thread_messages__status__in=OPEN_MESSAGES)
        .annotate(last_turn=Max("structured_calls__updated_at"))
        .annotate(last_activity=Greatest("updated_at", "last_turn"))
        .filter(last_activity__lt=cutoff)
        .distinct()
    )


def archive_idle_threads(bots: Iterable[Bot], now: datetime | None = None) -> list[str]:
    """Archive every idle thread of ``bots``. Returns the archived session ids."""
    archived = []
    for bot in bots:
        for session in idle_threads(bot, now):
            archive(session, reason="idle")
            archived.append(str(session.id))
    return archived
