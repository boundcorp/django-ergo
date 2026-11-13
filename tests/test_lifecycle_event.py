from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from django_ergo.conversation.lifecycle import record_event
from django_ergo.models import LifecycleEvent


@pytest.mark.django_db
def test_disabled_by_default():
    record_event("started", key=f"started:{uuid4()}")
    assert not LifecycleEvent.objects.exists()


@pytest.mark.django_db
@override_settings(DJANGO_ERGO={"LIFECYCLE_EVENTS_ENABLED": True})
def test_persistence_and_idempotency():
    worker = uuid4()
    thread = uuid4()
    occurred = timezone.now() - timedelta(minutes=1)
    key = f"worker_started:{worker}:1"
    record_event(
        "worker_started",
        key=key,
        worker_id=worker,
        thread_id=thread,
        occurred_at=occurred,
        category="worker",
        actor="worker",
        state="running",
    )
    record_event("worker_finished", key=key, worker_id=worker, state="completed")
    event = LifecycleEvent.objects.get(key=key)
    assert event.stage == "worker_started"
    assert event.worker_id == str(worker)
    assert event.thread_id == str(thread)
    assert event.occurred_at == occurred
    assert event.observed_at >= occurred
    assert (event.category, event.actor, event.state, event.attempt) == (
        "worker",
        "worker",
        "running",
        1,
    )
    assert LifecycleEvent.objects.count() == 1


@pytest.mark.django_db
@override_settings(DJANGO_ERGO={"LIFECYCLE_EVENTS_ENABLED": True})
def test_out_of_order_occurrences_and_retries_are_distinct():
    worker = uuid4()
    now = timezone.now()
    record_event(
        "worker_finished",
        key=f"worker_finished:{worker}:2",
        worker_id=worker,
        attempt=2,
        occurred_at=now,
    )
    record_event(
        "worker_started",
        key=f"worker_started:{worker}:1",
        worker_id=worker,
        attempt=1,
        occurred_at=now - timedelta(minutes=2),
    )
    record_event(
        "worker_started",
        key=f"worker_started:{worker}:2",
        worker_id=worker,
        attempt=2,
        occurred_at=now - timedelta(minutes=1),
    )
    events = list(LifecycleEvent.objects.order_by("occurred_at"))
    assert [(event.stage, event.attempt) for event in events] == [
        ("worker_started", 1),
        ("worker_started", 2),
        ("worker_finished", 2),
    ]
    assert events[0].observed_at > events[-1].observed_at


@pytest.mark.django_db
@override_settings(DJANGO_ERGO={"LIFECYCLE_EVENTS_ENABLED": True})
def test_rejects_private_or_invalid_values_without_raising():
    token = uuid4()
    record_event("started", key=f"started:{token}", task_id="private task description")
    record_event("started", key="started:private-text", task_id=token)
    record_event("private transcript", key=f"started:{token}")
    record_event("started", key=f"started:{token}", category="sensitive")
    record_event("started", key=f"started:{token}", attempt=0)
    assert not LifecycleEvent.objects.exists()


@pytest.mark.django_db
@override_settings(DJANGO_ERGO={"LIFECYCLE_EVENTS_ENABLED": True})
def test_database_failure_does_not_break_outer_transaction():
    def broken_write(*args, **kwargs):
        transaction.set_rollback(True)
        msg = "private detail"
        raise DatabaseError(msg)

    with transaction.atomic():
        with patch.object(
            LifecycleEvent.objects, "get_or_create", side_effect=broken_write
        ):
            record_event("started", key=f"started:{uuid4()}")
        assert LifecycleEvent.objects.count() == 0
        record_event("started", key=f"started:{uuid4()}")
        assert LifecycleEvent.objects.count() == 1


@pytest.mark.django_db
@override_settings(DJANGO_ERGO={"LIFECYCLE_EVENTS_ENABLED": True})
def test_retention_is_age_and_batch_bounded():
    keys = [f"started:{uuid4()}" for _ in range(3)]
    old = timezone.now() - timedelta(days=100)
    with patch("django.utils.timezone.now", return_value=old):
        for key in keys[:2]:
            record_event("started", key=key)
    record_event("started", key=keys[2])
    output = StringIO()
    call_command("prune_lifecycle_events", days=90, batch_size=1, stdout=output)
    assert LifecycleEvent.objects.count() == 2
    assert LifecycleEvent.objects.filter(key=keys[2]).exists()
    call_command("prune_lifecycle_events", days=90, batch_size=1, stdout=output)
    assert list(LifecycleEvent.objects.values_list("key", flat=True)) == [keys[2]]
    with pytest.raises(CommandError):
        call_command("prune_lifecycle_events", days=0)
    with pytest.raises(CommandError):
        call_command("prune_lifecycle_events", batch_size=10001)


@pytest.mark.django_db
@override_settings(DJANGO_ERGO={"LIFECYCLE_EVENTS_ENABLED": True})
def test_persisted_observations_cannot_be_updated():
    key = f"started:{uuid4()}"
    record_event("started", key=key)
    event = LifecycleEvent.objects.get(key=key)
    event.stage = "finished"
    with pytest.raises(TypeError, match="immutable"):
        event.save()
    with pytest.raises(TypeError, match="immutable"):
        LifecycleEvent.objects.filter(key=key).update(stage="finished")
    assert LifecycleEvent.objects.get(key=key).stage == "started"
