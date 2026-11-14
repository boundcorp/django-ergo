from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections
from django.db import transaction

from django_ergo.conversation import inference_ledger
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import InferenceOutcome
from django_ergo.conversation.models import InferenceRequest
from django_ergo.costs.coverage import coverage_manifest
from django_ergo.costs.models import CostObservation

MODEL = "accounts/fireworks/models/deepseek-v4p1-flash-1017"


def grant(user, *, session=None):
    return SimpleNamespace(
        pool=SimpleNamespace(provider="fireworks", name="private", daily_usd_cap=None),
        model_id=MODEL,
        session_id=session.pk if session else None,
        user_id=user.pk,
        bot="personal",
        selector="personal/flash",
        purpose="chat",
    )


def opened(user, session=None):
    return inference_ledger._open(
        grant(user, session=session), {"max_tokens": 100}, 1, None
    )


@pytest.mark.django_db(transaction=True)
def test_opt_in_transitions_and_reconciliation(settings, monkeypatch):
    settings.DJANGO_ERGO = {"INFERENCE_COST_LEDGER_ENABLED": True}
    user = get_user_model().objects.create_user("hooks")
    entry = opened(user)
    row = InferenceRequest.objects.get(pk=entry.pk)
    assert row.cost_ledger_state == "recorded"
    assert list(CostObservation.objects.values_list("kind", flat=True)) == [
        "reservation"
    ]
    from django_ergo.costs import adapters

    real = adapters.record_inference_request
    monkeypatch.setattr(
        adapters,
        "record_inference_request",
        lambda **kw: (_ for _ in ()).throw(ValueError("secret")),
    )
    inference_ledger._finish(
        entry.pk, outcome=InferenceOutcome.ERROR, cost_status="unknown"
    )
    row.refresh_from_db()
    assert row.outcome == InferenceOutcome.ERROR and row.cost_ledger_state == "pending"
    monkeypatch.setattr(adapters, "record_inference_request", real)
    call_command("reconcile_inference_costs", request_id=str(entry.pk))
    row.refresh_from_db()
    assert row.cost_ledger_state == "recorded"
    assert set(CostObservation.objects.values_list("kind", flat=True)) == {
        "reservation",
        "unknown",
    }
    assert CostObservation.objects.get(kind="unknown").amount is None
    assert inference_ledger.reconcile_cost_ledger(entry.pk)
    assert CostObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_nested_transactions_rollback_and_not_sent_release(settings):
    settings.DJANGO_ERGO = {"INFERENCE_COST_LEDGER_ENABLED": True}
    user = get_user_model().objects.create_user("nested")
    with pytest.raises(RuntimeError), transaction.atomic():  # noqa: PT012
        opened(user)
        assert not CostObservation.objects.exists()
        raise RuntimeError
    assert not InferenceRequest.objects.exists()
    with transaction.atomic():
        entry = opened(user)
        inference_ledger._finish(
            entry.pk,
            outcome=InferenceOutcome.NOT_SENT,
            cost_status="not_sent",
            cost_usd=Decimal(0),
        )
        assert not CostObservation.objects.exists()
    assert set(CostObservation.objects.values_list("kind", flat=True)) == {
        "reservation",
        "release",
    }


@pytest.mark.django_db(transaction=True)
def test_concurrent_pending_accounting(settings):
    settings.DJANGO_ERGO = {"INFERENCE_COST_LEDGER_ENABLED": True}
    user = get_user_model().objects.create_user("concurrent")
    entry = opened(user)
    InferenceRequest.objects.filter(pk=entry.pk).update(cost_ledger_state="pending")

    def run(_):
        close_old_connections()
        try:
            return inference_ledger.reconcile_cost_ledger(entry.pk)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(executor.map(run, range(2)))
    assert CostObservation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_off_and_foreign_session_stays_pending(settings):
    settings.DJANGO_ERGO = {}
    user = get_user_model().objects.create_user("owner")
    other = get_user_model().objects.create_user("other")
    assert coverage_manifest()["live_ingestion"] is False
    off = opened(user)
    assert InferenceRequest.objects.get(pk=off.pk).cost_ledger_state == ""
    settings.DJANGO_ERGO = {"INFERENCE_COST_LEDGER_ENABLED": True}
    foreign = ConversationSession.objects.create(
        user=other, bot_name="personal", engine_type="openai"
    )
    bad = opened(user, foreign)
    assert InferenceRequest.objects.get(pk=bad.pk).cost_ledger_state == "pending"
    assert not CostObservation.objects.exists()
    assert coverage_manifest()["live_ingestion"] is True
