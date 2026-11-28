"""Guarded paid attempts are durable cost sources, even without private opt-in."""

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync

from django_ergo.conversation import inference_ledger
from django_ergo.conversation.models import InferenceRequest
from django_ergo.costs.adapters import record_inference_request
from django_ergo.costs.models import CostObservation
from tests.test_model_pools_acceptance import held as held_fixture
from tests.test_model_pools_acceptance import metered_guard
from tests.test_model_pools_acceptance import request

held = held_fixture


@pytest.mark.django_db(transaction=True)
def test_guard_cost_ingestion_is_mandatory_and_idempotent(held, settings):
    settings.DJANGO_ERGO = {"INFERENCE_COST_LEDGER_ENABLED": False}
    guard = metered_guard(held)

    # Query assertions in the async transport need a synchronous bridge.
    from asgiref.sync import sync_to_async

    async def transport():
        assert await sync_to_async(CostObservation.objects.count)() == 1
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        )

    async_to_sync(guard.call)(
        request(max_completion_tokens=8), "max_completion_tokens", transport
    )
    row = InferenceRequest.objects.get()
    assert row.cost_ledger_state == "recorded"
    assert set(CostObservation.objects.values_list("kind", flat=True)) == {
        "reservation",
        "estimate",
    }
    assert CostObservation.objects.get(kind="estimate").amount == row.cost_usd
    record_inference_request(actor=row.user, request=row)
    assert inference_ledger.reconcile_cost_ledger(row.pk)
    assert CostObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_guard_unknown_hold_is_visible(held):
    guard = metered_guard(held)

    async def timeout():
        raise TimeoutError

    with pytest.raises(TimeoutError):
        async_to_sync(guard.call)(
            request(max_completion_tokens=8), "max_completion_tokens", timeout
        )
    row = InferenceRequest.objects.get()
    assert set(CostObservation.objects.values_list("kind", flat=True)) == {
        "reservation",
        "unknown",
    }
    assert CostObservation.objects.get(kind="reservation").amount == row.reserved_usd
