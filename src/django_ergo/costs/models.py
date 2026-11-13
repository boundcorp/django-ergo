"""Append-only ledger models, registered in the existing django_ergo app."""

import uuid

from django import VERSION as DJANGO_VERSION
from django.conf import settings
from django.db import models
from django.db.models import Q

from .types import CostKind


def _constraint(*, condition, name):
    # Django 4.2 uses check; 5.1 renamed it and 6.0 removed the old keyword.
    keyword = "condition" if DJANGO_VERSION >= (5, 1) else "check"
    return models.CheckConstraint(name=name, **{keyword: condition})


class ImmutableCostQuerySet(models.QuerySet):
    def update(self, **kwargs):
        msg = "Cost history is immutable"
        raise TypeError(msg)

    def delete(self):
        msg = "Cost history requires explicit retention administration"
        raise TypeError(msg)

    def bulk_update(self, *args, **kwargs):
        msg = "Cost history is immutable"
        raise TypeError(msg)

    def bulk_create(self, *args, **kwargs):
        msg = "Use record_observation for validated idempotent ingestion"
        raise TypeError(msg)

    def for_owner(self, actor):
        if not actor or not actor.is_authenticated or not actor.is_active:
            return self.none()
        if actor.is_superuser:
            return self
        field = (
            "owner_id"
            if self.model.__name__ == "CostOperation"
            else "operation__owner_id"
        )
        return self.filter(**{field: actor.pk})


class ImmutableCostModel(models.Model):
    objects = ImmutableCostQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(
        self, force_insert=False, force_update=False, using=None, update_fields=None
    ):
        if not self._state.adding or force_update or update_fields is not None:
            msg = "Cost history is immutable"
            raise TypeError(msg)
        return super().save(force_insert=True, using=using)

    def delete(self, *args, **kwargs):
        msg = "Cost history requires explicit retention administration"
        raise TypeError(msg)


class CostOperation(ImmutableCostModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    provider = models.CharField(max_length=200)
    account_ref = models.CharField(max_length=200)
    operation_key = models.CharField(max_length=200)
    currency = models.CharField(max_length=16)
    bot_name = models.CharField(max_length=200, blank=True)
    project_ref = models.CharField(max_length=200, blank=True)
    job_ref = models.CharField(max_length=200, blank=True)
    resource_type = models.CharField(max_length=200, blank=True)
    resource_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "account_ref", "operation_key"],
                name="cost_operation_identity",
            ),
            _constraint(
                condition=Q(resource_type="", resource_id="")
                | (~Q(resource_type="") & ~Q(resource_id="")),
                name="cost_op_resource_pair",
            ),
        ]
        indexes = [models.Index(fields=["owner", "bot_name"], name="cost_owner_bot")]

    def __str__(self):
        return f"{self.provider} {self.currency} operation {self.pk}"


class CostObservation(ImmutableCostModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey(
        CostOperation, on_delete=models.PROTECT, related_name="observations"
    )
    # Denormalized namespace lets the database protect source identity across operations.
    provider = models.CharField(max_length=200)
    account_ref = models.CharField(max_length=200)
    source = models.CharField(max_length=200)
    source_event_id = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=24, choices=[(kind.value, kind.value) for kind in CostKind]
    )
    revision = models.PositiveIntegerField()
    occurred_at = models.DateTimeField(null=True)
    observed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    period_start = models.DateTimeField(null=True)
    period_end = models.DateTimeField(null=True)
    amount = models.DecimalField(max_digits=38, decimal_places=18, null=True)
    session_ref = models.CharField(max_length=200, blank=True)
    worker_ref = models.CharField(max_length=200, blank=True)
    tool_ref = models.CharField(max_length=200, blank=True)
    attempt_ref = models.CharField(max_length=200, blank=True)
    resource_type = models.CharField(max_length=200, blank=True)
    resource_id = models.CharField(max_length=200, blank=True)
    quantities = models.JSONField(default=dict)
    tokens_uncached_input = models.PositiveBigIntegerField(null=True)
    tokens_cache_write = models.PositiveBigIntegerField(null=True)
    tokens_cache_read = models.PositiveBigIntegerField(null=True)
    tokens_output = models.PositiveBigIntegerField(null=True)
    tokens_reasoning = models.PositiveBigIntegerField(null=True)
    provenance = models.JSONField(default=dict)
    payload_sha256 = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "account_ref", "source", "source_event_id"],
                name="cost_source_identity",
            ),
            models.UniqueConstraint(
                fields=["operation", "kind", "revision"], name="cost_revision_identity"
            ),
            _constraint(
                condition=Q(resource_type="", resource_id="")
                | (~Q(resource_type="") & ~Q(resource_id="")),
                name="cost_obs_resource_pair",
            ),
            _constraint(condition=Q(revision__gte=1), name="cost_positive_revision"),
            _constraint(
                condition=Q(tokens_reasoning__isnull=True)
                | Q(tokens_output__isnull=True)
                | Q(tokens_reasoning__lte=models.F("tokens_output")),
                name="cost_reasoning_subset",
            ),
            _constraint(
                condition=Q(period_start__isnull=True, period_end__isnull=True)
                | Q(
                    period_start__isnull=False,
                    period_end__isnull=False,
                    period_end__gte=models.F("period_start"),
                ),
                name="cost_bounded_period",
            ),
            _constraint(
                condition=~Q(kind__in=["final", "provider_reported"])
                | Q(amount__isnull=False),
                name="cost_evidenced_money",
            ),
            _constraint(
                condition=~Q(kind__in=["unknown", "usage", "lifecycle", "subscription"])
                | Q(amount__isnull=True),
                name="cost_nonmonetary_kind",
            ),
        ]
        indexes = [
            models.Index(
                fields=["operation", "kind", "revision"], name="cost_projection"
            )
        ]

    def __str__(self):
        return f"{self.kind} observation {self.pk}"
