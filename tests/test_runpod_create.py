import base64
import hashlib
import io
import json
import socket
import sqlite3
from decimal import Decimal
from urllib.error import HTTPError

import pytest

from django_ergo.plugins.runpod.budget import Evidence
from django_ergo.plugins.runpod.budget import RentalTimeline
from django_ergo.plugins.runpod.cleanup import RESTCleanup
from django_ergo.plugins.runpod.experiment import Experiment
from django_ergo.plugins.runpod.transport import FixtureRunpodREST
from django_ergo.plugins.runpod.transport import RunpodREST


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        msg = "network forbidden: fixture opener required"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def reserved(tmp_path, request):
    path = tmp_path / "ledger"
    now, duration = getattr(request, "param", (0, 60))
    Experiment(path).reserve(
        now=now,
        idle_grace=10,
        eligibility=dict.fromkeys(
            ("model", "hardware", "license", "inputs", "budget"), True
        ),
        timeline=RentalTimeline(duration, 10, 10, 10, 10, 10, 10),
        compute_hourly_usd=Evidence(Decimal("1"), True),
        storage_usd=Evidence(Decimal("0.1"), True),
        network_usd=Evidence(Decimal("0.1"), True),
        fees_usd=Evidence(Decimal("0.1"), True),
        project_cap_usd=Decimal("3"),
        project_committed_usd=Decimal("0"),
    )
    return path


@pytest.fixture
def configured(reserved):
    path = reserved
    owner = RESTCleanup(path).configure_create(
        account="account",
        image="image@sha256:" + "1" * 64,
        container_disk_gb=120,
        gpu_type="NVIDIA RTX A6000",
        data_center_ids=["EU-RO-1"],
    )
    return path, owner


def owned_pod(owner, **changes):
    # GET /pods documents these fields, but does not require networkVolume or
    # promise an explicit null. The immutable request supplies that evidence.
    result = {
        "id": "pod123",
        "consumerUserId": owner.account,
        "image": owner.image,
        "containerDiskInGb": owner.container_disk_gb,
        "volumeInGb": 0,
        "env": {
            "ERGO_ATTEMPT_ID": owner.attempt,
            "ERGO_ATTEMPT_FINGERPRINT": owner.fingerprint,
        },
    }
    result.update(changes)
    return result


def missing(pod_id="pod123"):
    return HTTPError(
        "https://rest.runpod.io/v1/pods/" + pod_id, 404, "missing", {}, None
    )


class FixtureResponse(io.BytesIO):
    def __init__(self, result, status):
        super().__init__(json.dumps(result).encode() if result is not None else b"")
        self.status = status


class FixtureHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        assert request.full_url.startswith("https://rest.runpod.io/v1/pods")
        assert request.get_header("Authorization") == "Bearer offline-fixture"
        assert 0 < timeout <= 30
        self.requests.append(request)
        result = self.responses.pop(0)
        if callable(result):
            result = result(request)
        if isinstance(result, Exception):
            raise result
        status = {"POST": 201, "GET": 200, "DELETE": 204}[request.method]
        return FixtureResponse(result, status)

    @property
    def methods(self):
        return [request.method for request in self.requests]


def reconcile(path, fake, now=61):
    return RESTCleanup(path).reconcile_once(now=now, client=FixtureRunpodREST(fake))


def assert_terminated(state):
    assert state["phase"] == "terminated"
    assert state["outcome"] == "deadline"
    assert state["cleanup_error"] is None


def test_serialized_create_intent_is_durable_before_post_and_never_starts_work(
    configured,
):
    path, owner = configured
    bodies = []

    def accepted(request):
        # A separate connection must observe the committed intent while POST is
        # executing, not merely after the fake response returns.
        with sqlite3.connect(path) as db:
            assert db.execute(
                "SELECT phase, create_started FROM experiment"
            ).fetchone() == ("create_unknown", 1)
            assert db.execute(
                "SELECT COUNT(*) FROM experiment_receipts"
            ).fetchone() == (0,)
        assert request.full_url == "https://rest.runpod.io/v1/pods"
        assert request.get_header("Content-type") == "application/json"
        body = json.loads(request.data)
        bodies.append(body)
        assert body["name"] == "ergo-" + owner.attempt
        assert body["computeType"] == "GPU"
        assert body["imageName"] == owner.image
        assert body["containerDiskInGb"] == 120
        assert body["gpuTypeIds"] == ["NVIDIA RTX A6000"]
        assert body["gpuCount"] == 1
        assert body["cloudType"] == "SECURE"
        assert body["volumeInGb"] == 0
        assert body["minRAMPerGPU"] == 167
        assert body["minVCPUPerGPU"] == 10
        assert "allowedCudaVersions" not in body
        assert body["dataCenterIds"] == ["EU-RO-1"]
        assert body["dataCenterPriority"] == "custom"
        assert float(body["env"]["ERGO_DEADLINE"]) == 60
        assert "templateId" not in body
        assert "networkVolumeId" not in body
        fingerprint = body["env"].pop("ERGO_ATTEMPT_FINGERPRINT")
        assert body["env"]["ERGO_ATTEMPT_ID"] == owner.attempt
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        assert (
            fingerprint
            == owner.fingerprint
            == hashlib.sha256(canonical.encode()).hexdigest()
        )
        # POST returns a Pod, but ownership is checked only using later GETs.
        return {"id": "pod123"}

    fake = FixtureHTTP(accepted)
    state = RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    assert state["phase"] == "create_unknown"
    assert state["create_started"] == 1
    assert state["outcome"] is None
    assert fake.methods == ["POST"]
    assert len(bodies) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT resource_id FROM experiment_receipts").fetchall() == [
            ("pod123",)
        ]
    with pytest.raises(ValueError, match="already attempted"):
        RESTCleanup(path).create_once(now=2, client=FixtureRunpodREST(fake))
    assert fake.methods == ["POST"]


def test_ssh_access_bound_before_fingerprinting(reserved):
    from django_ergo.plugins.runpod.ssh import create_access

    key = (
        "ssh-ed25519 "
        + base64.b64encode(
            b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32
        ).decode()
    )
    owner = RESTCleanup(reserved).configure_create(
        account="account",
        image="image@sha256:" + "1" * 64,
        container_disk_gb=200,
        gpu_type="NVIDIA RTX 6000 Ada Generation",
        data_center_ids=["EU-RO-1"],
        ssh_public_key=key,
    )
    with sqlite3.connect(reserved) as db:
        body = json.loads(
            db.execute("SELECT body FROM rest_create_profile").fetchone()[0]
        )
    access = create_access(key)
    assert body["env"]["ERGO_SSH_PUBLIC_KEY"] == access["env"]["ERGO_SSH_PUBLIC_KEY"]
    assert body["dockerEntrypoint"] == access["dockerEntrypoint"]
    assert body["ports"] == ["22/tcp"]
    fingerprint = body["env"].pop("ERGO_ATTEMPT_FINGERPRINT")
    assert (
        fingerprint
        == owner.fingerprint
        == hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_accepted_post_lost_response_empty_scan_then_late_owned_cleanup(configured):
    path, owner = configured
    fake = FixtureHTTP(TimeoutError("fixture response lost"), [])
    state = RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    assert state["phase"] == "create_unknown"
    assert state["create_started"] == 1
    assert state["cleanup_error"] == "provider_request_failed"
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "create_unknown"
    assert state["outcome"] is None
    assert fake.methods == ["POST", "GET"]
    assert fake.requests[-1].full_url.endswith("/pods?includeNetworkVolume=true")

    # A later inventory reveals the accepted Pod. Omitted networkVolume is not
    # treated as a provider promise: cleanup relies on the strict create profile.
    resource = owned_pod(owner)
    fake.responses.extend([[resource], resource, resource, None, missing()])
    assert_terminated(reconcile(path, fake, now=62))
    assert fake.methods == ["POST", "GET", "GET", "GET", "GET", "DELETE", "GET"]
    assert all(
        request.full_url.endswith("?includeNetworkVolume=true")
        for request in fake.requests
        if request.method == "GET"
    )
    assert fake.responses == []


def test_empty_inventory_forever_neither_replays_create_nor_proves_absence(configured):
    path, _ = configured
    fake = FixtureHTTP(TimeoutError("lost"), [], [], [])
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    for now in (61, 62, 63):
        with pytest.raises(ValueError, match="already attempted"):
            RESTCleanup(path).create_once(now=now, client=FixtureRunpodREST(fake))
        state = reconcile(path, fake, now=now)
        assert state["cleanup_error"] == "create_unknown"
        assert state["outcome"] is None
    assert fake.methods == ["POST", "GET", "GET", "GET"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM rest_resources").fetchone() == (0,)


@pytest.mark.parametrize("mismatch", ["account", "attempt", "fingerprint", "name_only"])
def test_foreign_or_name_only_inventory_never_deleted(configured, mismatch):
    path, owner = configured
    resource = owned_pod(owner, name=owner.attempt)
    if mismatch == "account":
        resource["consumerUserId"] = "foreign"
    elif mismatch == "attempt":
        resource["env"]["ERGO_ATTEMPT_ID"] = "foreign"
    elif mismatch == "fingerprint":
        resource["env"]["ERGO_ATTEMPT_FINGERPRINT"] = "foreign"
    else:
        del resource["env"]
    fake = FixtureHTTP(TimeoutError("lost"), [resource])
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "create_unknown"
    assert state["outcome"] is None
    assert fake.methods == ["POST", "GET"]


def test_receipt_get404_without_owned_observation_cannot_prove_storage_absence(
    configured,
):
    path, _ = configured
    fake = FixtureHTTP({"id": "pod123"}, [], missing(), [])
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    for now in (61, 62):
        state = reconcile(path, fake, now=now)
        assert state["cleanup_error"] == "storage_unverified"
        assert state["outcome"] is None
    assert fake.methods == ["POST", "GET", "GET", "GET"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT compute_absent FROM rest_resources").fetchone() == (
            1,
        )


def test_stale_inventory_then_get404_never_establishes_storage_proof(configured):
    path, owner = configured
    fake = FixtureHTTP(TimeoutError("lost"), [owned_pod(owner)], missing())
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "storage_unverified"
    assert state["outcome"] is None
    assert fake.methods == ["POST", "GET", "GET"]


@pytest.mark.parametrize("read_stage", ["first_read", "fresh_delete_read"])
@pytest.mark.parametrize("mismatch", ["account", "fingerprint", "network_volume"])
def test_changed_ownership_or_storage_revokes_previous_storage_proof(
    configured, read_stage, mismatch
):
    path, owner = configured
    resource = owned_pod(owner)
    fake = FixtureHTTP(
        {"id": "pod123"},
        [resource],
        resource,
        resource,
        TimeoutError("delete uncertain"),
    )
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    assert reconcile(path, fake)["cleanup_error"] == "provider_request_failed"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM rest_storage_proofs").fetchone() == (1,)
    changed = owned_pod(owner)
    if mismatch == "account":
        changed["consumerUserId"] = "foreign"
    elif mismatch == "fingerprint":
        changed["env"]["ERGO_ATTEMPT_FINGERPRINT"] = "foreign"
    else:
        changed["networkVolume"] = {"id": "persistent123", "size": 20}
    fake.responses.append([])
    if read_stage == "fresh_delete_read":
        fake.responses.append(resource)
    fake.responses.append(changed)
    state = reconcile(path, fake, now=62)
    assert state["cleanup_error"] == "ownership_unverified"
    assert state["outcome"] is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM rest_storage_proofs").fetchone() == (0,)
    fake.responses.extend([[], missing()])
    state = reconcile(path, fake, now=63)
    assert state["cleanup_error"] == "storage_unverified"
    assert state["outcome"] is None
    assert fake.methods.count("DELETE") == 1


@pytest.mark.parametrize(
    "delete_failure", [missing(), TimeoutError("delete uncertain")]
)
def test_delete404_or_timeout_stays_pending_until_later_known_get404(
    configured, delete_failure
):
    path, owner = configured
    resource = owned_pod(owner)
    fake = FixtureHTTP({"id": "pod123"}, [resource], resource, resource, delete_failure)
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "provider_request_failed"
    assert state["outcome"] is None
    assert fake.methods == ["POST", "GET", "GET", "GET", "DELETE"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT compute_absent FROM rest_resources").fetchone() == (
            0,
        )
    fake.responses.extend([[], missing()])
    assert_terminated(reconcile(path, fake, now=62))
    assert fake.methods[-2:] == ["GET", "GET"]
    assert fake.methods.count("DELETE") == 1


def test_delayed_delete_get200_and_timeout_retry_do_not_mark_compute_absent(configured):
    path, owner = configured
    resource = owned_pod(owner, networkVolume=None)
    fake = FixtureHTTP({"id": "pod123"}, [resource], resource, resource, None, resource)
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "compute_cleanup_pending"
    assert state["outcome"] is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT compute_absent FROM rest_resources").fetchone() == (
            0,
        )
    fake.responses.extend([[], resource, resource, TimeoutError("delete uncertain")])
    state = reconcile(path, fake, now=62)
    assert state["cleanup_error"] == "provider_request_failed"
    assert state["outcome"] is None
    fake.responses.extend([[], resource, resource, None, missing()])
    assert_terminated(reconcile(path, fake, now=63))
    assert fake.methods.count("POST") == 1
    assert fake.methods.count("DELETE") == 3


@pytest.mark.parametrize("storage", ["missing_volume", "volume_disk", "network_volume"])
def test_unproven_storage_profile_is_never_deleted(configured, storage):
    path, owner = configured
    resource = owned_pod(owner)
    if storage == "missing_volume":
        del resource["volumeInGb"]
    elif storage == "volume_disk":
        resource["volumeInGb"] = 20
    else:
        resource["networkVolume"] = {"id": "persistent123", "size": 20}
    fake = FixtureHTTP({"id": "pod123"}, [], resource)
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "ownership_unverified"
    assert state["outcome"] is None
    assert fake.methods == ["POST", "GET", "GET"]


@pytest.mark.parametrize("response", [None, {}, [], {"id": ""}, {"id": "../foreign"}])
def test_invalid_create_receipt_remains_unknown_and_cannot_replay(configured, response):
    path, _ = configured
    fake = FixtureHTTP(response, [])
    state = RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    assert state["phase"] == "create_unknown"
    assert state["outcome"] is None
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM experiment_receipts").fetchone() == (0,)
    with pytest.raises(ValueError, match="already attempted"):
        RESTCleanup(path).create_once(now=2, client=FixtureRunpodREST(fake))
    assert reconcile(path, fake)["cleanup_error"] == "create_unknown"
    assert fake.methods == ["POST", "GET"]


def test_default_create_dry_run_and_real_client_refusal_leave_no_create_intent(
    configured,
):
    path, _ = configured
    state = RESTCleanup(path).create_once(now=1)
    assert state["phase"] == "reserved"
    assert state["create_started"] == 0

    def forbidden_credentials():
        msg = "real credential callback must never be reached"
        raise AssertionError(msg)

    fake = FixtureHTTP()
    real_client = RunpodREST(forbidden_credentials, opener=fake)
    with pytest.raises(ValueError, match="offline fixture"):
        RESTCleanup(path).create_once(now=2, client=real_client)
    state = Experiment(path).status()
    assert state["phase"] == "reserved"
    assert state["create_started"] == 0
    assert fake.methods == []


def test_profile_cannot_be_rebound_before_or_after_restart(configured):
    path, owner = configured
    with pytest.raises(ValueError, match="already bound"):
        RESTCleanup(path).configure_create(
            account="other",
            image="other@sha256:" + "2" * 64,
            container_disk_gb=121,
            gpu_type="other",
            data_center_ids=["EU-RO-1"],
        )
    with pytest.raises(ValueError, match="already bound"):
        RESTCleanup(path).configure(owner)
    fake = FixtureHTTP({"id": "pod123"})
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    assert json.loads(fake.requests[0].data)["imageName"] == owner.image


def test_reconcile_scans_once_and_processes_at_most_ten_resources_per_tick(configured):
    path, owner = configured
    resources = [owned_pod(owner, id=f"pod{number:03d}") for number in range(12)]
    fake = FixtureHTTP(TimeoutError("lost"), resources)
    for resource in resources[:10]:
        fake.responses.extend([resource, resource, None, missing(resource["id"])])
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["outcome"] is None
    assert state["phase"] == "cleanup_pending"
    assert fake.methods.count("DELETE") == 10
    assert (
        sum(
            request.full_url.endswith("/pods?includeNetworkVolume=true")
            for request in fake.requests
        )
        == 1
    )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM rest_resources").fetchone() == (12,)
        assert db.execute(
            "SELECT COUNT(*) FROM rest_resources WHERE compute_absent=1"
        ).fetchone() == (10,)
    fake.responses.append(resources[10:])
    for resource in resources[10:]:
        fake.responses.extend([resource, resource, None, missing(resource["id"])])
    assert_terminated(reconcile(path, fake, now=62))
    deleted = [
        request.full_url for request in fake.requests if request.method == "DELETE"
    ]
    assert len(deleted) == len(set(deleted)) == 12
    assert fake.methods.count("POST") == 1
    assert (
        sum(
            request.full_url.endswith("/pods?includeNetworkVolume=true")
            for request in fake.requests
        )
        == 2
    )
    assert fake.responses == []


@pytest.mark.parametrize("mismatch", ["account", "fingerprint", "network_volume"])
def test_post_delete_mismatched_readback_cannot_keep_storage_proof(
    configured, mismatch
):
    path, owner = configured
    resource = owned_pod(owner)
    changed = owned_pod(owner)
    if mismatch == "account":
        changed["consumerUserId"] = "foreign"
    elif mismatch == "fingerprint":
        changed["env"]["ERGO_ATTEMPT_FINGERPRINT"] = "foreign"
    else:
        changed["networkVolume"] = {"id": "persistent123", "size": 20}
    fake = FixtureHTTP({"id": "pod123"}, [resource], resource, resource, None, changed)
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "ownership_unverified"
    assert state["outcome"] is None
    fake.responses.extend([[], missing()])
    state = reconcile(path, fake, now=62)
    assert state["cleanup_error"] == "storage_unverified"
    assert state["outcome"] is None
    assert fake.methods.count("DELETE") == 1


def test_failed_inventory_scan_still_cleans_known_id_but_cannot_close_tick(configured):
    path, owner = configured
    resource = owned_pod(owner)
    fake = FixtureHTTP(
        {"id": "pod123"},
        TimeoutError("inventory unavailable"),
        resource,
        resource,
        None,
        missing(),
    )
    RESTCleanup(path).create_once(now=1, client=FixtureRunpodREST(fake))
    state = reconcile(path, fake)
    assert state["cleanup_error"] == "provider_request_failed"
    assert state["outcome"] is None
    assert fake.methods == ["POST", "GET", "GET", "GET", "DELETE", "GET"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT compute_absent FROM rest_resources").fetchone() == (
            1,
        )
    fake.responses.append([])
    assert_terminated(reconcile(path, fake, now=62))
    assert fake.methods.count("DELETE") == 1


@pytest.mark.parametrize(
    "image",
    ["image:latest", "image@sha256:123", "image@sha256:" + "g" * 64],
)
def test_create_rejects_mutable_or_invalid_image_reference(reserved, image):
    with pytest.raises(ValueError, match="digest-pinned image"):
        RESTCleanup(reserved).configure_create(
            account="account",
            image=image,
            container_disk_gb=200,
            gpu_type="NVIDIA RTX 6000 Ada Generation",
            data_center_ids=["EU-RO-1"],
        )
    assert Experiment(reserved).status()["create_started"] == 0


@pytest.mark.parametrize(
    "data_centers", [None, [], "EU-RO-1", [""], ["*"], ["EU-RO-1", "EU-RO-1"]]
)
def test_create_requires_explicit_datacenter_allowlist(reserved, data_centers):
    with pytest.raises(ValueError, match="data-center allowlist"):
        RESTCleanup(reserved).configure_create(
            account="account",
            image="image@sha256:" + "1" * 64,
            container_disk_gb=200,
            gpu_type="NVIDIA RTX 6000 Ada Generation",
            data_center_ids=data_centers,
        )
    assert Experiment(reserved).status()["create_started"] == 0


@pytest.mark.parametrize("reserved", [(1700000000, 3600)], indirect=True)
def test_full_hour_create_binds_original_absolute_clock_after_restart(reserved):
    RESTCleanup(reserved).configure_create(
        account="account",
        image="image@sha256:" + "1" * 64,
        container_disk_gb=200,
        gpu_type="NVIDIA RTX 6000 Ada Generation",
        data_center_ids=["EU-RO-1", "EU-SE-1"],
    )
    fake = FixtureHTTP({"id": "pod123"})
    RESTCleanup(reserved).create_once(now=1700000100, client=FixtureRunpodREST(fake))
    body = json.loads(fake.requests[0].data)
    assert float(body["env"]["ERGO_DEADLINE"]) == 1700003600
    assert float(body["env"]["ERGO_DEADLINE"]) - 120 == 1700003480
    assert body["dataCenterIds"] == ["EU-RO-1", "EU-SE-1"]
    assert body["containerDiskInGb"] == 200
    assert body["volumeInGb"] == 0
    assert "networkVolumeId" not in body


@pytest.mark.parametrize("deadline", [0, 3601, float("inf")])
def test_create_rejects_invalid_or_extended_reservation_clock(reserved, deadline):
    with sqlite3.connect(reserved) as db:
        db.execute("UPDATE experiment SET deadline=?", (deadline,))
    with pytest.raises(ValueError, match=r"deadline|finite timestamp"):
        RESTCleanup(reserved).configure_create(
            account="account",
            image="image@sha256:" + "1" * 64,
            container_disk_gb=200,
            gpu_type="NVIDIA RTX 6000 Ada Generation",
            data_center_ids=["EU-RO-1"],
        )
