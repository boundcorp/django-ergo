from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from django_ergo.conversation.lifecycle_report import project_lifecycle

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def event(  # noqa: PLR0913
    stage,
    second,
    *,
    key=None,
    task="task-a",
    question="",
    worker="",
    category="unknown",
    actor="unknown",
    state="unknown",
    related="",
    observed=None,
    attempt=1,
):
    return {
        "key": key or f"{stage}-{task}-{question}-{second}",
        "stage": stage,
        "occurred_at": BASE + timedelta(seconds=second),
        "observed_at": BASE
        + timedelta(seconds=second if observed is None else observed),
        "task_id": task,
        "thread_id": "thread-a",
        "turn_id": "",
        "tool_id": "",
        "worker_id": worker,
        "question_id": question,
        "message_id": "",
        "related_thread_id": "",
        "related_task_id": related,
        "category": category,
        "actor": actor,
        "state": state,
        "attempt": attempt,
    }


def report(events, seconds=100):
    return project_lifecycle(events, as_of=BASE + timedelta(seconds=seconds))


def test_duplicate_out_of_order_and_late_observations_are_deterministic():
    requested = event("requested", 1, observed=50)
    started = event("started", 2, attempt=2)
    finished = event("finished", 9, related="parent")
    events = [finished, requested, started, requested]
    assert report(events) == report(list(reversed(events)))
    assert [row["stage"] for row in report(events)["timeline"]] == [
        "requested",
        "started",
        "finished",
    ]
    assert [row["stage"] for row in report(events, 20)["timeline"]] == [
        "started",
        "finished",
    ]
    assert report(events)["related_tasks"] == [
        {"task_id": "task-a", "related_task_id": "parent"}
    ]
    assert report(events)["dependency_edges"] == []
    assert report(events)["critical_path_seconds"] is None


def test_conflicting_duplicate_is_not_silently_selected():
    first = event("started", 1, key="same")
    with pytest.raises(ValueError, match="conflicting"):
        report([first, {**first, "state": "ambiguous"}])


def test_only_explicit_surfaced_blocker_is_human_wait_and_overlap_is_not_summed():
    rows = [
        event(
            "blocker_created",
            10,
            question="q1",
            actor="lee",
            state="actionable_surfaced",
        ),
        event("answer_received", 30, question="q1"),
        event("resumed", 55, question="q1"),
        event(
            "blocker_created",
            12,
            task="task-b",
            question="q2",
            actor="unknown",
            state="unknown",
        ),
        event("started", 15, task="task-c"),
        event("finished", 40, task="task-c"),
    ]
    result = report(rows)
    a, b, c = result["tasks"]
    assert a["blockers"][0]["human_wait_seconds"] == 20
    assert a["blockers"][0]["answer_to_resume_seconds"] == 25
    assert b["blockers"][0]["human_wait_seconds"] is None
    assert b["blocked"] is True
    assert all(t["thread_active"] == "unknown" for t in (a, b, c))
    assert result["critical_path_seconds"] is None


def test_answered_unconsumed_and_closure_are_distinct():
    rows = [
        event("blocker_created", 1, question="q1"),
        event("answer_received", 5, question="q1"),
        event("blocker_created", 2, question="q2"),
        event("blocker_resolved", 7, question="q2"),
        event("blocker_created", 2, question="q3"),
        event("answer_received", 5, question="q3"),
        event("blocker_resolved", 7, question="q3"),
        event("resumed", 4, question="missing"),
    ]
    blockers = {b["question_id"]: b for b in report(rows, 20)["tasks"][0]["blockers"]}
    assert blockers["q1"]["status"] == "answered_unconsumed"
    assert blockers["q1"]["answer_to_resume_seconds"] == 15
    assert blockers["q2"]["status"] == "closed_unanswered"
    assert blockers["q3"]["status"] == "closed_after_answer_unconsumed"
    assert blockers["q3"]["answer_to_resume_seconds"] is None
    assert blockers["missing"]["status"] == "unknown"


def test_worker_result_requires_followup_processing_evidence():
    result = event(
        "result_persisted", 10, task="worker-a", worker="worker-a", category="worker"
    )
    consumed = event(
        "resumed", 20, task="worker-a", worker="worker-a", category="worker_result"
    )
    assert report([result])["workers"][0]["result_status"] == "result_unconsumed"
    assert (
        report([consumed, result])["workers"][0]["result_status"] == "followup_consumed"
    )
    assert report([consumed])["workers"][0]["result_status"] == "unknown"
    opaque_tool = event("started", 21, task="worker-a", worker="worker-a")
    assert (
        report([result, consumed, opaque_tool])["workers"][0]["result_status"]
        == "followup_consumed"
    )


def test_explicit_dependencies_and_separate_repeated_approvals():
    rows = [
        event("dependency_added", 1, related="task-b", category="dependency"),
        event("dependency_removed", 9, related="task-b", category="dependency"),
        event("blocker_created", 2, question="approval-1", category="approval"),
        event("answer_received", 3, question="approval-1", category="approval"),
        event("resumed", 4, question="approval-1", category="approval"),
        event("blocker_created", 6, question="approval-2", category="approval"),
    ]
    assert report(rows, 7)["dependency_edges"] == [
        {"task_id": "task-a", "related_task_id": "task-b"}
    ]
    full = report(reversed(rows))
    assert full["dependency_edges"] == []
    assert [b["status"] for b in full["tasks"][0]["blockers"]] == [
        "answered_resumed",
        "unanswered",
    ]


def test_transport_ack_and_other_task_answer_do_not_answer_blocked_question():
    rows = [
        event(
            "blocker_created",
            1,
            question="q1",
            actor="lee",
            state="actionable_surfaced",
        ),
        event("acknowledged", 2, question="q1"),
        event("answer_received", 3, task="task-b", question="q1"),
        event("queued", 4, task="task-b"),
        event("started", 5, task="task-c"),
        event("finished", 10, task="task-c"),
    ]
    tasks = {t["task_id"]: t for t in report(rows, 20)["tasks"]}
    blocker = tasks["task-a"]["blockers"][0]
    assert blocker["status"] == "unanswered"
    assert blocker["human_wait_seconds"] == 19
    assert tasks["task-a"]["blocked"] is True
    assert tasks["task-c"]["thread_active"] == "unknown"
    assert tasks["task-b"]["blockers"][0]["status"] == "unknown"


def test_partial_prerequisites_pause_and_scope_are_not_human_wait():
    rows = [
        event("blocker_created", 1, question="approval", category="approval"),
        event("answer_received", 2, question="approval"),
        event("resumed", 3, question="approval"),
        event("blocker_resolved", 3, question="approval"),
        event("blocker_created", 1, question="credential"),
        event("blocker_created", 1, question="pending-approval", category="approval"),
        event(
            "blocker_created",
            1,
            task="task-b",
            question="pause",
            state="deliberate_pause",
        ),
        event("blocker_created", 1, task="task-c", question="scope", state="new_scope"),
        event("started", 2, task="task-d"),
        event("finished", 9, task="task-d"),
    ]
    tasks = {t["task_id"]: t for t in report(rows)["tasks"]}
    assert tasks["task-a"]["blocked"] is True
    blockers = {b["question_id"]: b for b in tasks["task-a"]["blockers"]}
    assert blockers["approval"]["status"] == "answered_resumed"
    assert blockers["pending-approval"]["status"] == "unanswered"
    assert blockers["pending-approval"]["human_wait_seconds"] is None
    assert blockers["pending-approval"]["pending_age_seconds"] == 99
    assert blockers["credential"]["status"] == "unanswered"
    assert tasks["task-b"]["blockers"][0]["classification"] == "deliberate_pause"
    assert tasks["task-c"]["blockers"][0]["classification"] == "new_scope"
    assert tasks["task-b"]["blockers"][0]["human_wait_seconds"] is None
    assert tasks["task-c"]["blockers"][0]["human_wait_seconds"] is None


def test_answer_in_parent_does_not_answer_distinct_repeated_question():
    rows = [
        event("blocker_created", 1, task="parent", question="old"),
        event("answer_received", 2, task="parent", question="old"),
        event("blocker_created", 3, task="worker", question="new", related="parent"),
        event("acknowledged", 4, task="worker", question="new"),
    ]
    tasks = {t["task_id"]: t for t in report(rows)["tasks"]}
    assert tasks["parent"]["blockers"][0]["status"] == "answered_unconsumed"
    assert tasks["worker"]["blockers"][0]["status"] == "unanswered"
