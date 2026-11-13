import io
import json
import socket
import sqlite3

import pytest

from django_ergo.plugins.runpod.create import create_once
from django_ergo.plugins.runpod.create import prepare
from django_ergo.plugins.runpod.experiment import Experiment


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        msg = "network forbidden"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def config():
    return {
        "mode": "offline-fixture",
        "live_execute": False,
        "account": "fixture-account",
        "image": "fixture/h3@sha256:" + "1" * 64,
        "data_center_ids": ["EU-RO-1"],
        "idle_grace": 10,
        "eligibility": {
            "model": True,
            "hardware": True,
            "license": "delegated",
            "inputs": True,
            "budget": True,
        },
        "timeline": {
            "rental_seconds": 3600,
            "startup_seconds": 100,
            "download_seconds": 100,
            "compile_seconds": 100,
            "generation_seconds": 100,
            "export_seconds": 100,
            "termination_seconds": 100,
        },
        "pricing": {
            "compute_hourly_usd": {"amount": "1.20", "verified": True},
            "storage_usd": {"amount": "0.01", "verified": True},
            "network_usd": {"amount": "0.01", "verified": True},
            "fees_usd": {"amount": "0.01", "verified": True},
            "project_cap_usd": "3.00",
            "project_committed_usd": "0",
        },
    }


class FixtureHTTP:
    def __init__(self, path, response):
        self.path = path
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        with sqlite3.connect(self.path) as db:
            assert db.execute(
                "SELECT phase, create_started FROM experiment"
            ).fetchone() == ("create_unknown", 1)
        result = self.response() if callable(self.response) else self.response
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode())


def fresh_cleaner(path):
    from django_ergo.plugins.runpod.supervisor import CleanerSupervision

    CleanerSupervision(path).configure()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE cleaner_supervision SET heartbeat=101, process_id=-1, executing=1"
        )


def submit(path, opener):
    fresh_cleaner(path)
    return create_once(
        path, now=101, opener=opener, mode="offline-fixture", live_execute=False
    )


def test_profile_and_one_committed_post(tmp_path, config):
    path = tmp_path / "ledger"
    state = prepare(path, config, now=100)
    assert json.loads(state["evidence"])["eligibility"]["license"] == "delegated"
    fake = FixtureHTTP(path, {"id": "pod123"})
    result = submit(path, fake)
    assert result["phase"] == "create_unknown"
    assert result["create_started"] == 1
    body = json.loads(fake.requests[0].data)
    assert fake.requests[0].method == "POST"
    assert body["gpuTypeIds"] == ["NVIDIA RTX 6000 Ada Generation"]
    assert body["dataCenterIds"] == ["EU-RO-1"]
    assert body["containerDiskInGb"] == 200
    assert body["volumeInGb"] == 0
    assert "networkVolumeId" not in body and "templateId" not in body
    assert float(body["env"]["ERGO_DEADLINE"]) == state["deadline"] == 3700
    assert body["env"]["ERGO_COMPUTE_HOURLY_USD"] == "1.20"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT resource_id FROM experiment_receipts").fetchall() == [
            ("pod123",)
        ]
    with pytest.raises(ValueError, match="already attempted"):
        submit(path, fake)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("response", [TimeoutError(), {"id": "bad-id"}, {}])
def test_failed_receipt_never_replays(tmp_path, config, response):
    path = tmp_path / "ledger"
    prepare(path, config, now=100)
    fake = FixtureHTTP(path, response)
    state = submit(path, fake)
    assert state["phase"] == "create_unknown"
    assert state["cleanup_error"] == "provider_request_failed"
    with pytest.raises(ValueError, match="already attempted"):
        submit(path, fake)
    assert len(fake.requests) == 1


def test_late_receipt_survives_expiry(tmp_path, config):
    path = tmp_path / "ledger"
    prepare(path, config, now=100)

    def late():
        Experiment(path).begin_create(now=3700)  # expires existing intent
        return {"id": "late123"}

    state = submit(path, FixtureHTTP(path, late))
    assert state["stop_reason"] == "deadline"
    assert state["phase"] == "cleanup_pending"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT resource_id FROM experiment_receipts").fetchall() == [
            ("late123",)
        ]


def test_live_gate_never_posts_or_reserves(tmp_path, config):
    path = tmp_path / "ledger"
    config["live_execute"] = True
    with pytest.raises(ValueError, match="offline"):
        prepare(path, config, now=100)
    assert not path.exists()
    config["live_execute"] = False
    prepare(path, config, now=100)
    fake = FixtureHTTP(path, {"id": "pod123"})
    with pytest.raises(ValueError, match="mode differs"):
        create_once(path, now=101, opener=fake, mode="live", live_execute=True)
    assert not fake.requests
    assert Experiment(path).status()["create_started"] == 0


def test_offline_fixture_requires_cleaner(tmp_path, config):
    path = tmp_path / "ledger"
    prepare(path, config, now=100)
    fake = FixtureHTTP(path, {"id": "pod123"})
    with pytest.raises(ValueError, match="independent cleaner not configured"):
        create_once(
            path, now=101, opener=fake, mode="offline-fixture", live_execute=False
        )
    assert not fake.requests
    assert Experiment(path).status()["create_started"] == 0


def test_live_requires_fresh_executing_independent_cleaner(
    tmp_path, config, monkeypatch
):
    from django_ergo.plugins.runpod import create
    from django_ergo.plugins.runpod.supervisor import CleanerSupervision
    from django_ergo.plugins.runpod.transport import RunpodREST

    path = tmp_path / "ledger"
    config["mode"] = "live"
    config["live_execute"] = True
    prepare(path, config, now=100)
    CleanerSupervision(path).configure()
    fake = FixtureHTTP(path, TimeoutError())
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture")
    monkeypatch.setattr(
        create, "RunpodREST", lambda credential: RunpodREST(credential, opener=fake)
    )
    with pytest.raises(ValueError, match="dry-run"):
        create_once(
            path,
            now=101,
            mode="live",
            live_execute=True,
            credential_env="FIXTURE_TOKEN",
        )
    assert not fake.requests
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE cleaner_supervision SET heartbeat=101, process_id=-1, executing=1"
        )
    state = create_once(
        path, now=101, mode="live", live_execute=True, credential_env="FIXTURE_TOKEN"
    )
    assert state["cleanup_error"] == "provider_request_failed"
    with pytest.raises(ValueError, match="already attempted"):
        create_once(
            path,
            now=101,
            mode="live",
            live_execute=True,
            credential_env="FIXTURE_TOKEN",
        )
    assert len(fake.requests) == 1
