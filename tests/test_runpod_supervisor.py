import io
import json
import multiprocessing
import os
import socket
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from django_ergo.plugins.runpod.budget import Evidence
from django_ergo.plugins.runpod.budget import RentalTimeline
from django_ergo.plugins.runpod.cleanup import RESTCleanup
from django_ergo.plugins.runpod.experiment import Experiment
from django_ergo.plugins.runpod.supervisor import CleanerSupervision
from django_ergo.plugins.runpod.supervisor import main
from django_ergo.plugins.runpod.transport import FixtureRunpodREST
from tests.runpod_stock_fixtures import hw


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        msg = "network forbidden: offline fixture required"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / "ledger"
    Experiment(path).reserve(
        now=0,
        idle_grace=10,
        eligibility=dict.fromkeys(
            ("model", "hardware", "license", "inputs", "budget"), True
        ),
        timeline=RentalTimeline(60, 10, 10, 10, 10, 10, 10),
        compute_hourly_usd=Evidence(Decimal("1"), True),
        storage_usd=Evidence(Decimal("0.1"), True),
        network_usd=Evidence(Decimal("0.1"), True),
        fees_usd=Evidence(Decimal("0.1"), True),
        project_cap_usd=Decimal("3"),
        project_committed_usd=Decimal("0"),
        rental_cap_usd=Decimal("3"),
    )
    owner = RESTCleanup(path).configure_create(
        account="account",
        image="image@sha256:" + "1" * 64,
        hardware=hw(120),
        gpu_type="NVIDIA RTX A6000",
        data_center_ids=["EU-RO-1"],
    )
    return path, owner


def owned_pod(owner):
    return {
        "id": "pod123",
        "consumerUserId": owner.account,
        "imageName": owner.image,
        "containerDiskInGb": owner.container_disk_gb,
        "volumeInGb": 0,
        "env": {
            "ERGO_ATTEMPT_ID": owner.attempt,
            "ERGO_ATTEMPT_FINGERPRINT": owner.fingerprint,
        },
    }


class HTTPFixture:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.methods = []

    def open(self, request, timeout):
        self.methods.append(request.method)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode() if result is not None else b"")


def missing():
    return HTTPError("https://rest.runpod.io/v1/pods/pod123", 404, "missing", {}, None)


def run_fixture(path, http, now):
    def run(command, **kwargs):
        assert command[:3] == [
            sys.executable,
            "-m",
            "django_ergo.plugins.runpod.cleanup",
        ]
        assert command[3] == str(path.resolve())
        assert kwargs == {"timeout": 5, "capture_output": True, "check": False}
        RESTCleanup(path).reconcile_once(now=now, client=FixtureRunpodREST(http))
        return SimpleNamespace(returncode=0)

    return run


def test_missing_ledger_and_missing_configuration_fail_closed(tmp_path, prepared):
    with pytest.raises(ValueError, match="existing ledger"):
        CleanerSupervision(tmp_path / "absent")
    path, _ = prepared
    with pytest.raises(ValueError, match="not configured"):
        CleanerSupervision(path).require_fresh(now=1)


@pytest.mark.parametrize(
    "config",
    [
        {"interval_seconds": 0},
        {"operation_timeout": True},
        {"freshness_seconds": float("inf")},
        {"freshness_seconds": 6},
    ],
)
def test_invalid_timing_rejected(prepared, config):
    path, _ = prepared
    with pytest.raises(ValueError, match="positive timings"):
        CleanerSupervision(path).configure(**config)


def test_timings_immutable_and_hour_deadline_required(prepared):
    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    CleanerSupervision(path).configure()
    with pytest.raises(ValueError, match="already bound"):
        supervisor.configure(freshness_seconds=11)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE experiment SET deadline=3601")
    with pytest.raises(ValueError, match="within one hour"):
        supervisor.configure()


def test_freshness_is_independent_bounded_and_does_not_extend_deadline(
    prepared, monkeypatch
):
    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    with pytest.raises(ValueError, match="missing or stale"):
        supervisor.require_fresh(now=1, require_execute=False)
    monkeypatch.setattr(os, "getpid", lambda: 100)
    supervisor.tick(now=1, execute=True, credential_env="SCOPED_RUNPOD_TOKEN")
    with pytest.raises(ValueError, match="independent process"):
        supervisor.require_fresh(now=1)
    monkeypatch.setattr(os, "getpid", lambda: 101)
    assert supervisor.require_fresh(now=1)
    assert supervisor.require_fresh(now=10.99)
    with pytest.raises(ValueError, match="missing or stale"):
        supervisor.require_fresh(now=11)
    with pytest.raises(ValueError, match="missing or stale"):
        supervisor.require_fresh(now=0)
    assert Experiment(path).status()["deadline"] == 60
    with pytest.raises(ValueError, match="experiment stopped"):
        supervisor.require_fresh(now=60)
    assert Experiment(path).status()["stop_reason"] == "deadline"


def test_before_deadline_no_cleanup_process_and_deadline_controls_wakeup(prepared):
    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure()

    def deny(*args, **kwargs):
        msg = "cleanup process invoked before due"
        raise AssertionError(msg)

    assert supervisor.tick(now=1, run=deny)["stop_reason"] is None
    assert supervisor.delay(now=59.75) == 0.25
    with sqlite3.connect(path) as db:
        db.execute("UPDATE experiment SET work_deadline=2.5, idle_deadline=2.75")
    assert supervisor.delay(now=2.25) == 0.25


def test_bounded_child_timeout_persists_stop_and_retries_without_create(prepared):
    path, owner = prepared
    Experiment(path).begin_create(now=1)
    Experiment(path).receipt("pod123")
    supervisor = CleanerSupervision(path)
    supervisor.configure()

    def hung(command, **kwargs):
        assert kwargs["timeout"] == 5
        assert Experiment(path).status()["stop_reason"] == "deadline"
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"secret")

    state = supervisor.tick(now=60, run=hung)
    assert state["tick_error"] == "cleanup_operation_timeout"
    assert state["cleanup_error"] == "cleanup_operation_timeout"
    assert "secret" not in json.dumps(state)
    http = HTTPFixture(
        [owned_pod(owner)], owned_pod(owner), owned_pod(owner), None, missing()
    )
    state = CleanerSupervision(path).tick(now=61, run=run_fixture(path, http, 61))
    assert state["phase"] == "terminated"
    assert state["outcome"] == "deadline"
    assert http.methods == ["GET", "GET", "GET", "DELETE", "GET"]
    assert state["tick_error"] is None
    with pytest.raises(ValueError, match="inspect"):
        RESTCleanup(path).create_once(now=62, client=FixtureRunpodREST(HTTPFixture()))


def test_unknown_create_and_storage_remain_pending_across_ticks(prepared):
    path, _ = prepared
    Experiment(path).begin_create(now=1)
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    for now in (60, 61):
        state = supervisor.tick(now=now, run=run_fixture(path, HTTPFixture([]), now))
        assert state["cleanup_error"] == "create_unknown"
        assert state["outcome"] is None
    Experiment(path).receipt("pod123")
    state = supervisor.tick(
        now=62, run=run_fixture(path, HTTPFixture([], missing()), 62)
    )
    assert state["cleanup_error"] == "storage_unverified"
    assert state["phase"] == "cleanup_pending"


def test_explicit_credential_name_only_reaches_child_arguments(prepared):
    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    with pytest.raises(ValueError, match="explicit credential"):
        supervisor.tick(now=1, execute=True)
    with pytest.raises(ValueError, match="requires execute"):
        supervisor.tick(now=1, credential_env="SCOPED_RUNPOD_TOKEN")

    def failed(command, **kwargs):
        assert command[-3:] == [
            "--execute",
            "--credential-env",
            "SCOPED_RUNPOD_TOKEN",
        ]
        return SimpleNamespace(returncode=2, stderr=b"sensitive failure")

    state = supervisor.tick(
        now=60,
        execute=True,
        credential_env="SCOPED_RUNPOD_TOKEN",
        run=failed,
    )
    assert state["tick_error"] == "cleanup_process_failed"
    assert "sensitive" not in json.dumps(state)


def killed_worker(path, ready):
    CleanerSupervision(path).require_fresh(now=1)
    assert Experiment(path).work_started(now=1, lease_seconds=60)
    ready.send("mid-shot")
    ready.recv()


def test_killed_worker_midshot_does_not_prevent_hard_deadline_cleanup(prepared):
    path, owner = prepared
    Experiment(path).begin_create(now=0)
    Experiment(path).receipt("pod123")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE experiment SET phase='ready'")
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    supervisor.tick(now=1, execute=True, credential_env="SCOPED_RUNPOD_TOKEN")
    context = multiprocessing.get_context("fork")
    parent_pipe, worker_pipe = context.Pipe()
    worker = context.Process(target=killed_worker, args=(path, worker_pipe))
    worker.start()
    try:
        assert parent_pipe.poll(5), "worker failed to reach mid-shot fixture"
        assert parent_pipe.recv() == "mid-shot"
        worker.kill()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker.exitcode != 0
        assert Experiment(path).status()["work_deadline"] == 60
        http = HTTPFixture(
            [owned_pod(owner)], owned_pod(owner), owned_pod(owner), None, missing()
        )
        state = CleanerSupervision(path).tick(now=60, run=run_fixture(path, http, 60))
        assert state["phase"] == "terminated"
        assert state["outcome"] == "deadline"
        assert "POST" not in http.methods
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
        parent_pipe.close()
        worker_pipe.close()
        worker.close()


def test_supervisor_cli_preparation_and_fresh_check(prepared, monkeypatch, capsys):
    path, _ = prepared
    calls = []

    def run(self, **kwargs):
        calls.append(kwargs)
        self.tick(now=1)

    monkeypatch.setattr(CleanerSupervision, "run_forever", run)
    main([str(path)])
    assert calls == [{"execute": False, "credential_env": None}]
    monkeypatch.setattr(os, "getpid", lambda: -1)
    monkeypatch.setattr("django_ergo.plugins.runpod.supervisor.time.time", lambda: 2)
    with pytest.raises(SystemExit) as stale:
        main([str(path), "--check-fresh"])
    assert stale.value.code == 2
    assert "stale heartbeat" in capsys.readouterr().err
    # An execute-configured heartbeat needs no credential or HTTP before due.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE cleaner_supervision SET executing=1")
    main([str(path), "--check-fresh"])
    assert json.loads(capsys.readouterr().out) == {"cleaner_fresh": True}
    with pytest.raises(SystemExit) as exc:
        main([str(path), "--execute"])
    assert exc.value.code == 2
    assert "explicit --credential-env" in capsys.readouterr().err


def test_supervisor_keeps_monitoring_after_absence_and_excludes_second_process(
    prepared, monkeypatch
):
    import fcntl

    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    with Path(str(path) + ".cleaner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="another cleaner"):
            supervisor.run_forever()
    calls = []

    def tick(**kwargs):
        calls.append(kwargs)
        return {"phase": "terminated"}

    def sleep(delay):
        assert delay == 1
        if len(calls) == 2:
            msg = "fixture stops independent supervisor"
            raise RuntimeError(msg)

    monkeypatch.setattr(supervisor, "tick", tick)
    monkeypatch.setattr(supervisor, "delay", lambda **kwargs: 1)
    monkeypatch.setattr("django_ergo.plugins.runpod.supervisor.time.sleep", sleep)
    with pytest.raises(RuntimeError, match="fixture stops"):
        supervisor.run_forever()
    assert len(calls) == 2


def test_real_bounded_child_is_killed_and_reaped_without_provider_calls(prepared):
    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure(operation_timeout=0.1)

    def local_hang(command, **kwargs):
        return subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            check=kwargs.pop("check", False),
            **kwargs,
        )

    state = supervisor.tick(now=60, run=local_hang)
    assert state["tick_error"] == "cleanup_operation_timeout"
    assert state["stop_reason"] == "deadline"


def test_dry_run_and_failed_tick_cannot_admit_real_worker(prepared, monkeypatch):
    path, _ = prepared
    supervisor = CleanerSupervision(path)
    supervisor.configure()
    supervisor.tick(now=1)
    monkeypatch.setattr(os, "getpid", lambda: -1)
    assert supervisor.require_fresh(now=2, require_execute=False)
    with pytest.raises(ValueError, match="dry-run"):
        supervisor.require_fresh(now=2)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE cleaner_supervision SET executing=1, tick_error='cleanup_process_failed'"
        )
    with pytest.raises(ValueError, match="tick has failed"):
        supervisor.require_fresh(now=2)
