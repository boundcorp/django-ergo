import json
import os
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from django_ergo.plugins.runpod.budget import Evidence
from django_ergo.plugins.runpod.budget import RentalTimeline
from django_ergo.plugins.runpod.experiment import Experiment
from django_ergo.plugins.runpod.queue import Queue


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        msg = "network forbidden"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def reserve(e, now=0, **overrides):
    args = {
        "now": now,
        "idle_grace": 10,
        "eligibility": dict.fromkeys(
            ("model", "hardware", "license", "inputs", "budget"), True
        ),
        "timeline": RentalTimeline(60, 10, 10, 10, 10, 10, 10),
        "compute_hourly_usd": Evidence(Decimal("1"), True),
        "storage_usd": Evidence(Decimal("0.10"), True),
        "network_usd": Evidence(Decimal("0.10"), True),
        "fees_usd": Evidence(Decimal("0.10"), True),
        "project_cap_usd": Decimal("3"),
        "project_committed_usd": Decimal("0"),
    }
    args.update(overrides)
    return e.reserve(**args)


def created(e, now=0):
    reserve(e, now)
    token = e.begin_create(now=now)
    rid = e.fake_create(token, now=now)
    e.receipt(rid)
    return rid


def resource(path, rid):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT compute, storage FROM fake_resources WHERE resource_id=?", (rid,)
        ).fetchone()


def test_fail_closed_budget_eligibility_and_singleton(tmp_path):
    e = Experiment(tmp_path / "db")
    with pytest.raises(ValueError, match="eligibility"):
        reserve(e, eligibility={"model": True})
    with pytest.raises(ValueError, match="pricing"):
        reserve(e, fees_usd=Evidence(None, False))
    with pytest.raises(ValueError, match="authorization"):
        reserve(e, storage_usd=Evidence(Decimal("3"), True))
    with pytest.raises(ValueError, match="timeline"):
        reserve(e, timeline=RentalTimeline(3601, 0, 0, 0, 0, 0, 0))
    state = reserve(e)
    assert state["deadline"] == 60 and state["reservation"] == "0.32"
    with pytest.raises(ValueError, match="singleton"):
        reserve(Experiment(e.path))


def test_crash_before_send_never_replays_and_remains_due(tmp_path):
    path = tmp_path / "db"
    e = Experiment(path)
    reserve(e)
    token = e.begin_create(now=1)
    reopened = Experiment(path)
    with pytest.raises(ValueError, match="inspect"):
        reopened.fake_create(token, now=2)
    with pytest.raises(ValueError, match="inspect"):
        reopened.begin_create(now=2)
    reopened.stop()
    for now in (3, 4, 5):
        state = Experiment(path).reconcile_once(now=now)
        assert state["cleanup_error"] == "create_unknown"
        assert state["outcome"] is None
    assert state["cleanup_attempts"] == 3
    # A delayed, already-in-flight fake response arrives after cancellation.
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO fake_resources VALUES ('late',?,1,1,0,0)",
            (state["marker"],),
        )
    reopened.receipt("late")
    state = Experiment(path).reconcile_once(now=6)
    assert state["outcome"] == "cancelled"
    assert resource(path, "late") == (0, 0)


def test_lost_reply_late_receipt_cancel_and_retry_storage(tmp_path):
    path = tmp_path / "db"
    e = Experiment(path)
    reserve(e)
    rid = e.fake_create(e.begin_create(now=1), now=1)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE fake_resources SET compute_failures=2, storage_failures=1 WHERE resource_id=?",
            (rid,),
        )
    e.stop()
    for now in (2, 3):
        s = Experiment(path).reconcile_once(now=now)
        assert s["cleanup_error"] == "compute_cleanup_failed"
        assert resource(path, rid) == (1, 1)
    s = Experiment(path).reconcile_once(now=4)
    assert s["cleanup_error"] == "storage_cleanup_failed"
    assert resource(path, rid) == (0, 1)
    e.receipt(rid)
    s = Experiment(path).reconcile_once(now=5)
    assert (s["phase"], s["outcome"], s["stop_reason"]) == (
        "terminated",
        "cancelled",
        "cancelled",
    )
    assert resource(path, rid) == (0, 0)
    e.receipt(rid)
    assert Experiment(path).reconcile_once(now=6)["phase"] == "terminated"


def test_foreign_receipt_never_touched_and_owned_duplicate_cleanup(tmp_path):
    path = tmp_path / "db"
    e = Experiment(path)
    rid = created(e)
    marker = e.status()["marker"]
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO fake_resources VALUES ('foreign','other',1,1,0,0)")
        db.execute(
            "INSERT INTO fake_resources VALUES ('duplicate',?,1,1,0,0)", (marker,)
        )
    e.receipt("foreign")
    e.stop()
    state = e.reconcile_once(now=1)
    assert state["cleanup_error"] == "ownership_unverified"
    assert resource(path, "foreign") == (1, 1)
    assert resource(path, rid) == resource(path, "duplicate") == (0, 0)
    assert Experiment(path).reconcile_once(now=2)["outcome"] is None


def test_bounded_cleanup_does_not_starve_later_owned_resources(tmp_path):
    path = tmp_path / "db"
    e = Experiment(path)
    created(e)
    marker = e.status()["marker"]
    with sqlite3.connect(path) as db:
        for number in range(12):
            db.execute(
                "INSERT INTO fake_resources VALUES (?,?,1,1,99,0)",
                (f"a{number:02}", marker),
            )
        db.execute("INSERT INTO fake_resources VALUES ('zz',?,1,1,0,0)", (marker,))
    e.stop()
    assert e.reconcile_once(now=1)["outcome"] is None
    assert resource(path, "zz") == (1, 1)
    assert e.reconcile_once(now=2)["outcome"] is None
    assert resource(path, "zz") == (0, 0)


def test_idle_starts_only_on_ready_and_refill_does_not_extend(tmp_path):
    path = tmp_path / "db"
    q = Queue(path)
    e = Experiment(path)
    rid = created(e)
    assert e.reconcile_once(now=20)["stop_reason"] is None
    assert e.ready(now=20)
    q.append(project="p", request_key="one", shot="s", version="v", phase=0, spec={})
    assert e.reconcile_once(now=29)["stop_reason"] is None
    q.append(project="p", request_key="two", shot="s", version="v", phase=0, spec={})
    assert e.reconcile_once(now=30)["stop_reason"] == "idle"
    assert resource(path, rid) == (0, 0)


def test_dead_worker_lease_and_cancel_queue_stop(tmp_path):
    path = tmp_path / "db"
    q = Queue(path)
    e = Experiment(path)
    rid = created(e)
    e.ready(now=2)
    e.work_started(now=3, lease_seconds=5)
    with pytest.raises(ValueError, match="already active"):
        e.work_started(now=4, lease_seconds=50)
    assert Experiment(path).reconcile_once(now=8)["stop_reason"] == "worker_lost"
    assert resource(path, rid) == (0, 0)
    q.cancel()
    assert not e.work_finished(now=9)
    assert e.status()["stop_reason"] == "worker_lost"


def test_queue_cancellation_prevents_create(tmp_path):
    path = tmp_path / "db"
    q = Queue(path)
    e = Experiment(path)
    reserve(e)
    token = e.begin_create(now=1)
    q.cancel()
    assert e.fake_create(token, now=2) is None
    s = Experiment(path).reconcile_once(now=3)
    assert s["stop_reason"] == "cancelled" and s["cleanup_error"] == "create_unknown"


def test_export_durable_and_deadline_wins(tmp_path):
    path = tmp_path / "success"
    e = Experiment(path)
    rid = created(e)
    e.ready(now=1)
    e.work_started(now=2, lease_seconds=50)
    with pytest.raises(ValueError, match="export required"):
        e.finish(now=3)
    assert e.export(now=3, name="out", content=b"data", metadata={"shot": 1})
    assert e.export(now=4, name="out", content=b"data", metadata={"shot": 1})
    with pytest.raises(ValueError, match="conflict"):
        e.export(now=4, name="out", content=b"other", metadata={"shot": 1})
    assert e.finish(now=5)
    assert Experiment(path).reconcile_once(now=6)["outcome"] == "succeeded"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT content, metadata FROM experiment_exports"
        ).fetchone() == (b"data", '{"shot": 1}')
    assert resource(path, rid) == (0, 0)
    late = Experiment(tmp_path / "late")
    rid = created(late)
    late.ready(now=1)
    late.work_started(now=2, lease_seconds=58)
    assert not late.export(now=60, name="x", content=b"x", metadata={"shot": 1})
    assert late.reconcile_once(now=60)["outcome"] == "deadline"
    assert resource(late.path, rid) == (0, 0)


def test_separate_cli_deadline_tick_without_worker(tmp_path):
    path = tmp_path / "db"
    e = Experiment(path)
    rid = created(e, now=time.time() - 61)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-m", "django_ergo.plugins.runpod.experiment", str(path)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    state = json.loads(result.stdout)
    assert (state["stop_reason"], state["outcome"]) == ("deadline", "deadline")
    assert resource(path, rid) == (0, 0)
