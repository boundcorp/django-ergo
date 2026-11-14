"""Offline accounting contracts: no provider clients, credentials or spend."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from decimal import localcontext
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.db.models.deletion import ProtectedError
from django.test import override_settings

from django_ergo.costs.ledger import effective_observations
from django_ergo.costs.ledger import latest_observations
from django_ergo.costs.ledger import record_observation
from django_ergo.costs.ledger import summarize_costs
from django_ergo.costs.models import CostObservation
from django_ergo.costs.models import CostOperation
from django_ergo.costs.types import CostConflictError
from django_ergo.costs.types import CostKind
from django_ergo.costs.types import ObservationSpec
from django_ergo.costs.types import OperationSpec
from django_ergo.costs.types import TokenUsage
from django_ergo.costs.types import normalize_tokens

D = Decimal
AT = datetime(2026, 11, 13, 12, tzinfo=UTC).replace(tzinfo=None)
OP = OperationSpec(
    "example",
    "account-opaque",
    "request-1",
    "USD",
    bot_name="ads",
    project_ref="project-1",
)
OBS = ObservationSpec(
    "fixture-v1", "event-1", CostKind.ESTIMATE, 1, occurred_at=AT, amount=D("1.25")
)


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user(username="cost-owner")


def record(owner, *, operation=OP, observation=OBS):
    return record_observation(actor=owner, operation=operation, observation=observation)


def test_replay_is_durable_and_decimal_canonical(owner):
    first, created = record(
        owner, observation=replace(OBS, quantities={"audio_seconds": D("1.0")})
    )
    replay, replay_created = record(
        owner,
        observation=replace(
            OBS, amount=D("1.250"), quantities={"audio_seconds": D("1")}
        ),
    )
    assert created and not replay_created
    assert replay.pk == first.pk
    assert replay.observed_at == first.observed_at
    assert CostObservation.objects.count() == CostOperation.objects.count() == 1
    assert replay.amount == D("1.25")
    assert replay.occurred_at == AT
    assert replay.quantities == {"audio_seconds": "1"}


@pytest.mark.parametrize(
    "change",
    [
        {"amount": D("2")},
        {"occurred_at": AT + timedelta(seconds=1)},
        {"tokens": TokenUsage(1, 0, 0, 2, 1)},
        {"worker_ref": "worker-other"},
        {"provenance": {"model": "different-model"}},
        {"kind": CostKind.FINAL},
    ],
)
def test_conflicting_source_replay_never_mutates(owner, change):
    first, _ = record(owner)
    with pytest.raises(CostConflictError):
        record(owner, observation=replace(OBS, **change))
    first.refresh_from_db()
    assert first.amount == D("1.25")
    assert CostObservation.objects.count() == 1


def test_same_source_cannot_be_rebilled_under_new_economic_operation(owner):
    record(owner)
    with pytest.raises(CostConflictError):
        record(owner, operation=replace(OP, operation_key="summary-import"))
    assert CostOperation.objects.count() == 1  # failed import is fully rolled back


def test_revision_identity_cannot_be_filled_by_another_source(owner):
    record(owner)
    with pytest.raises(CostConflictError):
        record(
            owner, observation=replace(OBS, source="summary", source_event_id="copy")
        )
    assert CostObservation.objects.count() == 1


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "EUR"},
        {"bot_name": "other"},
        {"project_ref": "other"},
        {"resource_type": "pod", "resource_id": "pod-1"},
    ],
)
def test_economic_attribution_is_immutable(owner, change):
    record(owner)
    with pytest.raises(CostConflictError):
        record(
            owner,
            operation=replace(OP, **change),
            observation=replace(OBS, revision=2, source_event_id="event-2"),
        )


def test_namespace_separates_accounts_and_providers(owner):
    for change in (
        {},
        {"account_ref": "account-other"},
        {"provider": "provider-other"},
    ):
        record(owner, operation=replace(OP, **change))
    assert CostOperation.objects.count() == CostObservation.objects.count() == 3
    assert summarize_costs(owner)[0]["known_amount"] == D("3.75")


def test_projection_uses_revision_not_arrival_and_settlement_not_estimate(owner):
    for kind, revision, amount, event in [
        (CostKind.ESTIMATE, 3, "3", "estimate-3"),
        (CostKind.ESTIMATE, 1, "1", "estimate-1"),
        (CostKind.PROVIDER_REPORTED, 1, "2.5", "posted-1"),
        (CostKind.FINAL, 2, "2.1", "final-2"),
        (CostKind.FINAL, 1, "2.2", "final-1"),
        (CostKind.ESTIMATE, 4, "9", "estimate-4"),
    ]:
        record(
            owner,
            observation=replace(
                OBS,
                kind=kind,
                revision=revision,
                amount=D(amount),
                source_event_id=event,
            ),
        )
    projected = effective_observations(owner).get()
    assert (projected.kind, projected.revision, projected.amount) == (
        CostKind.FINAL,
        2,
        D("2.1"),
    )
    assert summarize_costs(owner) == [
        {
            "kind": "final",
            "currency": "USD",
            "known_amount": D("2.1"),
            "known_count": 1,
            "unknown_count": 0,
            "operation_count": 1,
        }
    ]
    assert CostObservation.objects.count() == 6
    # Consumer filtering must not move filters inside the ranking operation.
    assert not effective_observations(owner).filter(kind="estimate").exists()
    assert not latest_observations(owner).filter(kind="estimate", revision=1).exists()


def test_new_unknown_estimate_does_not_expose_stale_known_value(owner):
    record(owner)
    record(
        owner,
        observation=replace(OBS, revision=2, amount=None, source_event_id="event-2"),
    )
    bucket = summarize_costs(owner)[0]
    assert bucket["known_amount"] is None
    assert bucket["known_count"] == 0
    assert bucket["unknown_count"] == bucket["operation_count"] == 1
    assert effective_observations(owner).get().amount is None


def test_reservation_release_subscription_and_api_equivalent_are_not_charges(owner):
    for kind, amount in [
        (CostKind.RESERVATION, D("10")),
        (CostKind.RELEASE, D("10")),
        (CostKind.API_EQUIVALENT, D("4")),
        (CostKind.SUBSCRIPTION, None),
        (CostKind.UNKNOWN, None),
    ]:
        record(
            owner,
            observation=replace(
                OBS, kind=kind, amount=amount, source_event_id=kind.value
            ),
        )
    assert effective_observations(owner).get().kind == CostKind.UNKNOWN
    buckets = {row["kind"]: row for row in summarize_costs(owner)}
    assert buckets["unknown"]["known_amount"] is None
    assert buckets["reservation"]["known_amount"] == D("10")
    assert buckets["release"]["known_amount"] == D("10")
    assert buckets["api_equivalent"]["known_amount"] == D("4")
    assert buckets["subscription"]["unknown_count"] == 1


def test_totals_keep_currency_and_unknown_separate_and_allow_evidenced_credits(owner):
    rows = [
        ("one", "USD", D("0.9009412648156285")),
        ("two", "USD", None),
        ("three", "EUR", D("4.75")),
        ("credit", "USD", D("-0.1")),
    ]
    for key, currency, amount in rows:
        record(
            owner,
            operation=replace(OP, operation_key=key, currency=currency),
            observation=replace(OBS, source_event_id=key, amount=amount),
        )
    with localcontext() as context:
        context.prec = 2  # consumer Decimal context cannot round accounting totals
        buckets = {row["currency"]: row for row in summarize_costs(owner)}
    assert buckets["USD"]["known_amount"] == D("0.8009412648156285")
    assert buckets["USD"]["unknown_count"] == 1
    assert buckets["USD"]["known_count"] == 2
    assert buckets["EUR"]["known_amount"] == D("4.75")
    assert summarize_costs(owner, bot_name="different") == []
    assert summarize_costs(owner, project_ref="different") == []


def test_zero_is_known_only_when_explicitly_evidenced(owner):
    record(
        owner, observation=replace(OBS, kind=CostKind.PROVIDER_REPORTED, amount=D("0"))
    )
    row = summarize_costs(owner)[0]
    assert row["known_amount"] == D("0")
    assert row["unknown_count"] == 0


def test_resource_identity_survives_worker_replacement_and_storage_is_separate(owner):
    pod = replace(
        OP,
        operation_key="pod-1-compute-period-1",
        resource_type="pod",
        resource_id="pod-1",
        job_ref="native-job-uuid",
    )
    record(
        owner,
        operation=pod,
        observation=replace(
            OBS,
            worker_ref="worker-1",
            attempt_ref="create-intent-1",
            period_start=AT,
            period_end=AT + timedelta(hours=1),
            quantities={"gpu_seconds": D("3600")},
            provenance={"lifecycle": "running"},
        ),
    )
    record(
        owner,
        operation=pod,
        observation=replace(
            OBS,
            revision=2,
            source_event_id="stopped",
            worker_ref="worker-2",
            provenance={"lifecycle": "stopped"},
        ),
    )
    record(
        owner,
        operation=replace(pod, operation_key="pod-1-storage-period-1"),
        observation=replace(
            OBS,
            source_event_id="storage",
            amount=None,
            quantities={"storage_gib_hours": None},
        ),
    )
    rows = list(effective_observations(owner).select_related("operation"))
    assert {row.operation.resource_id for row in rows} == {"pod-1"}
    assert {row.worker_ref for row in rows} == {"worker-2", ""}
    assert summarize_costs(owner)[0]["unknown_count"] == 1
    assert summarize_costs(owner)[0]["known_amount"] == D("1.25")


def test_owner_scoped_reads_and_privileged_ingestion(owner):
    other = get_user_model().objects.create_user(username="cost-other")
    admin = get_user_model().objects.create_superuser(username="cost-admin")
    record(owner)
    assert not effective_observations(other).exists()
    assert summarize_costs(AnonymousUser()) == []
    assert effective_observations(admin).get().operation.owner_id == owner.pk
    with pytest.raises(PermissionDenied):
        record_observation(actor=other, owner=owner, operation=OP, observation=OBS)
    with pytest.raises(PermissionDenied):
        record_observation(
            actor=AnonymousUser(), owner=owner, operation=OP, observation=OBS
        )
    replay, created = record_observation(
        actor=admin, owner=owner, operation=OP, observation=OBS
    )
    assert not created and replay.operation.owner_id == owner.pk
    owner.is_active = False
    assert summarize_costs(owner) == []
    with pytest.raises(PermissionDenied):
        record(owner)


def test_owner_deletion_cannot_orphan_financial_history(owner):
    record(owner)
    with pytest.raises(ProtectedError):
        owner.delete()
    assert CostObservation.objects.count() == 1


@pytest.mark.parametrize(
    "change",
    [
        {"provenance": {"prompt": "private"}},
        {"provenance": {"model": "raw prompt text"}},
        {"provenance": {"evidence_ref": "https://provider/body?secret=x"}},
        {"provenance": {"funding": "AI-Pro-pays-for-all-Gemini"}},
        {"provenance": {"model": {"body": "secret"}}},
        {"source_event_id": "sk-privatecredential"},
        {"source_event_id": "../../raw/session/path"},
        {"source_event_id": "x" * 201},
        {"quantities": {"audio_seconds": 1.2}},
        {"quantities": {"audio_seconds": D("-1")}},
        {"quantities": {"image prompt": D("1")}},
        {"amount": 0.5},
        {"amount": D("NaN")},
        {"amount": D("Infinity")},
        {"amount": D("0.0000000000000000001")},
        {"amount": D("1e20")},
        {"kind": CostKind.FINAL, "amount": None},
        {"kind": CostKind.PROVIDER_REPORTED, "amount": None},
        {"kind": CostKind.UNKNOWN},
        {"kind": CostKind.USAGE},
        {"kind": CostKind.SUBSCRIPTION},
        {"revision": 0},
        {"revision": True},
        {"period_start": AT},
        {"period_start": AT, "period_end": AT - timedelta(seconds=1)},
        {"occurred_at": "yesterday"},
    ],
)
def test_invalid_or_private_payload_never_leaves_rows(owner, change):
    with pytest.raises((ValueError, TypeError)):
        record(owner, observation=replace(OBS, **change))
    assert CostOperation.objects.count() == CostObservation.objects.count() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"account_ref": "Bearer-secret"},
        {"account_ref": "https://account/private"},
        {"currency": ""},
        {"currency": "usd"},
        {"resource_id": "pod-1"},
    ],
)
def test_invalid_identity_is_rejected_before_writes(owner, change):
    with pytest.raises((TypeError, ValueError)):
        record(owner, operation=replace(OP, **change))
    assert CostOperation.objects.count() == 0


def test_typed_tokens_media_units_and_funding_provenance_roundtrip(owner):
    tokens = normalize_tokens(
        input_tokens=100,
        input_semantics="total",
        cache_write=20,
        cache_read=30,
        output=40,
        reasoning=15,
    )
    observed, _ = record(
        owner,
        observation=replace(
            OBS,
            tokens=tokens,
            quantities={
                "image_count": D("2"),
                "pixels": D("2097152"),
                "video_seconds": D("1.25"),
                "audio_seconds": None,
            },
            provenance={
                "model": "gemini-image-exact",
                "price_reference": "reviewed-v3",
                "evidence_ref": "sha256-abc",
                "funding": "metered_api",
                "source_model": "InferenceRequest",
                "source_ref": "request-uuid",
            },
        ),
    )
    observed.refresh_from_db()
    assert (
        observed.tokens_uncached_input,
        observed.tokens_cache_write,
        observed.tokens_cache_read,
        observed.tokens_output,
        observed.tokens_reasoning,
    ) == (50, 20, 30, 40, 15)
    assert tokens.total == 140  # reasoning is not added twice
    assert observed.quantities == {
        "image_count": "2",
        "pixels": "2097152",
        "video_seconds": "1.25",
        "audio_seconds": None,
    }
    assert observed.provenance["funding"] == "metered_api"


def test_immutable_history_rejects_ordinary_mutations(owner):
    row, _ = record(owner)
    with pytest.raises(TypeError):
        CostObservation.objects.filter(pk=row.pk).update(amount=D("9"))
    with pytest.raises(TypeError):
        row.save()
    with pytest.raises(TypeError):
        row.delete()
    with pytest.raises(TypeError):
        CostOperation.objects.all().delete()
    with pytest.raises(TypeError):
        CostObservation.objects.bulk_update([row], ["amount"])
    with pytest.raises(TypeError):
        CostObservation.objects.bulk_create(
            [row], update_conflicts=True, unique_fields=["id"], update_fields=["amount"]
        )
    row.refresh_from_db()
    assert row.amount == D("1.25")


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.django_db(transaction=True)
def test_concurrent_replay_serializes_one_observation(conflict):
    principal = get_user_model().objects.create_user(username="concurrent-owner")
    barrier = Barrier(2)

    def ingest(index):
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=principal.pk)
            barrier.wait(timeout=10)
            try:
                row, created = record(
                    actor,
                    observation=replace(
                        OBS, amount=D("2") if conflict and index else OBS.amount
                    ),
                )
                return "accepted", str(row.pk), created
            except CostConflictError:
                return "conflict", None, False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ingest, [0, 1]))
    assert CostObservation.objects.count() == CostOperation.objects.count() == 1
    assert sum(result[2] for result in results) == 1
    assert sum(result[0] == "conflict" for result in results) == int(conflict)
    if not conflict:
        assert results[0][1] == results[1][1]


@pytest.mark.django_db(transaction=True)
def test_concurrent_source_cannot_fund_two_different_operations():
    principal = get_user_model().objects.create_user(username="concurrent-source-owner")
    barrier = Barrier(2)

    def ingest(index):
        close_old_connections()
        try:
            actor = get_user_model().objects.get(pk=principal.pk)
            barrier.wait(timeout=10)
            try:
                record(actor, operation=replace(OP, operation_key=f"competing-{index}"))
                return "accepted"
            except CostConflictError:
                return "conflict"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ingest, [0, 1]))
    assert sorted(results) == ["accepted", "conflict"]
    assert CostObservation.objects.count() == CostOperation.objects.count() == 1


@pytest.mark.parametrize(
    ("semantics", "input_tokens", "write", "read", "expected"),
    [
        ("uncached", 50, 20, 30, 50),
        ("total", 100, 20, 30, 50),
        ("total", 100, None, 30, None),
        ("total", None, 0, 0, None),
        ("total", 0, 0, 0, 0),
    ],
)
def test_token_input_semantics_are_explicit(
    semantics, input_tokens, write, read, expected
):
    usage = normalize_tokens(
        input_tokens=input_tokens,
        input_semantics=semantics,
        cache_write=write,
        cache_read=read,
    )
    assert usage.uncached_input == expected
    assert usage.cache_write == write and usage.cache_read == read
    assert usage.total is None  # output is unknown, never silently zero


@pytest.mark.parametrize(
    "arguments",
    [
        {"input_tokens": 2, "cache_read": 3, "cache_write": 0},
        {"input_tokens": 2, "cache_read": 3},
        {"input_tokens": -1},
        {"input_tokens": True},
        {"input_tokens": 1.5},
        {"input_tokens": 2**63},
        {"input_tokens": 1, "output": 1, "reasoning": 2},
        {"input_tokens": 1, "input_semantics": "guess"},
    ],
)
def test_impossible_token_partitions_are_rejected(arguments):
    with pytest.raises(ValueError, match="Cache|Token|Input|Reasoning"):
        normalize_tokens(**{"input_semantics": "total", **arguments})


def test_late_resource_discovery_keeps_create_intent_and_prevents_rebinding(owner):
    unknown_resource = replace(OP, operation_key="create-intent-uuid")
    initial = replace(
        OBS, source_event_id="create-uncertain", amount=None, attempt_ref="intent-uuid"
    )
    record(owner, operation=unknown_resource, observation=initial)
    discovered = replace(
        OBS,
        source_event_id="resource-discovered",
        revision=2,
        resource_type="pod",
        resource_id="provider-pod-id",
        worker_ref="worker-new",
        attempt_ref="intent-uuid",
    )
    record(owner, operation=unknown_resource, observation=discovered)
    _, created = record(owner, operation=unknown_resource, observation=initial)
    assert not created  # late discovery cannot invalidate earlier exact replay
    assert effective_observations(owner).get().resource_id == "provider-pod-id"
    with pytest.raises(CostConflictError, match="provider resource"):
        record(
            owner,
            operation=unknown_resource,
            observation=replace(
                discovered,
                source_event_id="wrong-pod",
                revision=3,
                resource_id="other-pod",
            ),
        )
    assert CostObservation.objects.count() == 2


def test_resource_reference_must_match_stable_economic_identity(owner):
    resource = replace(OP, resource_type="pod", resource_id="pod-1")
    with pytest.raises(CostConflictError, match="provider resource"):
        record(
            owner,
            operation=resource,
            observation=replace(
                OBS,
                resource_type="pod",
                resource_id="pod-2",
            ),
        )
    assert CostOperation.objects.count() == 0


def test_aware_event_replay_normalizes_timezone_without_changing_observed_time(owner):
    with override_settings(USE_TZ=True):
        event = replace(OBS, occurred_at=AT.replace(tzinfo=UTC))
        first, _ = record(owner, observation=event)
        from datetime import timezone

        same_in_offset = event.occurred_at.astimezone(timezone(timedelta(hours=5)))
        replay, created = record(
            owner, observation=replace(event, occurred_at=same_in_offset)
        )
        assert not created and replay.observed_at == first.observed_at
        assert replay.occurred_at == event.occurred_at


def test_provider_model_namespace_is_preserved_but_source_paths_are_rejected(owner):
    row, _ = record(
        owner,
        observation=replace(
            OBS,
            provenance={"model": "accounts/fireworks/models/exact-v1"},
        ),
    )
    assert row.provenance["model"] == "accounts/fireworks/models/exact-v1"
    with pytest.raises(ValueError, match="safe code"):
        record(owner, observation=replace(OBS, source_event_id="private/session/path"))


def test_ambiguous_create_and_rejected_stock_are_nonmonetary_lifecycle(owner):
    for revision, lifecycle in enumerate(("create_uncertain", "stopped", "deleted"), 1):
        record(
            owner,
            observation=replace(
                OBS,
                kind=CostKind.LIFECYCLE,
                revision=revision,
                source_event_id=lifecycle,
                amount=None,
                provenance={"lifecycle": lifecycle},
            ),
        )
    assert not effective_observations(owner).exists()
    assert summarize_costs(owner) == []
    assert latest_observations(owner).get().provenance["lifecycle"] == "deleted"
