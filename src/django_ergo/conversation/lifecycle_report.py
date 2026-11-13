"""Content-free, deterministic projections of lifecycle evidence.

A report is an evidence view, not a reconstruction of uninstrumented history.
In particular, task blockers are never promoted to whole-thread inactivity.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC
from datetime import datetime

from django_ergo.conversation.history import as_aware

_FIELDS = (
    "key",
    "stage",
    "occurred_at",
    "observed_at",
    "task_id",
    "thread_id",
    "turn_id",
    "tool_id",
    "worker_id",
    "question_id",
    "message_id",
    "related_thread_id",
    "related_task_id",
    "category",
    "actor",
    "state",
    "attempt",
)


def _value(event, key):
    return event.get(key) if isinstance(event, dict) else getattr(event, key)


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return as_aware(value).astimezone(UTC)


def _seconds(start, end):
    return (end - start).total_seconds() if start and end and end >= start else None


def project_lifecycle(events, *, as_of):  # noqa: C901, PLR0912, PLR0915
    """Project events into JSON-serializable evidence, independent of input order.

    ``as_of`` is required: open intervals must not depend on report execution time.
    Only explicitly surfaced actionable Lee-owned blockers accrue human wait.
    Durations overlap by design and must not be added as a critical path.
    """
    as_of = _time(as_of)
    unique = {}
    for event in events:
        row = {field: _value(event, field) for field in _FIELDS}
        row["occurred_at"] = _time(row["occurred_at"])
        row["observed_at"] = _time(row["observed_at"])
        if row["occurred_at"] > as_of or row["observed_at"] > as_of:
            continue
        # A conflicting duplicate is not resolved by iteration order.
        previous = unique.get(row["key"])
        if previous is not None and previous != row:
            msg = "conflicting lifecycle idempotency key"
            raise ValueError(msg)
        unique[row["key"]] = row
    rows = sorted(
        unique.values(), key=lambda r: (r["occurred_at"], r["observed_at"], r["key"])
    )
    tasks = defaultdict(list)
    questions = defaultdict(list)
    workers = defaultdict(list)
    links = set()
    edges = {}
    for row in rows:
        if row["task_id"]:
            tasks[row["task_id"]].append(row)
        if row["question_id"]:
            questions[(row["task_id"], row["question_id"])].append(row)
        if row["worker_id"]:
            workers[row["worker_id"]].append(row)
        if row["task_id"] and row["related_task_id"]:
            pair = (row["task_id"], row["related_task_id"])
            if row["category"] == "dependency" and row["stage"] in (
                "dependency_added",
                "dependency_removed",
            ):
                edges[pair] = row["stage"] == "dependency_added"
            else:
                links.add(pair)

    blocker_reports = []
    for (task_id, question_id), items in sorted(questions.items()):

        def first(stage, items=items):
            return next((item for item in items if item["stage"] == stage), None)

        created, answer = first("blocker_created"), first("acknowledged")
        resumed, closed = first("resumed"), first("blocker_resolved")
        created_at = created["occurred_at"] if created else None
        answer_at = answer["occurred_at"] if answer else None
        resumed_at = resumed["occurred_at"] if resumed else None
        closed_at = closed["occurred_at"] if closed else None
        ordered = (
            created_at is not None
            and all(
                t is None or t >= created_at for t in (answer_at, resumed_at, closed_at)
            )
            and (not resumed_at or (bool(answer_at) and resumed_at >= answer_at))
            and (not closed_at or not answer_at or closed_at >= answer_at)
        )
        if not ordered:
            status = "unknown"
        elif answer_at and resumed_at:
            status = "answered_resumed"
        elif answer_at and closed_at:
            status = "closed_after_answer_unconsumed"
        elif answer_at:
            status = "answered_unconsumed"
        elif closed_at:
            status = "closed_unanswered"
        else:
            status = "unanswered"
        actionable = bool(
            created
            and created["actor"] == "lee"
            and created["state"] == "actionable_surfaced"
        )
        end = min(
            (t for t in (answer_at, closed_at, as_of) if t is not None), default=as_of
        )
        human_wait = _seconds(created_at, end) if actionable and ordered else None
        system_wait = (
            _seconds(answer_at, resumed_at or as_of)
            if answer_at and ordered and not (closed_at and not resumed_at)
            else None
        )
        blocker_reports.append(
            {
                "task_id": task_id,
                "question_id": question_id,
                "status": status,
                "human_wait_seconds": human_wait,
                "answer_to_resume_seconds": system_wait,
                "human_attribution": "lee" if actionable else "unknown",
                "classification": created["state"] if created else "unknown",
            }
        )

    task_reports = []
    for task_id, items in sorted(tasks.items()):
        blockers = [b for b in blocker_reports if b["task_id"] == task_id]
        task_reports.append(
            {
                "task_id": task_id,
                "thread_ids": sorted({r["thread_id"] for r in items if r["thread_id"]}),
                "blocked": True
                if any(
                    b["status"] in ("unanswered", "answered_unconsumed")
                    for b in blockers
                )
                else None,
                "thread_active": "unknown",
                "first_occurred_at": items[0]["occurred_at"].isoformat(),
                "last_occurred_at": items[-1]["occurred_at"].isoformat(),
                "blockers": blockers,
            }
        )

    worker_reports = []
    for worker_id, items in sorted(workers.items()):
        persisted = next(
            (
                r
                for r in items
                if r["stage"] == "result_persisted" and r["category"] != "handoff"
            ),
            None,
        )
        consumed = next(
            (
                r
                for r in items
                if r["stage"] == "resumed" and r["category"] == "worker_result"
            ),
            None,
        )
        if (
            persisted
            and consumed
            and consumed["occurred_at"] >= persisted["occurred_at"]
        ):
            result_status = "followup_consumed"
        elif persisted and not consumed:
            result_status = "result_unconsumed"
        else:
            result_status = "unknown"
        worker_reports.append({"worker_id": worker_id, "result_status": result_status})

    timeline = [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
        for row in rows
    ]
    return {
        "as_of": as_of.isoformat(),
        "timeline": timeline,
        "tasks": task_reports,
        "workers": worker_reports,
        "related_tasks": [
            {"task_id": a, "related_task_id": b} for a, b in sorted(links)
        ],
        "dependency_edges": [
            {"task_id": a, "related_task_id": b}
            for (a, b), active in sorted(edges.items())
            if active
        ],
        "critical_path_seconds": None,
    }
