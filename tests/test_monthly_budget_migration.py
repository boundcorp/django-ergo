"""Upgrade/reverse chronology without inferred funding or forgiven exposure."""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_monthly_upgrade_and_reverse_preserve_ambiguous_paid_history(settings):
    settings.USE_TZ = True
    before = ("django_ergo", "0063_google_engine_signatures")
    after = ("django_ergo", "0064_provider_monthly_budgets")
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([before])
        old = executor.loader.project_state([before]).apps
        User = old.get_model("auth", "User")
        owner = User.objects.create(username="historical-owner")
        envelope = old.get_model("django_ergo", "SpendEnvelope").objects.create(
            key="historical",
            pool="paid",
            owner_id=owner.pk,
            grantee_id=owner.pk,
        )
        grant = old.get_model("django_ergo", "SpendGrant").objects.create(
            envelope_id=envelope.pk,
            pool="paid",
            owner_id=owner.pk,
            grantee_id=owner.pk,
            routes=["unknown/model"],
            budget_usd="10",
            max_output_tokens=100,
            valid_from=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            expires_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
        )
        attempt = old.get_model("django_ergo", "InferenceRequest").objects.create(
            pool=f"grant:{grant.pk}",
            provider="unknown",
            model="model",
            billing_mode="metered",
            reserved_usd=Decimal("7.1234567890"),
        )
        old.get_model("django_ergo", "InferenceRequest").objects.filter(
            pk=attempt.pk
        ).update(
            created_at=dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=dt.UTC),
        )
        executor = MigrationExecutor(connection)
        executor.migrate([after])
        current = executor.loader.project_state([after]).apps
        migrated = current.get_model("django_ergo", "InferenceRequest").objects.get(
            pk=attempt.pk
        )
        assert migrated.budget_month == dt.date(2026, 10, 1)
        assert (
            migrated.provider_family_id is None and migrated.account_mapping_id is None
        )
        assert migrated.reserved_usd == Decimal("7.1234567890")
        assert (
            current.get_model("django_ergo", "SpendGrant")
            .objects.get(pk=grant.pk)
            .budget_period
            == "cumulative"
        )
        assert not current.get_model(
            "django_ergo", "ProviderMonthlyConsent"
        ).objects.exists()
        from django_ergo.conversation.monthly_budgets import historical_problem

        assert "Ambiguous" in historical_problem("openai")
        MigrationExecutor(connection).migrate([before])
        rolled_back = old.get_model("django_ergo", "InferenceRequest").objects.get(
            pk=attempt.pk
        )
        assert rolled_back.reserved_usd == migrated.reserved_usd
        MigrationExecutor(connection).migrate([after])
        assert "Ambiguous" in historical_problem("openai")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
