"""Native tool/Worker lifecycle with in-memory HTTP, never a GPU rental."""

# Fixture media executables have fixed arguments and never access the network.
import io
import json
import sqlite3
import subprocess
import time
import wave
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from django_ergo.bots import workers
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.models import Worker
from django_ergo.plugins.runpod import native
from django_ergo.plugins.runpod import native_h3
from django_ergo.plugins.runpod import runner
from django_ergo.plugins.runpod.experiment import Experiment
from django_ergo.plugins.runpod.frozen import digest
from django_ergo.plugins.runpod.h3 import H3Client
from django_ergo.plugins.runpod.models import RunpodAccount
from django_ergo.plugins.runpod.models import RunpodJob
from django_ergo.plugins.runpod.queue import Conflict
from django_ergo.plugins.runpod.ratebudget import RestBudget
from django_ergo.plugins.runpod.ratebudget import budget_path
from django_ergo.plugins.runpod.transport import FixtureRunpodREST
from django_ergo.plugins.runpod.transport import RateLimitError
from tests.runpod_stock_fixtures import no_capacity
from tests.runpod_stock_fixtures import respond
from tests.runpod_stock_fixtures import stock
from tests.test_bots import make_bot
from tests.test_runpod_controller import config as admission_config  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
STEPS = []


def schedule(worker_id, delay):
    STEPS.append((worker_id, delay))


def too_many(url):
    return HTTPError(url, 429, "rate limited", {"Retry-After": "30"}, None)


class ProviderHTTP:
    def __init__(self):
        self.pods = {}
        self.posts = 0
        self.deletes = []
        self.lose_response = False
        self.fail_delete = False
        self.stock = stock()
        self.refuse = set()
        self.bodies = []
        self.catalog_reads = 0
        self.refuse_after_create = False
        self.rate_limit_after_create = False
        self.rate_limit_delete = False
        # RunPod REST shares one per-key budget across endpoints (observed
        # 11/minute and 225/hour); enforce_limit answers 429 beyond it.
        self.clock = time.time
        self.enforce_limit = False
        self.rest_calls = []
        self.limited = 0
        self.lists = 0

    def open(self, request, timeout):  # noqa: C901, PLR0912 -- one fake REST router
        assert timeout <= 10
        if "/v2/catalog/gpus?" in request.full_url:
            self.catalog_reads += 1
            return respond({"gpus": self.stock})
        route = request.full_url.split("/v1", 1)[1]
        now = self.clock()
        if self.enforce_limit and (
            sum(now - at < 60 for at in self.rest_calls) >= 11
            or sum(now - at < 3600 for at in self.rest_calls) >= 225
        ):
            self.limited += 1
            raise too_many(request.full_url)
        self.rest_calls.append(now)
        if request.method == "POST":
            self.posts += 1
            body = json.loads(request.data)
            self.bodies.append(body)
            if body["dataCenterIds"][0] in self.refuse:
                raise no_capacity(request.full_url)
            pod = {
                "id": "pod123",
                "consumerUserId": "fixture-account",
                "env": body["env"],
                "imageName": body["imageName"],
                "containerDiskInGb": body["containerDiskInGb"],
                "volumeInGb": 0,
                "networkVolume": None,
                "costPerHr": "1.20",
            }
            self.pods[pod["id"]] = pod
            if self.lose_response:
                raise TimeoutError
            if self.refuse_after_create:
                raise no_capacity(request.full_url)
            if self.rate_limit_after_create:
                raise too_many(request.full_url)
            response = {"id": pod["id"]}
        elif request.method == "DELETE":
            if self.fail_delete:
                raise TimeoutError
            if self.rate_limit_delete:
                raise too_many(request.full_url)
            resource = route.rsplit("/", 1)[1]
            self.deletes.append(resource)
            self.pods.pop(resource, None)
            response = None
        elif route.startswith("/pods?"):
            self.lists += 1
            response = list(self.pods.values())
        else:
            resource = route.split("?", 1)[0].rsplit("/", 1)[1]
            if resource not in self.pods:
                raise HTTPError(request.full_url, 404, "fixture absent", {}, None)
            response = self.pods[resource]
        return io.BytesIO(json.dumps(response).encode())


@pytest.fixture
def frozen():
    wav = io.BytesIO()
    with wave.open(wav, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * (8000 * 29))
    shots = [
        {
            "shot": f"MS-{index + 1:02d}",
            "version": "v1",
            "seconds": seconds,
            "prompt": "Fixture-only synthetic scene",
            "prompt_sha256": "a" * 64,
            "image": b"fixture image, not model input",
            "image_sha256": "b" * 64,
            "historical_request": f"FIXTURE-{index}",
            "known_flags": ["synthetic test, not H3 output"],
        }
        for index, seconds in enumerate([5, 5, 5, 5, 5, 4])
    ]
    return shots, wav.getvalue()


@pytest.fixture
def setup(tmp_path, settings, monkeypatch, admission_config, frozen):  # noqa: F811 — pytest fixture injection
    STEPS.clear()
    settings.DJANGO_ERGO = {
        **settings.DJANGO_ERGO,
        "WORKER_RUNNER": "tests.test_runpod_native.schedule",
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
    }
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.RUNPOD_ACCOUNTS = {
        "gpu_jobs": {
            "enabled": True,
            "allowed_users": ["lee"],
            "allowed_bots": ["storytime"],
            "api_key_env": "RUNPOD_FIXTURE_KEY",
            "ledger_root": str(tmp_path / "rentals"),
            "budget_usd": "3",
            "max_concurrent": 1,
            "max_rentals": 1,
            "profiles": {
                "h3_frozen": {
                    "adapter": "frozen_h3",
                    "parts": ["fixture-bound-archive"],
                    "base_url": "http://127.0.0.1:30010",
                    "max_output_bytes": 4_000_000,
                    "config": admission_config,
                }
            },
        }
    }
    monkeypatch.setenv("RUNPOD_FIXTURE_KEY", "dummy-fixture-not-a-provider-key")
    monkeypatch.setattr(native, "read_frozen", lambda parts: frozen)
    monkeypatch.setattr(native_h3, "read_frozen", lambda parts: frozen)
    monkeypatch.setattr(runner, "AUDIO_SHA256", digest(frozen[1]))
    provider = ProviderHTTP()

    def provider_client(binding, root):
        # Rate-limit tests share the real durable budget across roles; others
        # drive many steps in real time and opt out of it.
        budget = None
        if provider.enforce_limit:
            budget = RestBudget(
                budget_path(root, "RUNPOD_FIXTURE_KEY"), clock=lambda: provider.clock()
            )
        return FixtureRunpodREST(provider, budget=budget)

    monkeypatch.setattr(native, "provider_client", provider_client)
    monkeypatch.setattr(native, "validate_private_key", lambda policy, config: "")
    monkeypatch.setattr(
        native_h3,
        "ensure_tunnel",
        lambda job, provider, pod, *, deadline: job.profile["base_url"],
        raising=False,
    )
    bot, _ = make_bot(
        tmp_path,
        name="storytime",
        yaml_text="""
        name: storytime
        plugins:
          - name: runpod
            account: gpu_jobs
            profiles: [h3_frozen]
            root_only: false
    """,
    )
    user = get_user_model().objects.create(username="lee", is_superuser=True)
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    toolkit = bot.plugin("runpod").toolkits(ctx)[0]
    native.reconcile_once()
    return SimpleNamespace(
        bot=bot,
        ctx=ctx,
        tools=toolkit,
        provider=provider,
        policy=settings.RUNPOD_ACCOUNTS["gpu_jobs"],
    )


def invoke(setup, name, **args):
    return json.loads(setup.tools.execute_tool("ergo_runpod_" + name, args))


def begin(setup):
    result = invoke(
        setup, "start", profile="h3_frozen", request_key="approved-comparison"
    )
    job = RunpodJob.objects.get(pk=result["job_id"])
    independent_tick(job)
    return job


def independent_tick(job):
    native.reconcile_once()
    # Models an independently scheduled maintenance process, not watch renewing
    # its own admission. Actual same-PID rejection is covered by supervisor tests.
    with sqlite3.connect(job.ledger_path) as db:
        db.execute("UPDATE cleaner_supervision SET process_id=-1")


def drive(setup, job):
    independent_tick(job)
    return workers.run(str(job.worker_id), setup.bot.registry)


def expire_owned_pod_read(job):
    worker = Worker.objects.get(pk=job.worker_id)
    worker.state["owned_pod"]["next"] = 0
    worker.save(update_fields=["state"])


def test_native_start_is_committed_idempotent_and_scoped(setup):
    assert setup.tools.requires_approval("ergo_runpod_start") is True
    with transaction.atomic():
        job = begin(setup)
        assert STEPS == []
        assert Worker.objects.get(pk=job.worker_id).next_poll_at is not None
    assert [(str(job.worker_id), 0)] == STEPS
    again = invoke(
        setup, "start", profile="h3_frozen", request_key="approved-comparison"
    )
    assert again["job_id"] == str(job.pk)
    assert again["reservation_usd"] == "1.23"
    assert again["actual_usd"] is None
    assert job.deadline - job.created == 3600
    assert setup.provider.posts == 0
    with pytest.raises(ValueError, match="lifetime rental"):
        invoke(setup, "start", profile="h3_frozen", request_key="replacement")
    other = SimpleNamespace(
        bot=SimpleNamespace(name="other"),
        user=setup.ctx.user,
        session=setup.ctx.session,
    )
    with pytest.raises(PermissionError, match="not owned"):
        native.owned(other, "gpu_jobs", job.pk)
    setup.policy["profiles"]["h3_frozen"]["config"]["image"] = (
        "changed@sha256:" + "2" * 64
    )
    with pytest.raises(Conflict, match="different"):
        invoke(setup, "start", profile="h3_frozen", request_key="approved-comparison")


@pytest.mark.parametrize("created", [False, True])
def test_terminal_reservation_release_requires_proven_no_create(setup, created):
    job = begin(setup)
    if created:
        drive(setup, job)
    native.stop(job, "failed")
    independent_tick(job)
    job.refresh_from_db()
    assert job.cleanup_complete
    assert job.reservation_usd == Decimal("1.23")
    if created:
        with pytest.raises(ValueError, match="lifetime rental"):
            invoke(setup, "start", profile="h3_frozen", request_key="new-approval")
        setup.policy["max_rentals"] = 2
    pricing = setup.policy["profiles"]["h3_frozen"]["config"]["pricing"]
    pricing["compute_hourly_usd"]["amount"] = "2.19"
    if created:
        with pytest.raises(ValueError, match="project cap exceeded"):
            invoke(setup, "start", profile="h3_frozen", request_key="new-approval")
        assert RunpodJob.objects.count() == 1
    else:
        result = invoke(setup, "start", profile="h3_frozen", request_key="new-approval")
        assert result["reservation_usd"] == "2.22"
        assert RunpodJob.objects.count() == 2
        assert setup.provider.posts == 0
    job.refresh_from_db()
    assert job.reservation_usd == Decimal("1.23")
    assert Experiment(job.ledger_path).status()["phase"] == "terminated"


def test_operator_cost_review_preserves_reservation_and_lifetime_limit(setup):
    job = begin(setup)
    drive(setup, job)
    native.stop(job, "failed")
    independent_tick(job)
    setup.policy["reconciled_costs"] = {
        str(job.pk): {
            "amount_usd": "0.20",
            "basis": "conservative_elapsed_rate",
            "evidence": "fixture: positive compute/storage absence",
            "observed_at": time.time(),
        }
    }
    with pytest.raises(ValueError, match="lifetime rental"):
        invoke(setup, "start", profile="h3_frozen", request_key="approved-bounded-run")
    setup.policy["max_rentals"] = 2
    pricing = setup.policy["profiles"]["h3_frozen"]["config"]["pricing"]
    pricing["compute_hourly_usd"]["amount"] = "2.19"
    result = invoke(
        setup, "start", profile="h3_frozen", request_key="approved-bounded-run"
    )
    new = RunpodJob.objects.get(pk=result["job_id"])
    assert new.profile["config"]["pricing"]["project_committed_usd"] == "0.20"
    job.refresh_from_db()
    assert job.reservation_usd == Decimal("1.23")
    assert Experiment(job.ledger_path).status()["create_started"] == 1
    assert setup.provider.posts == 1


def test_budget_counts_only_created_rentals_against_policy_cap(setup):
    created = begin(setup)
    drive(setup, created)
    native.stop(created, "failed")
    independent_tick(created)
    setup.policy["reconciled_costs"] = {
        str(created.pk): {
            "amount_usd": "1.00",
            "basis": "conservative_elapsed_rate",
            "evidence": "fixture: positive compute/storage absence",
            "observed_at": time.time(),
        }
    }
    setup.policy["max_rentals"] = 3
    # A lost-worker attempt cleaned up before any create, its ledger left mid-phase.
    lost = RunpodJob.objects.get(
        pk=invoke(setup, "start", profile="h3_frozen", request_key="lost")["job_id"]
    )
    RunpodJob.objects.filter(pk=lost.pk).update(cleanup_complete=True)
    assert Experiment(lost.ledger_path).status()["phase"] != "terminated"
    pricing = setup.policy["profiles"]["h3_frozen"]["config"]["pricing"]
    pricing["compute_hourly_usd"]["amount"] = "2.19"
    with pytest.raises(ValueError, match="project cap exceeded"):
        invoke(setup, "start", profile="h3_frozen", request_key="above-three")
    setup.policy["budget_usd"] = "6"
    result = invoke(setup, "start", profile="h3_frozen", request_key="above-three")
    config = RunpodJob.objects.get(pk=result["job_id"]).profile["config"]
    assert config["pricing"]["project_committed_usd"] == "1.00"
    assert config["pricing"]["project_cap_usd"] == "6"
    assert result["reservation_usd"] == "2.22"


def test_cleaned_zero_create_rows_do_not_consume_rental_slots(setup):
    first = begin(setup)
    native.stop(first, "failed")
    independent_tick(first)
    # Even a ledger interrupted before reaching a terminal phase has no rental
    # once independent cleanup proves no POST occurred.
    with Experiment(first.ledger_path)._tx() as db:
        db.execute("UPDATE experiment SET phase='reserved' WHERE id=1")
    second = RunpodJob.objects.get(
        pk=invoke(setup, "start", profile="h3_frozen", request_key="second")["job_id"]
    )
    assert second.profile["config"]["pricing"]["project_committed_usd"] == "0"
    assert setup.provider.posts == 0
    with pytest.raises(ValueError, match="lifetime rental"):
        invoke(setup, "start", profile="h3_frozen", request_key="concurrent")
    assert RunpodJob.objects.count() == 2


@pytest.mark.parametrize(
    "invalid",
    [
        {"amount_usd": "-0.01"},
        {"amount_usd": "NaN"},
        {"basis": "unverified"},
        {"observed_at": float("inf")},
    ],
)
def test_invalid_operator_cost_review_blocks_admission(setup, invalid):
    job = begin(setup)
    drive(setup, job)
    native.stop(job, "failed")
    independent_tick(job)
    setup.policy["max_rentals"] = 2
    setup.policy["reconciled_costs"] = {
        str(job.pk): {
            "amount_usd": "0.20",
            "basis": "conservative_elapsed_rate",
            "evidence": "fixture: positive compute/storage absence",
            "observed_at": time.time(),
            **invalid,
        }
    }
    with pytest.raises(ValueError, match="Invalid reconciled Runpod"):
        invoke(setup, "start", profile="h3_frozen", request_key="invalid-review")
    assert RunpodJob.objects.count() == 1
    assert setup.provider.posts == 1


def test_operator_cost_review_cannot_release_active_rental(setup):
    job = begin(setup)
    drive(setup, job)
    setup.policy.update(max_rentals=2, max_concurrent=2)
    setup.policy["reconciled_costs"] = {
        str(job.pk): {
            "amount_usd": "0.20",
            "basis": "conservative_elapsed_rate",
            "evidence": "fixture: not yet cleaned",
            "observed_at": time.time(),
        }
    }
    pricing = setup.policy["profiles"]["h3_frozen"]["config"]["pricing"]
    pricing["compute_hourly_usd"]["amount"] = "2.19"
    with pytest.raises(ValueError, match="project cap exceeded"):
        invoke(setup, "start", profile="h3_frozen", request_key="active-review")
    assert RunpodJob.objects.count() == 1


@pytest.mark.parametrize(
    "gate", ["principal", "bot", "budget", "hardware", "maintenance"]
)
def test_admission_denies_before_provider_or_worker(setup, gate):
    if gate == "principal":
        setup.ctx.user.is_superuser = False
    elif gate == "bot":
        setup.policy["allowed_bots"] = ["different"]
    elif gate == "budget":
        setup.policy["budget_usd"] = "1.00"
    elif gate == "hardware":
        setup.policy["profiles"]["h3_frozen"]["config"]["hardware"]["gpus"] = []
    else:
        RunpodAccount.objects.update(maintenance_at=0)
    with pytest.raises((PermissionError, ValueError)):
        invoke(setup, "start", profile="h3_frozen", request_key="denied")
    assert setup.provider.posts == 0
    assert not Worker.objects.exists()
    assert not RunpodJob.objects.exists()


def test_cancelled_worker_cleanup_survives_removed_config_and_chat(setup, settings):
    job = begin(setup)
    assert drive(setup, job) == "running"
    workers.cancel(job.worker)
    settings.RUNPOD_ACCOUNTS = {}
    setup.ctx.session.delete()
    native.reconcile_once()
    job.refresh_from_db()
    assert job.session_id is None and job.worker_id is None
    assert Experiment(job.ledger_path).status()["stop_reason"] == "worker_lost"
    assert job.cleanup_complete
    assert setup.provider.deletes == ["pod123"]


@pytest.mark.parametrize(
    "revocation", ["disabled", "malformed", "missing", "not_mapping"]
)
def test_live_policy_file_revokes_admission_without_losing_cleanup(
    setup, settings, tmp_path, revocation
):
    path = tmp_path / "accounts.json"
    settings.RUNPOD_ACCOUNTS_FILE = str(path)
    policy = json.loads(json.dumps(settings.RUNPOD_ACCOUNTS))
    policy["gpu_jobs"]["enabled"] = False
    path.write_text(json.dumps(policy))
    with pytest.raises(PermissionError, match="not authorized"):
        invoke(setup, "start", profile="h3_frozen", request_key="approved-comparison")
    assert not RunpodJob.objects.exists()
    policy["gpu_jobs"]["enabled"] = True
    path.write_text(json.dumps(policy))
    job = begin(setup)
    assert drive(setup, job) == "running"
    if revocation == "disabled":
        policy["gpu_jobs"]["enabled"] = False
        path.write_text(json.dumps(policy))
    elif revocation == "malformed":
        path.write_text("{")
    elif revocation == "missing":
        path.unlink()
    else:
        path.write_text("[]")
    # The enabled in-memory policy must not become a fallback after file loss.
    with pytest.raises((PermissionError, ValueError, TypeError)):
        invoke(setup, "start", profile="h3_frozen", request_key="replacement")
    native.reconcile_once()
    job.refresh_from_db()
    assert job.cleanup_complete
    assert Experiment(job.ledger_path).status()["stop_reason"] == "worker_lost"
    assert setup.provider.posts == 1
    assert setup.provider.deletes == ["pod123"]


def test_lost_create_restart_and_late_resource_never_replays(setup):
    job = begin(setup)
    setup.provider.lose_response = True
    assert drive(setup, job) == "running"
    assert Experiment(job.ledger_path).status()["create_started"] == 1
    # The watcher can disappear, and even an empty inventory must retain debt.
    late = setup.provider.pods.pop("pod123")
    job.worker.delete()
    native.reconcile_once()
    assert Experiment(job.ledger_path).status()["cleanup_error"] == "create_unknown"
    setup.provider.pods["pod123"] = late
    native.reconcile_once()
    job.refresh_from_db()
    assert job.cleanup_complete
    assert setup.provider.posts == 1
    assert setup.provider.deletes == ["pod123"]


def test_absolute_deadline_cleanup_retries_without_worker(setup):
    job = begin(setup)
    assert drive(setup, job) == "running"
    setup.provider.fail_delete = True
    native.reconcile_once(now=job.deadline + 1)
    assert (
        Experiment(job.ledger_path).status()["cleanup_error"]
        == "provider_request_failed"
    )
    assert Experiment(job.ledger_path).status()["stop_reason"] == "deadline"
    setup.provider.fail_delete = False
    native.reconcile_once(now=job.deadline + 2)
    job.refresh_from_db()
    assert job.cleanup_complete
    assert setup.provider.posts == 1


def test_worker_cancel_during_step_cannot_be_overwritten(setup, monkeypatch):
    job = begin(setup)

    def cancel_in_step(ctx, current):
        workers.cancel(ctx.worker)
        return "late progress after cancellation"

    monkeypatch.setattr(native_h3, "step", cancel_in_step)
    assert drive(setup, job) == "cancelled"
    job.worker.refresh_from_db()
    assert job.worker.status == "cancelled"
    native.reconcile_once()
    assert Experiment(job.ledger_path).status()["phase"] == "terminated"


def test_native_readiness_question_and_failure_are_visible(setup, monkeypatch):
    job = begin(setup)
    drive(setup, job)

    def unavailable(*args, **kwargs):
        raise OSError

    monkeypatch.setattr(
        native_h3, "H3Client", lambda base: SimpleNamespace(call=unavailable)
    )
    assert drive(setup, job) == "running"
    job.worker.refresh_from_db()
    assert "readiness" in job.worker.progress
    assert (
        ThreadMessage.objects.filter(
            metadata__worker=str(job.worker_id), metadata__update=True
        ).count()
        == 1
    )
    assert drive(setup, job) == "running"
    assert (
        ThreadMessage.objects.filter(
            metadata__worker=str(job.worker_id), metadata__update=True
        ).count()
        == 1
    )
    # The rate is re-read once the throttled owned-Pod interval has elapsed.
    expire_owned_pod_read(job)
    setup.provider.pods["pod123"]["costPerHr"] = "99"
    assert drive(setup, job) == "failed"
    job.worker.refresh_from_db()
    assert "cleanup confirmed" in job.worker.error
    assert setup.provider.deletes == ["pod123"]


def test_real_media_native_worker_completion_and_idempotent_collect(
    setup, tmp_path, monkeypatch, frozen
):
    videos = {}
    for seconds, frames in [(5, 124), (4, 107)]:
        path = tmp_path / f"synthetic-{seconds}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=768x1344:r=24",
                "-frames:v",
                str(frames),
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
        videos[seconds] = path.read_bytes()
    submissions = {}

    def h3_http(request, timeout):
        route = request.full_url.split(":30010", 1)[1]
        if request.method == "POST":
            body = json.loads(request.data)
            receipt = "video" + str(len(submissions))
            submissions[receipt] = body["seconds"]
            result = {"id": receipt}
        elif route == "/health":
            result = {"status": "ok"}
        elif route.endswith("/content"):
            return io.BytesIO(videos[submissions[route.split("/")[-2]]])
        else:
            result = {"status": "completed"}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr(
        native_h3, "H3Client", lambda base: H3Client(base, opener=h3_http)
    )
    job = begin(setup)
    states = []
    for _ in range(12):
        states.append(drive(setup, job))
        if states[-1] != "running":
            break
    job.refresh_from_db()
    job.worker.refresh_from_db()
    assert states[-1] == "completed", job.worker.error
    assert setup.provider.posts == 1 and len(submissions) == 6
    result = job.worker.result
    assert result["cleanup_complete"] is True
    assert result["manifest"]["complete"] is True
    provenance = [json.loads(line) for line in result["generations_jsonl"].splitlines()]
    assert [row["shot"] for row in provenance] == [shot["shot"] for shot in frozen[0]]
    assert all("est_usd" not in row and "actual_usd" not in row for row in provenance)
    assert [row["index"] for row in result["manifest"]["items"]] == list(range(6))
    assert result["manifest"]["assembly"]["frames"] == 696
    assert result["manifest"]["assembly"]["approved_audio"]["sha256"] == digest(
        frozen[1]
    )
    assert ThreadMessage.objects.filter(
        metadata__worker=str(job.worker_id), metadata__worker_finished=True
    ).exists()
    first = invoke(setup, "collect", job_id=str(job.pk))
    second = invoke(setup, "collect", job_id=str(job.pk))
    assert first["attachments"] == second["attachments"]
    assert (
        ConversationAttachment.objects.filter(metadata__runpod_job=str(job.pk)).count()
        == 15
    )
    assert setup.provider.deletes == ["pod123"]


def test_cancel_before_create_retains_all_original_positions(setup):
    job = begin(setup)
    invoke(setup, "terminate", job_id=str(job.pk))
    job.refresh_from_db()
    result = invoke(setup, "collect", job_id=str(job.pk))
    assert setup.provider.posts == 0
    assert result["cleanup_complete"] is True
    assert [item["status"] for item in result["manifest"]["items"]] == ["cancelled"] * 6
    assert result["manifest"]["complete"] is False


def test_expired_worker_claim_cannot_overwrite_successor(setup, monkeypatch):
    from django.utils import timezone

    job = begin(setup)

    def successor_claim(ctx, current):
        Worker.objects.filter(pk=ctx.worker.pk).update(
            step_started_at=timezone.now() + timezone.timedelta(seconds=1),
            state={"successor": "new owner"},
            progress="new owner's progress",
        )
        ctx.state = {"obsolete": "old owner"}
        return ctx.again(5, progress="obsolete progress")

    monkeypatch.setattr(native_h3, "step", successor_claim)
    assert drive(setup, job) == "running"
    job.worker.refresh_from_db()
    assert job.worker.state == {"successor": "new owner"}
    assert job.worker.progress == "new owner's progress"


def test_rolled_back_admission_publishes_nothing(setup):
    def abort_admission():
        with transaction.atomic():
            begin(setup)
            msg = "rollback"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="rollback"):
        abort_admission()
    assert STEPS == []
    assert not Worker.objects.exists()
    assert not RunpodJob.objects.exists()
    assert setup.provider.posts == 0


def test_collect_recovers_storage_save_before_transaction_commit(
    setup, monkeypatch, tmp_path
):
    job = begin(setup)
    experiment = Experiment(job.ledger_path)
    experiment.export(
        now=time.time(),
        name="partial.txt",
        content=b"retained partial result",
        metadata={"item_index": 0, "partial": True},
        retained=True,
    )
    original = native._attachment

    def interrupted(job, entry):
        original(job, entry)
        msg = "process interrupted after storage save"
        raise RuntimeError(msg)

    monkeypatch.setattr(native, "_attachment", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        invoke(setup, "collect", job_id=str(job.pk))
    assert not ConversationAttachment.objects.filter(
        metadata__runpod_job=str(job.pk)
    ).exists()
    monkeypatch.setattr(native, "_attachment", original)
    recovered = invoke(setup, "collect", job_id=str(job.pk))
    assert (
        recovered["attachments"]
        == invoke(setup, "collect", job_id=str(job.pk))["attachments"]
    )
    [attachment] = ConversationAttachment.objects.filter(
        metadata__runpod_job=str(job.pk)
    )
    with attachment.file.open("rb") as stream:
        assert stream.read() == b"retained partial result"
    files = [path for path in (tmp_path / "media").rglob("*") if path.is_file()]
    assert len(files) == 1


def test_new_worker_cannot_self_admit_without_independent_heartbeat(setup):
    accepted = invoke(
        setup, "start", profile="h3_frozen", request_key="wait-for-cleaner"
    )
    job = RunpodJob.objects.get(pk=accepted["job_id"])
    assert workers.run(str(job.worker_id), setup.bot.registry) == "running"
    assert workers.run(str(job.worker_id), setup.bot.registry) == "running"
    assert setup.provider.posts == 0
    job.worker.refresh_from_db()
    assert "independent executing cleaner" in job.worker.progress
    assert drive(setup, job) == "running"
    assert setup.provider.posts == 1


def test_concurrent_intents_share_one_locked_installation_budget(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connection

    barrier = Barrier(2)

    def submit(key):
        try:
            barrier.wait(timeout=5)
            return invoke(setup, "start", profile="h3_frozen", request_key=key)
        except ValueError as error:
            return error
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["intent-a", "intent-b"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert RunpodJob.objects.count() == Worker.objects.count() == 1
    assert RunpodJob.objects.get().reservation_usd == Decimal("1.23")
    assert setup.provider.posts == 0


def test_independent_maintenance_stops_stale_process_with_active_worker_row(setup):
    from django.utils import timezone

    job = begin(setup)
    assert drive(setup, job) == "running"
    Worker.objects.filter(pk=job.worker_id).update(
        updated_at=timezone.now()
        - timezone.timedelta(seconds=native.OWNER_STALE_SECONDS + 1),
    )
    native.reconcile_once()
    job.refresh_from_db()
    assert job.worker.status == "running"
    assert job.cleanup_complete
    assert Experiment(job.ledger_path).status()["stop_reason"] == "worker_lost"
    assert setup.provider.posts == 1 and setup.provider.deletes == ["pod123"]


def test_cancel_during_readiness_suppresses_late_question(setup, monkeypatch):
    job = begin(setup)
    assert drive(setup, job) == "running"

    def cancelled_health(*args, **kwargs):
        workers.cancel(job.worker)
        return {"status": "not-ready"}

    monkeypatch.setattr(
        native_h3, "H3Client", lambda base: SimpleNamespace(call=cancelled_health)
    )
    assert drive(setup, job) == "cancelled"
    assert not ThreadMessage.objects.filter(
        metadata__worker=str(job.worker_id),
        metadata__update=True,
    ).exists()
    native.reconcile_once()
    job.refresh_from_db()
    assert job.cleanup_complete


@pytest.mark.parametrize("local_state_lost", [False, True])
@pytest.mark.parametrize("diagnostic_write_fails", [False, True])
def test_launcher_failure_stops_worker_before_startup_deadline(
    setup, monkeypatch, local_state_lost, diagnostic_write_fails
):
    from django_ergo.plugins.runpod.native_tunnel import LauncherFailed

    job = begin(setup)
    assert drive(setup, job) == "running"
    if local_state_lost:
        from django_ergo.plugins.runpod import native_tunnel

        def inaccessible(job):
            message = "fixture filesystem unavailable"
            raise PermissionError(message)

        monkeypatch.setattr(native_tunnel, "_directory", inaccessible)

    marker = {
        "state": "failed",
        "stage": "startup",
        "returncode": 42,
        "stderr_tail": "Traceback:\nRuntimeError: checkpoint component failed\n",
    }
    if diagnostic_write_fails:
        export = Experiment.export

        def fail_diagnostic(self, **kwargs):
            if kwargs["name"] == "launcher-failure.json":
                message = "fixture diagnostic disk unavailable"
                raise OSError(message)
            return export(self, **kwargs)

        monkeypatch.setattr(Experiment, "export", fail_diagnostic)

    def failed_launcher(*args, **kwargs):
        message = "H3 launcher failed"
        raise LauncherFailed(message, marker=marker)

    monkeypatch.setattr(native_h3, "ensure_tunnel", failed_launcher)
    startup_deadline = (
        job.created
        + job.profile["config"]["timeline"]["startup_seconds"]
        + job.profile["config"]["timeline"]["download_seconds"]
    )
    assert time.time() < startup_deadline
    assert drive(setup, job) == "failed"
    job.refresh_from_db()
    assert job.error == "LauncherFailed"
    assert job.cleanup_complete
    assert Experiment(job.ledger_path).status()["stop_reason"] == "failed"
    assert setup.provider.posts == 1
    assert setup.provider.deletes == ["pod123"]
    result = invoke(setup, "collect", job_id=str(job.pk))
    diagnostics = [
        item
        for item in result["attachments"]
        if item["name"] == "launcher-failure.json"
    ]
    if diagnostic_write_fails:
        assert diagnostics == []
    else:
        attachment = ConversationAttachment.objects.get(
            pk=diagnostics[0]["attachment_id"]
        )
        with attachment.file.open("rb") as stream:
            retained = json.load(stream)
        assert retained["returncode"] == 42
        assert retained["stderr_tail"] == marker["stderr_tail"]
    assert setup.provider.pods == {}  # diagnostics never keep paid compute alive


def attempts(setup, job):
    status = invoke(setup, "status", job_id=str(job.pk))
    return status, [
        (row["data_center_id"], row["outcome"]) for row in status["offer_attempts"]
    ]


def test_no_capacity_falls_back_to_next_offer_in_the_same_admission(setup):
    setup.provider.refuse = {"EU-RO-1"}
    job = begin(setup)
    assert drive(setup, job) == "running"
    assert [body["dataCenterIds"] for body in setup.provider.bodies] == [
        ["EU-RO-1"],
        ["US-KS-2"],
    ]
    status, history = attempts(setup, job)
    assert history == [("EU-RO-1", "no_capacity"), ("US-KS-2", "created")]
    assert status["offer"]["data_center_id"] == "US-KS-2"
    assert status["offer"]["hourly_usd"] == "1.19"
    assert status["create_started"] == 1
    # The refused request stays in cleanup's ownership scan; the real Pod is
    # terminated and nothing needs manual ledger repair.
    invoke(setup, "terminate", job_id=str(job.pk))
    job.refresh_from_db()
    assert job.cleanup_complete
    assert setup.provider.deletes == ["pod123"]


def test_late_pod_from_a_refused_offer_is_terminated_while_the_job_runs(setup):
    setup.provider.refuse = {"EU-RO-1"}
    job = begin(setup)
    assert drive(setup, job) == "running"
    refused = setup.provider.bodies[0]
    # The provider materializes the refused request after the inventory check.
    setup.provider.pods["late456"] = {
        **setup.provider.pods["pod123"],
        "id": "late456",
        "env": refused["env"],
    }
    native.reconcile_once()
    assert setup.provider.deletes == ["late456"]
    assert "pod123" in setup.provider.pods
    assert Experiment(job.ledger_path).status()["stop_reason"] is None


def test_maintenance_lists_the_account_once_per_pass(setup):
    setup.provider.refuse = {"EU-RO-1"}
    job = begin(setup)
    assert drive(setup, job) == "running"
    invoke(setup, "terminate", job_id=str(job.pk))
    job.refresh_from_db()
    assert job.cleanup_complete
    # A stopped ledger with a retired (refused) request needs both the late-Pod
    # sweep and the liability scan; they share one Pod listing per pass.
    setup.provider.lists = 0
    native.reconcile_once()
    assert setup.provider.lists == 1


def test_generation_hour_stays_inside_the_provider_rate_limit(setup, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    # Worker heartbeats (updated_at) follow the same simulated clock.
    aware = timezone.is_aware(timezone.now())

    def now():
        moment = datetime.fromtimestamp(clock[0], tz=UTC)
        return moment if aware else timezone.make_naive(moment)

    monkeypatch.setattr(timezone, "now", now)
    setup.provider.clock = lambda: clock[0]
    setup.provider.enforce_limit = True

    def h3_http(request, timeout):
        if request.method == "POST":
            return io.BytesIO(json.dumps({"id": "video0"}).encode())
        route = request.full_url.split(":30010", 1)[1]
        status = {"status": "ok"} if route == "/health" else {"status": "in_progress"}
        return io.BytesIO(json.dumps(status).encode())

    monkeypatch.setattr(
        native_h3, "H3Client", lambda base: H3Client(base, opener=h3_http)
    )
    job = begin(setup)
    # Maintenance every minute and the Worker every 5s, up to the work cutoff.
    for step in range(55 * 12):
        if step % 12 == 0:
            independent_tick(job)
        assert workers.run(str(job.worker_id), setup.bot.registry) == "running"
        clock[0] += 5
    assert setup.provider.limited == 0
    assert Experiment(job.ledger_path).status()["stop_reason"] is None
    assert len(setup.provider.rest_calls) <= 225


def test_refusals_everywhere_create_nothing_and_release_the_rental_slot(setup):
    setup.provider.refuse = {"EU-RO-1", "US-KS-2", "US-WA-1"}
    job = begin(setup)
    assert drive(setup, job) == "running"
    assert setup.provider.posts == 3
    status, history = attempts(setup, job)
    assert [outcome for _, outcome in history] == ["no_capacity"] * 3
    assert status["create_started"] == 0
    assert status["phase"] == "reserved"
    job.worker.refresh_from_db()
    assert "No stock matching profile yet" in job.worker.progress
    invoke(setup, "terminate", job_id=str(job.pk))
    job.refresh_from_db()
    assert job.cleanup_complete
    # Zero-create attempts cost nothing and do not burn max_rentals=1.
    second = invoke(setup, "start", profile="h3_frozen", request_key="after-refusals")
    config = RunpodJob.objects.get(pk=second["job_id"]).profile["config"]
    assert config["pricing"]["project_committed_usd"] == "0"


@pytest.mark.parametrize(
    "failure", ["timeout", "refused_but_created", "rate_limited_after_create"]
)
def test_uncertain_create_is_reconciled_never_retried_elsewhere(setup, failure):
    if failure == "timeout":
        setup.provider.lose_response = True
    elif failure == "refused_but_created":
        setup.provider.refuse_after_create = True
    else:
        # A 429 on POST is ambiguous too: never a no-capacity refusal or a replay.
        setup.provider.rate_limit_after_create = True
    job = begin(setup)
    assert drive(setup, job) == "running"
    for _ in range(2):
        drive(setup, job)
    assert setup.provider.posts == 1
    status, history = attempts(setup, job)
    assert history == [("EU-RO-1", "uncertain")]
    assert status["create_started"] == 1
    invoke(setup, "terminate", job_id=str(job.pk))
    job.refresh_from_db()
    assert job.cleanup_complete
    assert setup.provider.deletes == ["pod123"]


def test_budget_refusal_at_create_sends_nothing_and_waits(setup, monkeypatch):
    message = "provider request budget exhausted"
    refusals = [RateLimitError(message, 30)]
    real = FixtureRunpodREST.reserve

    def reserve(client):
        if refusals:
            raise refusals.pop()
        return real(client)

    monkeypatch.setattr(FixtureRunpodREST, "reserve", reserve)
    job = begin(setup)
    assert drive(setup, job) == "running"
    assert setup.provider.posts == 0
    status, history = attempts(setup, job)
    assert history == [("EU-RO-1", "deferred")]
    assert status["create_started"] == 0
    assert "create deferred" in Worker.objects.get(pk=job.worker_id).progress
    # The next stock poll retires the unsent bind and creates exactly once.
    age_stock_watch(job, 120, first=1)
    assert drive(setup, job) == "running"
    assert setup.provider.posts == 1
    assert attempts(setup, job)[1][-1] == ("EU-RO-1", "created")


def test_rate_limited_delete_stays_pending_and_maintenance_retries_it(setup):
    job = begin(setup)
    assert drive(setup, job) == "running"
    setup.provider.rate_limit_delete = True
    invoke(setup, "terminate", job_id=str(job.pk))
    job.refresh_from_db()
    assert not job.cleanup_complete
    assert "pod123" in setup.provider.pods
    setup.provider.rate_limit_delete = False
    independent_tick(job)
    job.refresh_from_db()
    assert job.cleanup_complete
    assert setup.provider.deletes == ["pod123"]


def test_rate_limited_owned_pod_read_waits_instead_of_failing(setup, monkeypatch):
    job = begin(setup)
    assert drive(setup, job) == "running"
    assert drive(setup, job) == "running"

    def limited(job, provider):
        message = "provider rate limit exceeded"
        raise RateLimitError(message, 30)

    monkeypatch.setattr(native_h3, "_owned_pod", limited)
    expire_owned_pod_read(job)
    # A cached Pod keeps the Worker going; the next read honors Retry-After.
    assert drive(setup, job) == "running"
    state = Worker.objects.get(pk=job.worker_id).state["owned_pod"]
    assert state["pod"]["id"] == "pod123"
    assert 0 < state["next"] - time.time() <= 30
    assert Experiment(job.ledger_path).status()["stop_reason"] is None


def age_stock_watch(job, seconds, first=0):
    with sqlite3.connect(job.ledger_path) as db:
        db.execute(
            "UPDATE stock_watch SET last_poll=last_poll-?, first_poll=first_poll-?",
            (seconds, first or seconds),
        )


def test_waits_for_stock_on_the_poll_interval_without_spending_the_rental(setup):
    for gpu in setup.provider.stock:
        gpu["availability"] = "NONE"
        gpu.pop("dataCenters", None)
    job = begin(setup)
    assert drive(setup, job) == "running"
    assert drive(setup, job) == "running"
    assert setup.provider.catalog_reads == 1
    job.worker.refresh_from_db()
    assert "Waiting for stock matching profile" in job.worker.progress
    reserved = Experiment(job.ledger_path).status()
    age_stock_watch(job, 31)
    setup.provider.stock = stock()
    assert drive(setup, job) == "running"
    assert setup.provider.catalog_reads == 2
    assert setup.provider.posts == 1
    created = Experiment(job.ledger_path).status()
    # The rental clock restarts at launch, keeping the full approved interval.
    assert created["created"] > reserved["created"]
    assert created["deadline"] - created["created"] == pytest.approx(3600)
    job.refresh_from_db()
    assert job.deadline == created["deadline"]


def test_stock_deadline_fails_cleanly_listing_what_was_seen(setup):
    setup.provider.stock = [
        gpu for gpu in setup.provider.stock if gpu["id"] != "NVIDIA A100-SXM4-80GB"
    ]
    job = begin(setup)
    assert drive(setup, job) == "running"
    age_stock_watch(job, 31, first=121)
    assert drive(setup, job) == "failed"
    job.refresh_from_db()
    job.worker.refresh_from_db()
    assert job.error == "no_stock"
    assert "no_stock" in job.worker.error and "cleanup confirmed" in job.worker.error
    assert job.cleanup_complete
    status = invoke(setup, "status", job_id=str(job.pk))
    assert status["outcome"] == "no_stock"
    assert status["stock"]["polls"] == 2
    assert status["stock"]["seen"]["gpus"][0]["reason"] == (
        "$2.69/h is above the $1.20/h ceiling"
    )
    assert setup.provider.posts == 0


def test_gpu_and_region_change_is_a_policy_edit(setup):
    hardware = setup.policy["profiles"]["h3_frozen"]["config"]["hardware"]
    hardware["gpus"] = ["NVIDIA RTX PRO 6000 Blackwell Server Edition"]
    hardware["compute_capabilities"] = ["12.0"]
    hardware["exclude_data_centers"] = ["US-MD-1"]
    preview = invoke(setup, "stock", profile="h3_frozen")
    assert preview["would_launch"]["data_center_id"] == "EU-RO-1"
    job = begin(setup)
    drive(setup, job)
    [body] = setup.provider.bodies
    assert body["gpuTypeIds"] == ["NVIDIA RTX PRO 6000 Blackwell Server Edition"]
    assert body["dataCenterIds"] == ["EU-RO-1"]
    assert body["minRAMPerGPU"] == 167 and body["cloudType"] == "SECURE"
