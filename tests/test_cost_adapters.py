"""Offline fixtures for the passive source adapters, window projection and coverage."""

# Naive datetimes follow the test settings (USE_TZ off); ValueError is the contract.
# ruff: noqa: DTZ001, PT011

import uuid
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import InferenceOutcome
from django_ergo.conversation.models import InferenceRequest
from django_ergo.costs.adapters import record_inference_request
from django_ergo.costs.coverage import coverage_manifest
from django_ergo.costs.ledger import effective_observations
from django_ergo.costs.ledger import record_observation
from django_ergo.costs.ledger import summarize_costs
from django_ergo.costs.models import CostObservation
from django_ergo.costs.types import CostConflictError
from django_ergo.costs.types import CostKind
from django_ergo.costs.types import ObservationSpec
from django_ergo.costs.types import OperationSpec
from django_ergo.plugins.cost_runpod import estimate_amount
from django_ergo.plugins.cost_runpod import record_component
from django_ergo.plugins.cost_runpod import record_job_reservation
from django_ergo.plugins.runpod.models import RunpodAccount
from django_ergo.plugins.runpod.models import RunpodJob

D = Decimal
T0 = datetime(2026, 11, 10, 12)
WINDOW = (datetime(2026, 11, 1), datetime(2026, 11, 13, 23, 59))


@pytest.fixture
def users(db):
    model = get_user_model()
    return model.objects.create_user("a"), model.objects.create_user("b")


def request_row(user, **fields):
    defaults = {
        "user": user,
        "bot_name": "kitchen",
        "selector": "heavy",
        "pool": "pool-1",
        "provider": "openai",
        "model": "gpt-6-luna",
        "reserved_usd": D("0.5"),
    }
    return InferenceRequest.objects.create(**{**defaults, **fields})


def finished(user, **fields):
    return request_row(
        user,
        outcome=InferenceOutcome.OK,
        cost_status="priced",
        cost_usd=D("0.1"),
        prompt_tokens=100,
        cached_tokens=40,
        output_tokens=10,
        reasoning_tokens=3,
        finished_at=T0,
        **fields,
    )


def test_inference_request_maps_to_separate_reservation_and_estimate(users):
    a, _ = users
    rows = record_inference_request(actor=a, request=finished(a))
    assert [r.kind for r, _ in rows] == ["reservation", "estimate"]
    estimate = rows[1][0]
    assert (estimate.tokens_uncached_input, estimate.tokens_cache_read) == (None, 40)
    (effective,) = effective_observations(a)
    assert effective.kind == "estimate"
    kinds = {b["kind"]: b["known_amount"] for b in summarize_costs(a)}
    assert kinds == {"estimate": D("0.1"), "reservation": D("0.5")}


def test_inference_replay_is_idempotent_and_changed_facts_conflict(users):
    a, _ = users
    req = finished(a)
    record_inference_request(actor=a, request=req)
    assert not any(c for _, c in record_inference_request(actor=a, request=req))
    req.cost_usd = D("0.2")
    with pytest.raises(CostConflictError):
        record_inference_request(actor=a, request=req)
    assert CostObservation.objects.count() == 2


def test_inference_conflict_rolls_back_new_reservation(users):
    a, _ = users
    req = finished(a)
    req.reserved_usd = None
    record_inference_request(actor=a, request=req)
    req.reserved_usd = D("0.5")
    req.cost_usd = D("0.2")
    with pytest.raises(CostConflictError):
        record_inference_request(actor=a, request=req)
    assert list(CostObservation.objects.values_list("kind", flat=True)) == ["estimate"]


@pytest.mark.parametrize(
    ("prompt", "cached"), [(None, None), (100, None), (None, 40), (100, 40)]
)
def test_inference_preserves_unknown_token_buckets(users, prompt, cached):
    a, _ = users
    req = finished(a)
    req.prompt_tokens, req.cached_tokens = prompt, cached
    estimate = record_inference_request(actor=a, request=req)[1][0]
    assert estimate.tokens_uncached_input is None
    assert estimate.tokens_cache_write is None
    assert estimate.tokens_cache_read == cached


def test_inference_rejects_cache_read_exceeding_prompt(users):
    a, _ = users
    req = finished(a)
    req.prompt_tokens, req.cached_tokens = 10, 11
    with pytest.raises(ValueError):
        record_inference_request(actor=a, request=req)
    assert not CostObservation.objects.exists()


@pytest.mark.parametrize(
    ("status", "amount"),
    [("unknown", D("0.1")), ("pending", D("0.1")), ("priced", None)],
)
def test_inference_cost_requires_priced_status_and_amount(users, status, amount):
    a, _ = users
    req = finished(a)
    req.cost_status, req.cost_usd, req.finished_at = status, amount, None
    observation = record_inference_request(actor=a, request=req)[1][0]
    assert observation.kind == "unknown"
    assert observation.amount is None
    assert observation.occurred_at is None


def test_unpriced_finished_request_is_unknown_not_zero_and_pending_has_only_reservation(
    users,
):
    a, _ = users
    pending = request_row(a)
    assert [r.kind for r, _ in record_inference_request(actor=a, request=pending)] == [
        "reservation"
    ]
    unpriced = request_row(
        a, outcome=InferenceOutcome.ERROR, cost_status="unknown", finished_at=T0
    )
    record_inference_request(actor=a, request=unpriced)
    bucket = next(b for b in summarize_costs(a) if b["kind"] == "unknown")
    assert bucket["known_amount"] is None
    assert (bucket["unknown_count"], bucket["known_count"]) == (1, 0)


def test_not_sent_releases_its_reservation_and_subscription_rows_are_skipped(users):
    a, _ = users
    refused = request_row(
        a, outcome=InferenceOutcome.NOT_SENT, cost_status="not_sent", finished_at=T0
    )
    assert [r.kind for r, _ in record_inference_request(actor=a, request=refused)] == [
        "reservation",
        "release",
    ]
    assert effective_observations(a).count() == 0
    assert (
        record_inference_request(
            actor=a, request=finished(a, billing_mode="subscription")
        )
        == []
    )


def test_inference_adapter_denies_cross_owner_and_foreign_session(users):
    a, b = users
    req = finished(a)
    with pytest.raises(PermissionDenied):
        record_inference_request(actor=b, request=req)
    foreign = ConversationSession.objects.create(
        user=b, bot_name="kitchen", engine_type="openai"
    )
    mixed = finished(a, session=foreign)
    with pytest.raises(PermissionDenied):
        record_inference_request(actor=a, request=mixed)
    with pytest.raises(PermissionDenied):
        record_inference_request(actor=a, request=finished(None))
    assert CostObservation.objects.count() == 0


@pytest.fixture
def job(users):
    a, _ = users
    account = RunpodAccount.objects.create(key="acct")
    return RunpodJob.objects.create(
        account=account,
        bot_name="kitchen",
        owner_id=str(a.pk),
        session_identity=uuid.uuid4(),
        request_key="r1",
        spec_hash="0" * 64,
        profile={},
        ledger_path="/nonexistent/rental.sqlite3",
        reservation_usd=D("1.50"),
        created=T0.timestamp(),
        deadline=T0.timestamp() + 3600,
    )


def component(a, job, **kw):
    args = {
        "actor": a,
        "job": job,
        "component": "compute",
        "resource_id": "pod1",
        "period_start": T0,
        "period_end": T0 + timedelta(hours=1),
        "kind": CostKind.ESTIMATE,
        "revision": 1,
    }
    return record_component(**{**args, **kw})


def test_runpod_compute_and_storage_are_separate_and_charge_supersedes_estimate(
    users, job
):
    a, _ = users
    record_job_reservation(actor=a, job=job)
    est = estimate_amount(D("0.6"), T0, T0 + timedelta(hours=1))
    assert est == D("0.6")
    component(a, job, amount=est)
    component(a, job, component="storage", kind=CostKind.UNKNOWN)
    component(
        a,
        job,
        kind=CostKind.PROVIDER_REPORTED,
        revision=2,
        amount=D("0.55"),
        charge_id="chg1",
    )
    by_kind = {b["kind"]: b for b in summarize_costs(a)}
    assert by_kind["provider_reported"]["known_amount"] == D("0.55")
    assert "estimate" not in by_kind  # superseded, not added
    assert (
        by_kind["unknown"]["unknown_count"] == 1
        and by_kind["unknown"]["known_amount"] is None
    )
    assert by_kind["reservation"]["known_amount"] == D("1.50")


def test_fractional_hour_estimate_can_be_recorded(users, job):
    a, _ = users
    end = T0 + timedelta(seconds=1)
    amount = estimate_amount(D("1"), T0, end)
    row, _ = component(a, job, period_end=end, amount=amount)
    assert row.amount == D("0.000277777777777778")


def test_period_ending_at_window_start_is_excluded(users):
    a, _ = users
    make(
        a,
        "ended",
        CostKind.ESTIMATE,
        D("1"),
        period_start=WINDOW[0] - timedelta(hours=1),
        period_end=WINDOW[0],
    )
    assert summarize_costs(a, since=WINDOW[0], until=WINDOW[1]) == []


def test_runpod_missing_rate_is_unknown_and_unreconciled_cannot_be_final(users, job):
    a, _ = users
    assert estimate_amount(None, T0, T0 + timedelta(hours=1)) is None
    assert estimate_amount(D("1"), T0, None) is None
    with pytest.raises(ValueError):
        component(a, job, kind=CostKind.FINAL, amount=D("1"))  # no provider charge id
    with pytest.raises(ValueError):
        component(a, job, kind=CostKind.RELEASE)
    with pytest.raises(ValueError):
        component(a, job, period_end=None)


def test_runpod_denies_cross_owner_and_replay_is_idempotent(users, job):
    a, b = users
    with pytest.raises(PermissionDenied):
        component(b, job, amount=D("1"))
    assert component(a, job, amount=D("1"))[1] is True
    assert component(a, job, amount=D("1"))[1] is False
    with pytest.raises(CostConflictError):
        component(a, job, amount=D("2"))


def test_runpod_successor_settles_same_resource_period_without_double_count(users, job):
    a, _ = users
    estimate, _ = component(a, job, amount=D("1"))
    successor = RunpodJob.objects.create(
        account=job.account,
        bot_name=job.bot_name,
        owner_id=job.owner_id,
        session_identity=uuid.uuid4(),
        request_key="successor",
        spec_hash=job.spec_hash,
        profile={},
        ledger_path=job.ledger_path,
        reservation_usd=job.reservation_usd,
        created=job.created,
        deadline=job.deadline,
    )
    final, _ = component(
        a, successor, kind=CostKind.FINAL, amount=D("0.7"), charge_id="charge1"
    )
    assert final.operation_id == estimate.operation_id
    assert final.operation.job_ref == ""
    assert (estimate.attempt_ref, final.attempt_ref) == (str(job.pk), str(successor.pk))
    assert [(b["kind"], b["known_amount"]) for b in summarize_costs(a)] == [
        ("final", D("0.7"))
    ]


def test_runpod_rejects_overlapping_periods_and_reused_charge(users, job):
    a, _ = users
    component(a, job, kind=CostKind.FINAL, amount=D("1"), charge_id="charge1")
    with pytest.raises(CostConflictError):
        component(a, job, period_end=T0 + timedelta(minutes=30))
    with pytest.raises(CostConflictError):
        component(
            a,
            job,
            component="storage",
            kind=CostKind.FINAL,
            amount=D("1"),
            charge_id="charge1",
        )
    with pytest.raises(CostConflictError):
        component(
            a,
            job,
            period_start=T0 + timedelta(hours=1),
            period_end=T0 + timedelta(hours=2),
            kind=CostKind.FINAL,
            amount=D("1"),
            charge_id="charge1",
        )
    assert CostObservation.objects.count() == 1


def test_runpod_fractional_adjacent_periods_remain_distinct(users, job):
    a, _ = users
    boundary = T0 + timedelta(microseconds=1)
    first, _ = component(a, job, period_end=boundary, amount=D("1"))
    second, _ = component(
        a,
        job,
        period_start=boundary,
        period_end=boundary + timedelta(microseconds=1),
        amount=D("2"),
    )
    assert first.operation_id != second.operation_id
    assert summarize_costs(a)[0]["known_amount"] == D("3")
    with pytest.raises(ValueError):
        component(a, job, period_end=T0)


def make(a, key, kind, amount, **kw):
    return record_observation(
        actor=a,
        operation=OperationSpec(
            "p", "acct", key, kw.pop("currency", "USD"), bot_name=kw.pop("bot", "x")
        ),
        observation=ObservationSpec(
            "s", f"{key}-{kind}", kind, kw.pop("revision", 1), amount=amount, **kw
        ),
    )


def test_window_applies_after_supersession_and_to_periods(users):
    a, _ = users
    inside, before = T0, datetime(2026, 10, 1)
    make(a, "old-final", CostKind.ESTIMATE, D("1"), occurred_at=inside)
    make(a, "old-final", CostKind.FINAL, D("2"), occurred_at=before, revision=2)
    # The settled row is outside the window: the in-window estimate must not resurrect.
    assert summarize_costs(a, since=WINDOW[0], until=WINDOW[1]) == []
    make(
        a,
        "span",
        CostKind.ESTIMATE,
        D("3"),
        period_start=datetime(2026, 10, 30),
        period_end=T0,
    )
    make(a, "none", CostKind.ESTIMATE, D("4"))
    (bucket,) = summarize_costs(a, since=WINDOW[0], until=WINDOW[1])
    assert bucket["known_amount"] == D("3") and bucket["partial_period_count"] == 1
    assert bucket["undated_count"] == 1 and bucket["operation_count"] == 1


def test_currencies_and_bots_stay_separate(users):
    a, b = users
    make(a, "u", CostKind.ESTIMATE, D("1"), occurred_at=T0, bot="ads")
    make(
        a, "c", CostKind.ESTIMATE, D("1"), occurred_at=T0, currency="CREDITS", bot="ads"
    )
    make(a, "k", CostKind.ESTIMATE, D("9"), occurred_at=T0, bot="kitchen")
    make(b, "other", CostKind.ESTIMATE, D("7"), occurred_at=T0, bot="ads")
    rows = summarize_costs(a, bot_name="ads", since=WINDOW[0], until=WINDOW[1])
    assert {(r["currency"], r["known_amount"]) for r in rows} == {
        ("USD", D("1")),
        ("CREDITS", D("1")),
    }
    with pytest.raises(ValueError):
        summarize_costs(a, since=WINDOW[0])


def test_coverage_manifest_never_claims_live_or_complete():
    manifest = coverage_manifest()
    assert manifest["live_ingestion"] is False and manifest["complete"] is False
    status = {s["source"]: s["status"] for s in manifest["sources"]}
    assert status["llm_inference_request"] == "adapter_callable_not_wired"
    assert status["ads_mediagen_central"] == "not_integrated"
    assert status["ryan_minimax_proxy"] == "external_unknown"
    assert status["runpod_rest_api_quota"] == "unsupported"
