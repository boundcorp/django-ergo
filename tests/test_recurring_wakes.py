from __future__ import annotations

import threading
from datetime import UTC
from datetime import timedelta
from importlib import reload
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db import close_old_connections
from django.db import connection
from django.db import transaction
from django.utils import timezone

from django_ergo.bots import messaging
from django_ergo.bots import recurring_wakes
from django_ergo.conversation.budget import BudgetExceeded
from django_ergo.conversation.budget import current_meter
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import RecurringWake
from django_ergo.conversation.models import RecurringWakeOccurrence
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import ThreadMessage
from django_ergo.models import LifecycleEvent

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def now():
    return timezone.now().replace(microsecond=0)


@pytest.fixture
def session():
    user = get_user_model().objects.create(username="recurring")
    return ConversationSession.objects.create(
        user=user,
        engine_type="claude",
        transport_type="api",
        status="active",
        bot_name="test-bot",
        metadata={"bot_role": "thread"},
    )


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):
    ids = []
    monkeypatch.setattr(
        messaging, "dispatch", lambda message_id, registry=None: ids.append(message_id)
    )
    monkeypatch.setattr("django_ergo.bots.todos.notify_session", lambda session: None)
    return ids


def create(session, now, **kwargs):
    data = recurring_wakes.create(
        session,
        kwargs.pop("note", "Check the deploy"),
        interval_minutes=kwargs.pop("interval_minutes", 1),
        now=now,
        **({"max_occurrences": 3} | kwargs),
    )
    return RecurringWake.objects.get(pk=data["id"])


def answer(message, now):
    ThreadMessage.objects.filter(pk=message.pk).update(
        status="answered", reply_text="Done", updated_at=now
    )
    recurring_wakes.delivery_finished(message, now=now)


def run_workers(function, count=4):
    barrier = threading.Barrier(count)
    results, errors = [], []

    def run(index):
        try:
            barrier.wait(timeout=10)
            results.append(function(index))
        except Exception as exc:  # noqa: BLE001 — transfer worker failures to pytest
            errors.append(exc)
        finally:
            close_old_connections()

    workers = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert not any(worker.is_alive() for worker in workers)
    assert not errors
    return results


def test_requires_finite_lifetime_and_valid_values(session, now):
    with pytest.raises(ValueError, match="cannot run forever"):
        recurring_wakes.create(session, "Check", interval_minutes=1, now=now)
    for minutes in (True, 0, -1, 0.99, float("inf"), float("nan"), "no"):
        with pytest.raises(ValueError, match="interval"):
            create(session, now, interval_minutes=minutes)
    for maximum in (True, 0, -1, 1.2, "3"):
        with pytest.raises(ValueError, match="max_occurrences"):
            create(session, now, max_occurrences=maximum)
    for expiry in (now, now + timedelta(seconds=60), now.replace(tzinfo=None)):
        with pytest.raises(ValueError, match="expires_at|UTC offset"):
            create(session, now, expires_at=expiry)
    with pytest.raises(ValueError, match="note"):
        create(session, now, note=" ")
    with pytest.raises(ValueError, match="boolean"):
        create(session, now, periodic_checkins="yes")
    assert not RecurringWake.objects.exists()


def test_configurable_minimum_and_interval_rounds_up(session, now, settings):
    settings.DJANGO_ERGO = {"RECURRING_WAKE_MIN_INTERVAL_SECONDS": 120}
    with pytest.raises(ValueError, match="120 seconds"):
        create(session, now, interval_minutes=1)
    schedule = create(session, now, interval_minutes=2.001)
    assert schedule.interval_seconds == 121
    assert schedule.next_run_at == now + timedelta(seconds=121)


def test_database_rejects_unbounded_schedules(session, now):
    with pytest.raises(IntegrityError), transaction.atomic():
        RecurringWake.objects.create(
            session=session,
            bot_name=session.bot_name,
            note="Unbounded",
            interval_seconds=60,
            next_run_at=now,
        )


def test_stable_identity_metadata_and_duplicate_sweeps(session, now, dispatched):
    schedule = create(session, now, periodic_checkins=True)
    assert recurring_wakes.fire_due(now + timedelta(seconds=59)) == []
    due = now + timedelta(minutes=1)
    assert recurring_wakes.fire_due(due) == [str(schedule.pk)]
    assert recurring_wakes.fire_due(due) == []
    assert recurring_wakes.fire_due(now - timedelta(hours=1)) == []
    occurrence = RecurringWakeOccurrence.objects.get(schedule=schedule)
    message = occurrence.thread_message
    assert message.metadata["recurring_wake"] == str(schedule.pk)
    assert message.metadata["recurring_occurrence"] == str(occurrence.pk)
    assert message.metadata["periodic_checkins"] is True
    assert message.metadata["message_author"]["kind"] == "system"
    assert occurrence.scheduled_for == due
    assert occurrence.dispatched_at == due
    assert dispatched == [str(message.pk)]
    assert "not a message from the user" in message.text
    assert message.text.endswith("Your note:\nCheck the deploy")
    assert recurring_wakes.list_schedules(session)[0]["id"] == str(schedule.pk)


def test_max_count_completes_after_final_answer(session, now):
    schedule = create(session, now, max_occurrences=2)
    for index in (1, 2):
        moment = now + timedelta(minutes=index)
        assert recurring_wakes.fire_due(moment) == [str(schedule.pk)]
        occurrence = schedule.scheduled_occurrences.order_by("-scheduled_for").first()
        answer(occurrence.thread_message, moment)
    schedule.refresh_from_db()
    assert schedule.status == "completed"
    assert schedule.occurrences == 2
    assert recurring_wakes.fire_due(now + timedelta(days=1)) == []
    assert schedule.scheduled_occurrences.count() == 2


def test_expiry_never_starts_overdue_work_and_cancels_queued(session, now):
    expiry = now + timedelta(minutes=3)
    schedule = create(session, now, max_occurrences=None, expires_at=expiry)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = schedule.scheduled_occurrences.get()
    assert recurring_wakes.fire_due(expiry) == []
    schedule.refresh_from_db()
    occurrence.refresh_from_db()
    occurrence.thread_message.refresh_from_db()
    assert schedule.status == "completed"
    assert occurrence.status == "cancelled"
    assert occurrence.thread_message.status == "failed"
    assert not recurring_wakes.validate_delivery(
        occurrence.thread_message, now=expiry, claim=True
    )
    assert recurring_wakes.fire_due(expiry + timedelta(days=1)) == []


def test_expiry_without_any_sweep_drops_all_missed_work(session, now):
    schedule = create(
        session, now, max_occurrences=None, expires_at=now + timedelta(minutes=3)
    )
    assert recurring_wakes.fire_due(now + timedelta(hours=2)) == []
    schedule.refresh_from_db()
    assert schedule.status == "completed"
    assert schedule.occurrences == 0
    assert not RecurringWakeOccurrence.objects.exists()


def test_missed_slots_coalesce_and_restart_backward_clock_cannot_replay(session, now):
    schedule = create(session, now)
    late = now + timedelta(minutes=10, seconds=30)
    assert recurring_wakes.fire_due(late) == [str(schedule.pk)]
    occurrence = schedule.scheduled_occurrences.get()
    assert occurrence.scheduled_for == now + timedelta(minutes=10)
    assert occurrence.dispatched_at == late
    schedule.refresh_from_db()
    assert schedule.next_run_at == now + timedelta(minutes=11)
    answer(occurrence.thread_message, late)
    reload(recurring_wakes)
    assert recurring_wakes.fire_due(now + timedelta(minutes=9)) == []
    assert recurring_wakes.fire_due(late) == []
    assert recurring_wakes.fire_due(now + timedelta(minutes=12)) == [str(schedule.pk)]
    assert schedule.scheduled_occurrences.count() == 2


def test_elapsed_interval_crosses_dst_in_utc(session, now):
    local = now.replace(year=2026, month=11, day=1, hour=0, minute=30, second=0)
    local = local.replace(tzinfo=ZoneInfo("America/New_York"))
    schedule = create(session, local, interval_minutes=120)
    next_utc = schedule.next_run_at.replace(tzinfo=UTC)
    assert next_utc - local.astimezone(UTC) == timedelta(hours=2)
    assert next_utc.astimezone(ZoneInfo("America/New_York")).hour == 1


@pytest.mark.parametrize("state", ["in_progress", "awaiting_approval"])
def test_busy_and_approval_coalesce_without_making_occurrences(session, now, state):
    schedule = create(session, now)
    call = StructuredCall.objects.create(
        session=session, kind="chat_reply", status=state
    )
    assert recurring_wakes.fire_due(now + timedelta(minutes=30)) == []
    assert not schedule.scheduled_occurrences.exists()
    schedule.refresh_from_db()
    assert schedule.blocked_reason == (
        "thread_busy" if state == "in_progress" else "awaiting_approval"
    )
    assert schedule.next_run_at == now + timedelta(minutes=31)
    call.status = "completed"
    call.save(update_fields=["status", "updated_at"])
    assert recurring_wakes.fire_due(now + timedelta(minutes=31)) == [str(schedule.pk)]
    assert schedule.scheduled_occurrences.count() == 1


@pytest.mark.parametrize("state", ["queued", "delivered", "waiting"])
def test_one_outstanding_occurrence_coalesces_all_thread_schedules(session, now, state):
    first = create(session, now)
    second = create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = RecurringWakeOccurrence.objects.get()
    ThreadMessage.objects.filter(pk=occurrence.thread_message_id).update(status=state)
    if state != "queued":
        StructuredCall.objects.create(
            session=session,
            kind="chat_reply",
            status="in_progress" if state == "delivered" else "awaiting_approval",
            metadata={"thread_message": str(occurrence.thread_message_id)},
        )
    recurring_wakes.delivery_finished(
        occurrence.thread_message, now=now + timedelta(minutes=1)
    )
    assert recurring_wakes.fire_due(now + timedelta(minutes=50)) == []
    assert RecurringWakeOccurrence.objects.count() == 1
    if state == "waiting":
        assert all(
            item["blocked_reason"] == "awaiting_approval"
            for item in recurring_wakes.list_schedules(session)
        )
    assert {first.pk, second.pk} == set(
        RecurringWake.objects.values_list("pk", flat=True)
    )


def test_whole_thread_rate_limit_survives_fast_answers(session, now):
    first = create(session, now)
    second = create(session, now + timedelta(seconds=20))
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = RecurringWakeOccurrence.objects.get()
    assert occurrence.schedule_id == first.pk
    answer(occurrence.thread_message, now + timedelta(seconds=61))
    assert recurring_wakes.fire_due(now + timedelta(seconds=80)) == []
    second.refresh_from_db()
    assert second.blocked_reason == "thread_rate_limit"
    assert RecurringWakeOccurrence.objects.count() == 1


def test_pause_resume_cancels_queue_and_does_not_catch_up(session, now):
    schedule = create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = schedule.scheduled_occurrences.get()
    paused = recurring_wakes.control(
        session, schedule.pk, "pause", now=now + timedelta(seconds=61)
    )
    assert paused["status"] == "paused"
    occurrence.refresh_from_db()
    assert occurrence.status == "cancelled"
    assert recurring_wakes.fire_due(now + timedelta(hours=1)) == []
    resumed = recurring_wakes.control(
        session, schedule.pk, "resume", now=now + timedelta(hours=1)
    )
    assert resumed["status"] == "active"
    expected = (now + timedelta(hours=1, minutes=1)).replace(tzinfo=UTC)
    assert resumed["next_run_at"] == expected
    assert recurring_wakes.fire_due(now + timedelta(hours=1)) == []
    assert recurring_wakes.fire_due(now + timedelta(hours=1, minutes=1)) == [
        str(schedule.pk)
    ]


def test_resume_during_backward_clock_never_rewinds_next_slot(session, now):
    schedule = create(session, now)
    recurring_wakes.control(session, schedule.pk, "pause", now=now)
    data = recurring_wakes.control(
        session, schedule.pk, "resume", now=now - timedelta(hours=1)
    )
    assert data["next_run_at"] == schedule.next_run_at.replace(tzinfo=UTC)


def test_control_is_owned_by_exact_session_and_terminal_cancel_is_final(session, now):
    schedule = create(session, now)
    other = ConversationSession.objects.create(user=session.user, status="active")
    for identity in (schedule.pk, uuid4(), "invalid"):
        with pytest.raises(LookupError):
            recurring_wakes.control(other, identity, "cancel", now=now)
    with pytest.raises(ValueError, match="action"):
        recurring_wakes.control(session, schedule.pk, "delete", now=now)
    cancelled = recurring_wakes.control(session, schedule.pk, "cancel", now=now)
    assert cancelled["status"] == "cancelled"
    assert recurring_wakes.control(session, schedule.pk, "cancel", now=now) == cancelled
    with pytest.raises(ValueError, match="cancelled"):
        recurring_wakes.control(session, schedule.pk, "resume", now=now)
    assert recurring_wakes.fire_due(now + timedelta(days=1)) == []


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_control_after_claim_prevents_start_before_bot_ask(session, now, action):
    schedule = create(session, now)
    due = now + timedelta(minutes=1)
    recurring_wakes.fire_due(due)
    message = schedule.scheduled_occurrences.get().thread_message
    assert recurring_wakes.validate_delivery(message, now=due, claim=True)
    recurring_wakes.control(session, schedule.pk, action, now=due)
    assert not recurring_wakes.validate_delivery(message, now=due)
    message.refresh_from_db()
    assert message.status == "failed"


def test_archive_cancels_and_reopen_does_not_revive(session, now):
    schedule = create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    session.status = "completed"
    session.metadata["archived_at"] = now.isoformat()
    session.save(update_fields=["status", "metadata"])
    assert recurring_wakes.cancel_session(session, now=now + timedelta(minutes=1)) == 1
    session.status = "active"
    session.metadata = {"bot_role": "thread"}
    session.save(update_fields=["status", "metadata"])
    assert recurring_wakes.fire_due(now + timedelta(hours=1)) == []
    schedule.refresh_from_db()
    assert schedule.status == "cancelled"
    assert schedule.scheduled_occurrences.get().status == "cancelled"


def test_archived_create_is_refused_and_tick_cancels_stale_schedule(session, now):
    schedule = create(session, now)
    session.status = "completed"
    session.save(update_fields=["status"])
    with pytest.raises(ValueError, match="archived"):
        create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    schedule.refresh_from_db()
    assert schedule.status == "cancelled"
    assert not ThreadMessage.objects.exists()


def test_deleted_thread_cascades_schedules_occurrences_and_messages(session, now):
    create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    session.delete()
    assert not RecurringWake.objects.exists()
    assert not RecurringWakeOccurrence.objects.exists()
    assert not ThreadMessage.objects.exists()


def test_failed_turn_blocks_until_owner_resume_without_retry(session, now):
    schedule = create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = schedule.scheduled_occurrences.get()
    ThreadMessage.objects.filter(pk=occurrence.thread_message_id).update(
        status="failed", error="Stopped by the request budget"
    )
    recurring_wakes.delivery_failed(
        occurrence.thread_message, "Stopped by the request budget"
    )
    schedule.refresh_from_db()
    occurrence.refresh_from_db()
    assert schedule.status == "blocked"
    assert schedule.blocked_reason == "Stopped by the request budget"
    assert occurrence.status == "failed"
    assert recurring_wakes.fire_due(now + timedelta(hours=1)) == []
    assert schedule.scheduled_occurrences.count() == 1
    resumed = recurring_wakes.control(
        session, schedule.pk, "resume", now=now + timedelta(hours=1)
    )
    assert resumed["blocked_reason"] == ""
    recurring_wakes.fire_due(now + timedelta(hours=1, minutes=1))
    assert schedule.scheduled_occurrences.count() == 2


def test_crash_reconciliation_observes_failed_or_answered_message(session, now):
    schedule = create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = schedule.scheduled_occurrences.get()
    ThreadMessage.objects.filter(pk=occurrence.thread_message_id).update(
        status="failed", error="Delivery failed"
    )
    # No callback happened before the process stopped.
    reload(recurring_wakes)
    assert recurring_wakes.fire_due(now + timedelta(minutes=2)) == []
    schedule.refresh_from_db()
    assert schedule.status == "blocked"
    assert schedule.blocked_reason == "Delivery failed"
    assert schedule.scheduled_occurrences.count() == 1


def test_send_failure_rolls_back_message_and_blocks_schedule(session, now, monkeypatch):
    schedule = create(session, now)
    original = messaging.send_event

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        msg = "Queue unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(messaging, "send_event", broken)
    assert recurring_wakes.fire_due(now + timedelta(minutes=1)) == []
    schedule.refresh_from_db()
    assert schedule.status == "blocked"
    assert schedule.blocked_reason == "Queue unavailable"
    assert not ThreadMessage.objects.exists()
    assert not RecurringWakeOccurrence.objects.exists()
    assert recurring_wakes.fire_due(now + timedelta(days=1)) == []


def test_quotas_include_paused_blocked_and_release_only_terminal(
    session, now, settings
):
    settings.DJANGO_ERGO = {
        "RECURRING_WAKE_MAX_PER_THREAD": 2,
        "RECURRING_WAKE_MAX_GLOBAL": 3,
    }
    first = create(session, now)
    recurring_wakes.control(session, first.pk, "pause", now=now)
    second = create(session, now)
    RecurringWake.objects.filter(pk=second.pk).update(
        status="blocked", blocked_reason="failed"
    )
    with pytest.raises(ValueError, match="thread.*quota"):
        create(session, now)
    other = ConversationSession.objects.create(user=session.user, status="active")
    create(other, now)
    with pytest.raises(ValueError, match="global.*quota"):
        create(other, now)
    recurring_wakes.control(session, first.pk, "cancel", now=now)
    assert create(session, now).status == "active"


def test_expired_paused_and_blocked_schedules_are_reaped(session, now):
    first = create(session, now, expires_at=now + timedelta(minutes=2))
    second = create(session, now, expires_at=now + timedelta(minutes=2))
    recurring_wakes.control(session, first.pk, "pause", now=now)
    RecurringWake.objects.filter(pk=second.pk).update(status="blocked")
    assert recurring_wakes.fire_due(now + timedelta(minutes=3)) == []
    assert set(RecurringWake.objects.values_list("status", flat=True)) == {"completed"}


def test_paused_rows_cannot_starve_due_active_batch(session, now, settings):
    settings.DJANGO_ERGO = {
        "RECURRING_WAKE_MAX_PER_THREAD": 200,
        "RECURRING_WAKE_MAX_GLOBAL": 200,
    }
    for index in range(recurring_wakes.FIRE_BATCH):
        paused = create(session, now, note=f"paused {index}")
        recurring_wakes.control(session, paused.pk, "pause", now=now)
    active = create(session, now + timedelta(seconds=1))
    assert recurring_wakes.fire_due(now + timedelta(minutes=2)) == [str(active.pk)]


def test_budget_forbids_create_and_resume(session, now):
    schedule = create(session, now)
    recurring_wakes.control(session, schedule.pk, "pause", now=now)
    meter = SimpleNamespace(violations=[])
    token = current_meter.set(meter)
    try:
        with pytest.raises(BudgetExceeded):
            create(session, now)
        with pytest.raises(BudgetExceeded):
            recurring_wakes.control(session, schedule.pk, "resume", now=now)
    finally:
        current_meter.reset(token)
    assert meter.violations == ["schedule recurring wakes", "resume recurring wakes"]
    assert RecurringWake.objects.count() == 1


def test_depth_policy_is_preserved_and_recurring_turn_cannot_extend_lifetime(
    session, now
):
    inbound = ThreadMessage.objects.create(
        recipient_session=session,
        text="Restricted request",
        depth=messaging.MAX_DEPTH,
        metadata={"tool_execution_policy": "read_only", "model_retry_policy": "never"},
    )
    call = StructuredCall.objects.create(
        session=session, kind="chat_reply", metadata={"thread_message": str(inbound.pk)}
    )
    schedule = create(session, now)
    call.delete()
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = schedule.scheduled_occurrences.get()
    assert occurrence.thread_message.depth == messaging.MAX_DEPTH
    assert occurrence.thread_message.metadata["tool_execution_policy"] == "read_only"
    assert occurrence.thread_message.metadata["model_retry_policy"] == "never"
    StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        metadata={"thread_message": str(occurrence.thread_message_id)},
    )
    with pytest.raises(ValueError, match="cannot create another"):
        create(session, now)


def test_stored_budget_cannot_escape_through_contextless_create(session, now):
    inbound = ThreadMessage.objects.create(
        recipient_session=session, text="Budgeted", metadata={"turn_budget": {}}
    )
    StructuredCall.objects.create(
        session=session, kind="chat_reply", metadata={"thread_message": str(inbound.pk)}
    )
    with pytest.raises(ValueError, match="budgeted"):
        create(session, now)


def test_lifecycle_opt_in_is_content_free_and_records_slot_separately(
    session, now, settings
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    schedule = create(session, now, note="SECRET PRIVATE CONTENT")
    late = now + timedelta(minutes=8, seconds=20)
    recurring_wakes.fire_due(late)
    occurrence = schedule.scheduled_occurrences.get()
    requested = LifecycleEvent.objects.get(key=f"requested:{occurrence.pk}")
    queued = LifecycleEvent.objects.get(key=f"queued:{occurrence.pk}")
    admission = LifecycleEvent.objects.get(key=f"requested:{schedule.pk}")
    assert admission.occurred_at == now
    assert occurrence.scheduled_for < requested.occurred_at == queued.occurred_at
    assert queued.occurred_at == occurrence.dispatched_at == late
    assert requested.task_id == str(schedule.pk)
    assert requested.message_id == str(occurrence.thread_message_id)
    assert "SECRET" not in repr(list(LifecycleEvent.objects.values()))


def test_lifecycle_is_disabled_by_default(session, now):
    create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    assert not LifecycleEvent.objects.exists()


def test_nonrecurring_delivery_hooks_are_noops(session, now):
    message = ThreadMessage.objects.create(recipient_session=session, text="Ordinary")
    assert recurring_wakes.validate_delivery(message, now=now)
    recurring_wakes.delivery_finished(message, now=now)
    recurring_wakes.delivery_failed(message, "Unrelated failure")
    message.refresh_from_db()
    assert message.status == "queued"


def test_concurrent_postgres_ticks_create_one_occurrence_and_message(session, now):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    schedule = create(session, now)
    results = run_workers(
        lambda _: recurring_wakes.fire_due(now + timedelta(minutes=1))
    )
    assert sum(bool(result) for result in results) == 1
    assert RecurringWakeOccurrence.objects.count() == 1
    assert ThreadMessage.objects.filter(recipient_session=session).count() == 1
    schedule.refresh_from_db()
    assert schedule.occurrences == 1


@pytest.mark.parametrize("global_quota", [False, True])
def test_concurrent_postgres_admission_serializes_empty_table_quotas(
    session, now, settings, global_quota
):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    settings.DJANGO_ERGO = {
        "RECURRING_WAKE_MAX_PER_THREAD": 4 if global_quota else 1,
        "RECURRING_WAKE_MAX_GLOBAL": 1 if global_quota else 4,
    }
    other = ConversationSession.objects.create(user=session.user, status="active")

    def admit(index):
        target = other if global_quota and index % 2 else session
        try:
            return create(target, now).pk
        except ValueError as exc:
            return exc

    results = run_workers(admit)
    failures = [result for result in results if isinstance(result, ValueError)]
    assert all("quota" in str(error) for error in failures)
    assert (
        sum(
            result is not None and not isinstance(result, ValueError)
            for result in results
        )
        == 1
    )
    assert RecurringWake.objects.count() == 1


def test_concurrent_postgres_delivery_claims_only_one_worker(session, now):
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    schedule = create(session, now)
    due = now + timedelta(minutes=1)
    recurring_wakes.fire_due(due)
    message_id = schedule.scheduled_occurrences.get().thread_message_id

    def claim(_):
        message = ThreadMessage.objects.get(pk=message_id)
        return recurring_wakes.validate_delivery(message, now=due, claim=True)

    assert sum(run_workers(claim)) == 1
    message = ThreadMessage.objects.get(pk=message_id)
    assert message.status == "delivered"
    assert recurring_wakes.validate_delivery(message, now=due)
    assert not recurring_wakes.validate_delivery(message, now=due, claim=True)


def test_occurrence_unique_slot_and_outstanding_database_constraints(session, now):
    schedule = create(session, now)
    recurring_wakes.fire_due(now + timedelta(minutes=1))
    occurrence = schedule.scheduled_occurrences.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        RecurringWakeOccurrence.objects.create(
            schedule=schedule, scheduled_for=occurrence.scheduled_for, dispatched_at=now
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        RecurringWakeOccurrence.objects.create(
            schedule=schedule,
            scheduled_for=occurrence.scheduled_for + timedelta(minutes=1),
            dispatched_at=now,
        )


@pytest.mark.parametrize("terminal", ["cancelled", "failed", "missing"])
def test_terminal_occurrence_events_are_opt_in_and_content_free(
    session, now, settings, terminal
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    schedule = create(session, now)
    due = now + timedelta(minutes=1)
    recurring_wakes.fire_due(due)
    occurrence = schedule.scheduled_occurrences.get()
    if terminal == "cancelled":
        recurring_wakes.control(session, schedule.pk, "cancel", now=due)
    elif terminal == "failed":
        recurring_wakes.delivery_failed(occurrence.thread_message, "PRIVATE EXCEPTION")
    else:
        occurrence.thread_message.delete()
        recurring_wakes.fire_due(now + timedelta(minutes=2))
    event = LifecycleEvent.objects.get(key=f"finished:{occurrence.pk}")
    assert event.state == ("cancelled" if terminal == "cancelled" else "failed")
    assert event.task_id == str(schedule.pk)
    assert event.related_task_id == str(occurrence.pk)
    assert "PRIVATE" not in repr(list(LifecycleEvent.objects.values()))


def test_missing_occurrence_blocks_delivery_without_unbudgeted_fallback(session, now):
    schedule = create(session, now)
    due = now + timedelta(minutes=1)
    recurring_wakes.fire_due(due)
    occurrence = schedule.scheduled_occurrences.get()
    message = occurrence.thread_message
    occurrence.delete()
    assert not recurring_wakes.validate_delivery(message, now=due, claim=True)
    schedule.refresh_from_db()
    message.refresh_from_db()
    assert schedule.status == "blocked"
    assert schedule.blocked_reason == "Recurring wake occurrence missing"
    assert message.status == "failed"
    assert recurring_wakes.fire_due(now + timedelta(hours=1)) == []


def test_expiry_is_processed_even_when_next_slot_is_in_the_future(session, now):
    schedule = create(session, now, expires_at=now + timedelta(minutes=3))
    recurring_wakes.control(session, schedule.pk, "pause", now=now)
    RecurringWake.objects.filter(pk=schedule.pk).update(
        next_run_at=now + timedelta(days=1)
    )
    recurring_wakes.fire_due(now + timedelta(minutes=4))
    schedule.refresh_from_db()
    assert schedule.status == "completed"


def test_first_finite_limit_wins_when_expiry_and_count_are_both_given(session, now):
    schedule = create(
        session, now, max_occurrences=1, expires_at=now + timedelta(hours=1)
    )
    due = now + timedelta(minutes=1)
    recurring_wakes.fire_due(due)
    answer(schedule.scheduled_occurrences.get().thread_message, due)
    schedule.refresh_from_db()
    assert schedule.status == "completed"
    assert recurring_wakes.fire_due(now + timedelta(minutes=2)) == []


def test_without_django_timezone_support_values_remain_explicit_utc(
    session, now, settings
):
    settings.USE_TZ = False
    schedule = create(session, now)
    data = recurring_wakes.list_schedules(session)[0]
    assert data["next_run_at"].utcoffset() == timedelta(0)
    assert data["next_run_at"] == (now + timedelta(minutes=1)).replace(tzinfo=UTC)
    assert recurring_wakes.fire_due(now + timedelta(minutes=1)) == [str(schedule.pk)]
    occurrence = schedule.scheduled_occurrences.get()
    assert occurrence.scheduled_for == (now + timedelta(minutes=1)).replace(tzinfo=None)


def test_claim_start_grace_is_bounded_and_unknown_outcome_is_not_retried(session, now):
    schedule = create(session, now - timedelta(minutes=2))
    recurring_wakes.fire_due(now)
    occurrence = schedule.scheduled_occurrences.get()
    message = occurrence.thread_message
    assert recurring_wakes.validate_delivery(message, now=now, claim=True)
    ThreadMessage.objects.filter(pk=message.pk).update(updated_at=now)
    grace = recurring_wakes.CLAIM_START_GRACE_SECONDS
    assert recurring_wakes.fire_due(now + timedelta(seconds=grace - 1)) == []
    occurrence.refresh_from_db()
    assert occurrence.status == "delivered"
    assert recurring_wakes.fire_due(now + timedelta(seconds=grace + 1)) == []
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    message.refresh_from_db()
    assert (occurrence.status, message.status, schedule.status) == (
        "failed",
        "failed",
        "blocked",
    )
    assert "Unknown outcome" in occurrence.error
    assert schedule.occurrences == 1
    assert recurring_wakes.fire_due(now + timedelta(days=1)) == []
    assert schedule.scheduled_occurrences.count() == 1


@pytest.mark.parametrize(
    "state", ["in_progress", "failed", "completed", "superseded_approval"]
)
def test_abandoned_matching_turns_and_superseded_approvals_are_retired(
    session, now, state
):
    schedule = create(session, now - timedelta(minutes=2))
    recurring_wakes.fire_due(now)
    occurrence = schedule.scheduled_occurrences.get()
    message = occurrence.thread_message
    assert recurring_wakes.validate_delivery(message, now=now, claim=True)
    stale = now - timedelta(seconds=messaging.STALE_TURN_SECONDS + 60)
    message_state = "waiting" if state == "superseded_approval" else "delivered"
    ThreadMessage.objects.filter(pk=message.pk).update(
        status=message_state, updated_at=stale
    )
    call = StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        status="awaiting_approval" if state == "superseded_approval" else state,
        metadata={"thread_message": str(message.pk)},
    )
    StructuredCall.objects.filter(pk=call.pk).update(created_at=stale, updated_at=stale)
    if state == "superseded_approval":
        StructuredCall.objects.create(
            session=session, kind="chat_reply", status="completed"
        )
    assert recurring_wakes.fire_due(now) == []
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    assert occurrence.status == "failed"
    assert occurrence.completed_at is not None
    assert schedule.status == "blocked"
    assert "Unknown outcome" in schedule.blocked_reason
    assert schedule.scheduled_occurrences.count() == 1


@pytest.mark.parametrize("state", ["in_progress", "awaiting_approval"])
def test_matching_live_turn_or_actionable_approval_survives_old_claim(
    session, now, state
):
    schedule = create(session, now - timedelta(minutes=2))
    recurring_wakes.fire_due(now)
    occurrence = schedule.scheduled_occurrences.get()
    message = occurrence.thread_message
    assert recurring_wakes.validate_delivery(message, now=now, claim=True)
    ThreadMessage.objects.filter(pk=message.pk).update(
        status="waiting" if state == "awaiting_approval" else "delivered",
        updated_at=now - timedelta(days=1),
    )
    StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        status=state,
        metadata={"thread_message": str(message.pk)},
    )
    assert recurring_wakes.fire_due(now) == []
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    assert occurrence.status == (
        "waiting" if state == "awaiting_approval" else "delivered"
    )
    assert schedule.status == "active"
    assert schedule.scheduled_occurrences.count() == 1


def test_recent_completed_call_gets_result_handoff_grace(session, now):
    schedule = create(session, now - timedelta(minutes=2), max_occurrences=1)
    recurring_wakes.fire_due(now)
    occurrence = schedule.scheduled_occurrences.get()
    message = occurrence.thread_message
    assert recurring_wakes.validate_delivery(message, now=now, claim=True)
    ThreadMessage.objects.filter(pk=message.pk).update(
        updated_at=now - timedelta(minutes=5)
    )
    StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        status="completed",
        metadata={"thread_message": str(message.pk)},
    )
    assert recurring_wakes.fire_due(now + timedelta(seconds=10)) == []
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    assert (occurrence.status, schedule.status) == ("delivered", "active")
    answer(message, now + timedelta(seconds=20))
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    assert (occurrence.status, schedule.status) == ("answered", "completed")


@pytest.mark.parametrize("boundary", ["cancel", "expire"])
def test_terminal_schedule_releases_abandoned_claim_for_other_thread_schedule(
    session, now, boundary
):
    first = create(
        session,
        now - timedelta(minutes=2),
        expires_at=now + timedelta(minutes=2),
    )
    second = create(session, now - timedelta(minutes=1))
    recurring_wakes.fire_due(now)
    occurrence = RecurringWakeOccurrence.objects.get()
    assert occurrence.schedule_id == first.pk
    assert recurring_wakes.validate_delivery(
        occurrence.thread_message, now=now, claim=True
    )
    ThreadMessage.objects.filter(pk=occurrence.thread_message_id).update(
        updated_at=now - timedelta(minutes=5)
    )
    if boundary == "cancel":
        recurring_wakes.control(session, first.pk, "cancel", now=now)
    assert recurring_wakes.fire_due(now + timedelta(minutes=2)) == [str(second.pk)]
    first.refresh_from_db()
    occurrence.refresh_from_db()
    assert first.status == ("cancelled" if boundary == "cancel" else "completed")
    assert occurrence.status == "failed"
    assert "Unknown outcome" in occurrence.error
    assert first.scheduled_occurrences.count() == 1


def test_explicit_resume_retires_stale_claim_but_preserves_count_expiry_and_future_slot(
    session, now
):
    expiry = now + timedelta(hours=1)
    schedule = create(session, now - timedelta(minutes=2), expires_at=expiry)
    recurring_wakes.fire_due(now)
    occurrence = schedule.scheduled_occurrences.get()
    assert recurring_wakes.validate_delivery(
        occurrence.thread_message, now=now, claim=True
    )
    ThreadMessage.objects.filter(pk=occurrence.thread_message_id).update(
        updated_at=now - timedelta(minutes=5)
    )
    resumed = recurring_wakes.control(session, schedule.pk, "resume", now=now)
    occurrence.refresh_from_db()
    assert occurrence.status == "failed"
    assert resumed["status"] == "active"
    assert resumed["occurrences"] == 1
    assert resumed["expires_at"] == expiry.replace(tzinfo=UTC)
    assert resumed["next_run_at"] == (now + timedelta(minutes=1)).replace(tzinfo=UTC)
    assert recurring_wakes.fire_due(now) == []
    assert recurring_wakes.fire_due(now + timedelta(minutes=1)) == [str(schedule.pk)]
    assert schedule.scheduled_occurrences.count() == 2


def test_same_interval_schedules_rotate_service_without_exceeding_thread_rate(
    session, now
):
    expiry = now + timedelta(minutes=10)
    first = create(session, now, max_occurrences=None, expires_at=expiry)
    second = create(session, now, max_occurrences=None, expires_at=expiry)
    served = []
    for minute in range(1, 5):
        moment = now + timedelta(minutes=minute)
        fired = recurring_wakes.fire_due(moment)
        assert len(fired) == 1
        served.extend(fired)
        occurrence = RecurringWakeOccurrence.objects.get(dispatched_at=moment)
        answer(occurrence.thread_message, moment)
    assert set(served[:2]) == {str(first.pk), str(second.pk)}
    assert served[0] == served[2] and served[1] == served[3]
    assert first.scheduled_occurrences.count() == 2
    assert second.scheduled_occurrences.count() == 2


@pytest.mark.parametrize("state", ["waiting", "answered"])
def test_full_exhausted_lifecycle_batch_cannot_starve_new_thread(
    session, now, settings, state
):
    size = recurring_wakes.FIRE_BATCH
    settings.DJANGO_ERGO = {"RECURRING_WAKE_MAX_GLOBAL": size + 10}
    sessions = ConversationSession.objects.bulk_create(
        [
            ConversationSession(user=session.user, status="active", bot_name="test-bot")
            for _ in range(size)
        ]
    )
    schedules = RecurringWake.objects.bulk_create(
        [
            RecurringWake(
                session=thread,
                bot_name="test-bot",
                note="A previously dispatched finite schedule",
                interval_seconds=60,
                next_run_at=now - timedelta(minutes=1),
                max_occurrences=1,
                occurrences=1,
            )
            for thread in sessions
        ]
    )
    messages = ThreadMessage.objects.bulk_create(
        [
            ThreadMessage(
                recipient_session=thread,
                text="Already dispatched",
                status=state,
                metadata={"recurring_wake": str(schedule.pk)},
            )
            for thread, schedule in zip(sessions, schedules, strict=True)
        ]
    )
    RecurringWakeOccurrence.objects.bulk_create(
        [
            RecurringWakeOccurrence(
                schedule=schedule,
                thread_message=message,
                status=state,
                scheduled_for=now - timedelta(minutes=2),
                dispatched_at=now - timedelta(minutes=2),
            )
            for schedule, message in zip(schedules, messages, strict=True)
        ]
    )
    if state == "waiting":
        StructuredCall.objects.bulk_create(
            [
                StructuredCall(
                    session=thread,
                    kind="chat_reply",
                    status="awaiting_approval",
                    metadata={"thread_message": str(message.pk)},
                )
                for thread, message in zip(sessions, messages, strict=True)
            ]
        )
    fresh = create(session, now - timedelta(minutes=1))
    assert recurring_wakes.fire_due(now) == [str(fresh.pk)]
    assert fresh.scheduled_occurrences.count() == 1
    if state == "answered":
        assert (
            RecurringWake.objects.filter(
                pk__in=[schedule.pk for schedule in schedules], status="completed"
            ).count()
            == size
        )
    else:
        assert RecurringWakeOccurrence.objects.filter(status="waiting").count() == size


@pytest.mark.parametrize("completed_call", [False, True])
def test_fresh_claim_and_completed_handoff_use_django_local_source_time(
    session, now, settings, completed_call
):
    settings.USE_TZ = False
    settings.TIME_ZONE = "America/New_York"
    utc_now = now.replace(tzinfo=UTC)
    local_now = timezone.make_naive(utc_now, ZoneInfo(settings.TIME_ZONE))
    schedule = create(session, utc_now - timedelta(minutes=2))
    recurring_wakes.fire_due(utc_now)
    occurrence = schedule.scheduled_occurrences.get()
    message = occurrence.thread_message
    assert recurring_wakes.validate_delivery(message, now=utc_now, claim=True)
    ThreadMessage.objects.filter(pk=message.pk).update(
        updated_at=local_now - timedelta(minutes=5) if completed_call else local_now
    )
    if completed_call:
        call = StructuredCall.objects.create(
            session=session,
            kind="chat_reply",
            status="completed",
            metadata={"thread_message": str(message.pk)},
        )
        StructuredCall.objects.filter(pk=call.pk).update(updated_at=local_now)
    assert recurring_wakes.fire_due(utc_now + timedelta(seconds=30)) == []
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    assert (occurrence.status, schedule.status) == ("delivered", "active")
    assert recurring_wakes.fire_due(utc_now + timedelta(seconds=61)) == []
    occurrence.refresh_from_db()
    schedule.refresh_from_db()
    assert (occurrence.status, schedule.status) == ("failed", "blocked")


def test_observed_lifecycle_times_convert_scheduler_utc_to_django_local_storage(
    session, now, settings
):
    settings.USE_TZ = False
    settings.TIME_ZONE = "America/New_York"
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    utc_now = now.replace(tzinfo=UTC)
    local_now = timezone.make_naive(utc_now, ZoneInfo(settings.TIME_ZONE))
    schedule = create(session, utc_now - timedelta(minutes=2))
    recurring_wakes.fire_due(utc_now)
    occurrence = schedule.scheduled_occurrences.get()
    assert LifecycleEvent.objects.get(
        key=f"requested:{schedule.pk}"
    ).occurred_at == local_now - timedelta(minutes=2)
    assert (
        LifecycleEvent.objects.get(key=f"requested:{occurrence.pk}").occurred_at
        == local_now
    )
    assert (
        LifecycleEvent.objects.get(key=f"queued:{occurrence.pk}").occurred_at
        == local_now
    )
    recurring_wakes.control(
        session, schedule.pk, "cancel", now=utc_now + timedelta(seconds=30)
    )
    assert LifecycleEvent.objects.get(
        key=f"finished:{occurrence.pk}"
    ).occurred_at == local_now + timedelta(seconds=30)


def test_resume_of_last_abandoned_occurrence_retires_it_without_replaying(session, now):
    schedule = create(session, now - timedelta(minutes=2), max_occurrences=1)
    recurring_wakes.fire_due(now)
    occurrence = schedule.scheduled_occurrences.get()
    assert recurring_wakes.validate_delivery(
        occurrence.thread_message, now=now, claim=True
    )
    ThreadMessage.objects.filter(pk=occurrence.thread_message_id).update(
        updated_at=now - timedelta(minutes=5)
    )
    resumed = recurring_wakes.control(session, schedule.pk, "resume", now=now)
    occurrence.refresh_from_db()
    assert resumed["status"] == "completed"
    assert resumed["occurrences"] == 1
    assert occurrence.status == "failed"
    assert recurring_wakes.fire_due(now + timedelta(hours=1)) == []
    assert schedule.scheduled_occurrences.count() == 1


@pytest.mark.parametrize("marker", ["recurring_wake", "recurring_origin"])
def test_direct_or_inherited_recurring_ancestry_cannot_create_a_schedule(
    session, now, marker
):
    inbound = ThreadMessage.objects.create(
        recipient_session=session,
        text="A recurring turn or its delegated result",
        metadata={marker: str(uuid4())},
    )
    StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        metadata={"thread_message": str(inbound.pk)},
    )
    with pytest.raises(ValueError, match="cannot create another"):
        create(session, now)
    assert not RecurringWake.objects.exists()


@pytest.mark.parametrize("marker", ["recurring_wake", "recurring_origin"])
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_worker_update_and_result_keep_durable_recurring_ancestry_without_quiet_policy(
    session, now, monkeypatch, marker, status
):
    from django_ergo.bots import workers

    origin = create(session, now)
    origin_id = str(origin.pk)
    inbound = ThreadMessage.objects.create(
        recipient_session=session,
        text="Inspect the deployment",
        metadata={marker: origin_id},
    )
    call = StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        metadata={"thread_message": str(inbound.pk)},
    )
    monkeypatch.setattr(workers, "_publish", lambda worker_id, delay: None)
    bot = SimpleNamespace(
        name=session.bot_name, registry=None, tasks={"inspect": lambda: "Done"}
    )
    worker = workers.start(bot, session, "inspect", {})
    assert str(worker.recurring_origin) == origin_id
    call.delete()
    origin.delete()
    worker.refresh_from_db()
    assert str(worker.recurring_origin) == origin_id
    context = workers.WorkerContext(bot, worker)
    context.tell("An intermediate worker update")
    workers._finish(
        worker,
        status,
        "An actual result" if status == "completed" else None,
        "" if status == "completed" else "The worker failed",
    )
    messages = list(ThreadMessage.objects.filter(metadata__worker=str(worker.pk)))
    assert len(messages) == 2
    assert any(message.metadata.get("update") for message in messages)
    assert any(message.metadata.get("worker_finished") for message in messages)
    for message in messages:
        assert message.metadata["recurring_origin"] == origin_id
        assert (
            not {"recurring_wake", "recurring_occurrence", "periodic_checkins"}
            & message.metadata.keys()
        )
        handling = StructuredCall.objects.create(
            session=session,
            kind="chat_reply",
            metadata={"thread_message": str(message.pk)},
        )
        with pytest.raises(ValueError, match="cannot create another"):
            create(session, now)
        handling.delete()


def test_ordinary_worker_result_does_not_acquire_a_recurring_restriction(
    session, now, monkeypatch
):
    from django_ergo.bots import workers

    monkeypatch.setattr(workers, "_publish", lambda worker_id, delay: None)
    bot = SimpleNamespace(
        name=session.bot_name, registry=None, tasks={"inspect": lambda: "Done"}
    )
    worker = workers.start(bot, session, "inspect", {})
    assert worker.recurring_origin is None
    workers._finish(worker, "completed", "Done", "")
    message = ThreadMessage.objects.get(metadata__worker=str(worker.pk))
    assert "recurring_origin" not in message.metadata
    StructuredCall.objects.create(
        session=session,
        kind="chat_reply",
        metadata={"thread_message": str(message.pk)},
    )
    assert create(session, now).status == "active"
