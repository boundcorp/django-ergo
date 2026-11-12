import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
from decimal import Decimal
from urllib.error import HTTPError

import pytest

from django_ergo.plugins.runpod.budget import Evidence
from django_ergo.plugins.runpod.budget import RentalTimeline
from django_ergo.plugins.runpod.cleanup import RESTCleanup
from django_ergo.plugins.runpod.experiment import Experiment
from django_ergo.plugins.runpod.transport import Ownership
from django_ergo.plugins.runpod.transport import RunpodREST


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        msg = "network forbidden"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def make_ledger(path):
    experiment = Experiment(path)
    state = experiment.reserve(
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
    )
    owner = Ownership(
        "account", state["marker"], "fingerprint", "image@sha256:123", 120
    )
    RESTCleanup(path).configure(owner)
    experiment.begin_create(now=1)
    return experiment, owner


def pod(owner, **changes):
    result = {
        "id": "pod123",
        "consumerUserId": owner.account,
        "image": owner.image,
        "containerDiskInGb": owner.container_disk_gb,
        "volumeInGb": 0,
        "networkVolume": None,
        "env": {
            "ERGO_ATTEMPT_ID": owner.attempt,
            "ERGO_ATTEMPT_FINGERPRINT": owner.fingerprint,
        },
    }
    result.update(changes)
    return result


class FakeHTTP:
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
    return HTTPError("https://invalid/secret", 404, "secret", {}, None)


def tick(path, fake, now=61):
    return RESTCleanup(path).reconcile_once(
        now=now, client=RunpodREST(lambda: "secret", opener=fake)
    )


def test_lost_receipt_discovery_failed_delete_retried_and_404_keeps_storage_unknown(
    tmp_path,
):
    path = tmp_path / "ledger"
    experiment, owner = make_ledger(path)
    with pytest.raises(ValueError, match="inspect"):
        Experiment(path).begin_create(now=2)
    first = FakeHTTP([pod(owner)], pod(owner), pod(owner), TimeoutError("secret"))
    state = tick(path, first)
    assert state["stop_reason"] == "deadline"
    assert state["cleanup_error"] == "provider_request_failed"
    assert first.methods == ["GET", "GET", "GET", "DELETE"]
    second = FakeHTTP([pod(owner)], pod(owner), pod(owner), None, missing())
    state = tick(path, second, now=62)
    assert second.methods == ["GET", "GET", "GET", "DELETE", "GET"]
    assert state["cleanup_error"] == "storage_unverified"
    assert state["outcome"] is None and state["phase"] == "cleanup_pending"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT compute_absent FROM rest_resources").fetchone() == (
            1,
        )
    assert experiment.status()["stop_reason"] == "deadline"


def test_foreign_receipt_is_never_deleted(tmp_path):
    path = tmp_path / "ledger"
    experiment, owner = make_ledger(path)
    experiment.receipt("pod123")
    fake = FakeHTTP([], pod(owner, consumerUserId="foreign"))
    state = tick(path, fake)
    assert state["cleanup_error"] == "ownership_unverified"
    assert fake.methods == ["GET", "GET"]


def test_uncertain_reads_and_empty_inventory_never_prove_absence(tmp_path):
    path = tmp_path / "ledger"
    experiment, _ = make_ledger(path)
    empty = tick(path, FakeHTTP([]))
    assert empty["cleanup_error"] == "create_unknown"
    experiment.receipt("pod123")
    state = tick(path, FakeHTTP([], TimeoutError("secret")), now=62)
    assert state["cleanup_error"] == "provider_request_failed"
    assert state["outcome"] is None


def test_empty_successful_read_does_not_confirm_compute_absence(tmp_path):
    path = tmp_path / "ledger"
    experiment, _ = make_ledger(path)
    experiment.receipt("pod123")
    state = tick(path, FakeHTTP([], None))
    assert state["cleanup_error"] == "provider_request_failed"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT compute_absent FROM rest_resources").fetchone() == (
            0,
        )


@pytest.mark.parametrize(
    ("field", "reason"),
    [("idle_deadline", "idle"), ("work_deadline", "worker_lost")],
)
def test_expired_lease_persists_stop_without_worker(tmp_path, field, reason):
    path = tmp_path / "ledger"
    make_ledger(path)
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE experiment SET {field} = 5")
    state = RESTCleanup(path).reconcile_once(now=6)
    assert state["stop_reason"] == reason
    assert state["cleanup_error"] == "dry_run"


def test_not_due_does_not_request_credentials(tmp_path):
    path = tmp_path / "ledger"
    make_ledger(path)

    def deny():
        msg = "credential fetched prematurely"
        raise AssertionError(msg)

    state = RESTCleanup(path).reconcile_once(now=2, client=RunpodREST(deny))
    assert state["stop_reason"] is None


def test_dry_run_command_expires_worker_without_credentials_or_network(tmp_path):
    path = tmp_path / "ledger"
    experiment, _ = make_ledger(path)
    result = subprocess.run(
        [sys.executable, "-m", "django_ergo.plugins.runpod.cleanup", str(path)],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        check=True,
    )
    state = json.loads(result.stdout)
    assert state["stop_reason"] == "deadline"
    assert state["cleanup_error"] == "dry_run"
    assert state["outcome"] is None
    with pytest.raises(ValueError, match="REST cleanup"):
        experiment.reconcile_once(now=100)


def test_execute_requires_explicit_credential_name(tmp_path):
    path = tmp_path / "ledger"
    make_ledger(path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "django_ergo.plugins.runpod.cleanup",
            str(path),
            "--execute",
        ],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "explicit --credential-env" in result.stderr
