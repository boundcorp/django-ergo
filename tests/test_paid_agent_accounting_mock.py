"""Mock-only contract probe: exercises the real per-attempt guard, not a CLI sandbox."""

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync

from django_ergo.bots import pools
from django_ergo.bots.providers import parse_providers
from django_ergo.conversation import spend_guard
from django_ergo.conversation.models import InferenceRequest
from tests.test_model_pools import POOLS
from tests.test_model_pools_acceptance import held as held_fixture
from tests.test_model_pools_acceptance import metered_guard
from tests.test_model_pools_acceptance import request

held = held_fixture


@pytest.mark.django_db(transaction=True)
def test_mock_attempt_receipt_then_disconnect_keeps_hold(held):
    guard = metered_guard(held)
    sends = []

    async def success():
        sends.append("ok")
        return SimpleNamespace(
            _request_id="mock-request-1",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    async def disconnect():
        sends.append("disconnect")
        raise TimeoutError

    async_to_sync(guard.call)(
        request(max_completion_tokens=8), "max_completion_tokens", success
    )
    with pytest.raises(TimeoutError):
        async_to_sync(guard.call)(
            request(max_completion_tokens=8), "max_completion_tokens", disconnect
        )
    rows = list(InferenceRequest.objects.order_by("attempt"))
    assert sends == ["ok", "disconnect"]
    assert len(rows) == 2 and all(row.reserved_usd > 0 for row in rows)
    assert (
        rows[0].upstream_request_id == "mock-request-1"
        and rows[0].cost_status == "priced"
    )
    assert rows[1].cost_status == "unknown" and rows[1].cost_usd is None
    assert spend_guard.spent_and_reserved(held.pk)[1] == rows[1].reserved_usd


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("denial", ["model", "provider_pause", "exhausted"])
def test_mock_denial_never_calls_transport(held, denial):
    guard = metered_guard(held)
    sent = []

    async def send():
        sent.append(True)

    payload = request(max_completion_tokens=8)
    if denial == "model":
        payload["model"] = "unauthorized-model"
    elif denial == "provider_pause":
        pools.set_provider_enabled("openai", parse_providers(POOLS), False, version=0)
    else:
        # A prior uncertain attempt consumes the entire grant; no network involved.
        spend_guard.reserve(
            grant_id=held.pk,
            provider="openai",
            model="gpt-6.1-sol",
            amount=held.budget_usd,
            user_id=held.grantee_id,
            fields={},
            current=guard.current,
        )
    with pytest.raises(spend_guard.SpendDenied):
        async_to_sync(guard.call)(payload, "max_completion_tokens", send)
    assert not sent
