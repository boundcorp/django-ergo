"""Content-free, opt-in lifecycle observations.

Only database identifiers or opaque hexadecimal tokens belong in identifier fields.
The key consists of colon-separated stage names and opaque identifiers; never
include text, tool arguments, provider names, or other user-controlled content.
"""

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from django_ergo.settings import api_settings


class Stage(StrEnum):
    REQUESTED = "requested"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    CLAIMED = "claimed"
    STARTED = "started"
    PROGRESS = "progress"
    HEARTBEAT = "heartbeat"
    FINISHED = "finished"
    RESULT_PERSISTED = "result_persisted"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    CONSUMED = "consumed"
    RESUMED = "resumed"
    BLOCKER_CREATED = "blocker_created"
    BLOCKER_RESOLVED = "blocker_resolved"
    DEPENDENCY_ADDED = "dependency_added"
    DEPENDENCY_REMOVED = "dependency_removed"
    MESSAGE_ENQUEUED = "message_enqueued"
    MESSAGE_DISPATCHED = "message_dispatched"
    MESSAGE_CLAIMED = "message_claimed"
    MESSAGE_CONSUMED = "message_consumed"
    MESSAGE_REPLIED = "message_replied"
    WORKER_REQUESTED = "worker_requested"
    WORKER_QUEUED = "worker_queued"
    WORKER_STARTED = "worker_started"
    WORKER_PROGRESS = "worker_progress"
    WORKER_FINISHED = "worker_finished"
    WORKER_RESULT_PERSISTED = "worker_result_persisted"
    WORKER_RESULT_CONSUMED = "worker_result_consumed"


class Category(StrEnum):
    UNKNOWN = "unknown"
    WORKER = "worker"
    HANDOFF = "handoff"
    WORKER_RESULT = "worker_result"
    APPROVAL = "approval"
    QUESTION = "question"
    DEPENDENCY = "dependency"
    DOWNLOAD = "download"
    IMAGE = "image"
    GRADLE = "gradle"
    IOS_EAS = "ios_eas"
    COMPILE = "compile"
    TEST = "test"
    PACKAGE = "package"
    CI_DISPATCH = "ci_dispatch"
    CI_QUEUE = "ci_queue"
    DEPLOY = "deploy"
    RETRY = "retry"
    CACHE = "cache"


class Actor(StrEnum):
    UNKNOWN = "unknown"
    HUMAN = "human"
    LEE = "lee"
    SYSTEM = "system"
    WORKER = "worker"
    AGENT = "agent"
    MODEL = "model"
    PROVIDER = "provider"
    INFRASTRUCTURE = "infrastructure"


class State(StrEnum):
    UNKNOWN = "unknown"
    QUEUED = "queued"
    RUNNING = "running"
    EXECUTING = "executing"
    IDLE = "idle"
    STALLED = "stalled"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    OPEN = "open"
    RESOLVED = "resolved"
    HIT = "hit"
    MISS = "miss"
    WARM = "warm"
    COLD = "cold"
    ACTIONABLE_SURFACED = "actionable_surfaced"
    ROUTINE_INQUIRY = "routine_inquiry"
    NEW_SCOPE = "new_scope"
    DELIBERATE_PAUSE = "deliberate_pause"
    AMBIGUOUS = "ambiguous"


_OPAQUE = re.compile(
    r"(?:[0-9]{1,20}|[a-fA-F0-9]{16,128}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})\Z"
)
MAX_KEY_LENGTH = 255
MAX_ATTEMPT = 2_147_483_647


def _identifier(value: str | UUID | int, *, empty: bool = True) -> str:
    if isinstance(value, bool) or not isinstance(value, str | UUID | int):
        msg = "invalid identifier"
        raise TypeError(msg)
    text = str(value)
    if (not text and empty) or _OPAQUE.fullmatch(text):
        return text
    msg = "invalid identifier"
    raise ValueError(msg)


def _key(value: str) -> str:
    if not isinstance(value, str) or len(value) > MAX_KEY_LENGTH:
        msg = "invalid key"
        raise ValueError(msg)
    parts = value.split(":")
    if not parts or not all(
        part in Stage._value2member_map_ or _OPAQUE.fullmatch(part) for part in parts
    ):
        msg = "invalid key"
        raise ValueError(msg)
    if not any(_OPAQUE.fullmatch(part) for part in parts):
        msg = "key requires an opaque identifier"
        raise ValueError(msg)
    return value


def record_event(  # noqa: PLR0913
    stage: str | Stage,
    *,
    key: str,
    occurred_at: datetime | None = None,
    task_id: str = "",
    thread_id: str = "",
    turn_id: str = "",
    tool_id: str = "",
    worker_id: str = "",
    question_id: str = "",
    message_id: str = "",
    related_thread_id: str = "",
    related_task_id: str = "",
    category: str | Category = Category.UNKNOWN,
    actor: str | Actor = Actor.UNKNOWN,
    state: str | State = State.UNKNOWN,
    attempt: int = 1,
) -> None:
    """Append once; invalid observations and storage failures never affect work.

    The inner atomic block isolates database errors even inside an outer atomic
    transaction. A duplicate key cannot mutate the original observation.
    """
    try:
        if not api_settings.LIFECYCLE_EVENTS_ENABLED:
            return
        stage = Stage(stage).value
        category = Category(category).value
        actor = Actor(actor).value
        state = State(state).value
        key = _key(key)
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= MAX_ATTEMPT
        ):
            return
        if occurred_at is None:
            occurred_at = timezone.now()
        if not isinstance(occurred_at, datetime):
            return
        if settings.USE_TZ and timezone.is_naive(occurred_at):
            return
        if not settings.USE_TZ and timezone.is_aware(occurred_at):
            occurred_at = timezone.make_naive(
                occurred_at, timezone.get_default_timezone()
            )
        identifiers = {
            name: _identifier(value)
            for name, value in {
                "task_id": task_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "tool_id": tool_id,
                "worker_id": worker_id,
                "question_id": question_id,
                "message_id": message_id,
                "related_thread_id": related_thread_id,
                "related_task_id": related_task_id,
            }.items()
        }
        from django_ergo.models import LifecycleEvent

        with transaction.atomic():
            LifecycleEvent.objects.get_or_create(
                key=key,
                defaults={
                    "stage": stage,
                    "occurred_at": occurred_at,
                    "category": category,
                    "actor": actor,
                    "state": state,
                    "attempt": attempt,
                    **identifiers,
                },
            )
    except Exception:  # noqa: BLE001 - telemetry must never affect the caller
        # This is deliberately silent: exceptions can contain private inputs.
        return
