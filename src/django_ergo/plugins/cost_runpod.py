"""Passive RunPod cost projection adapter over the durable `RunpodJob` index.

Reads nothing from provider APIs or rental files and edits no lifecycle code: the
caller supplies evidence it already holds. Compute and storage are separate
operations. Termination, cleanup and the REST request budget are never money
evidence; only an explicit provider charge settles an estimate. Never retries or
alters the rental.
"""

import hashlib
import json
from datetime import UTC
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from django_ergo.costs.ledger import _authorize
from django_ergo.costs.ledger import record_observation
from django_ergo.costs.models import CostObservation
from django_ergo.costs.types import CostConflictError
from django_ergo.costs.types import CostKind
from django_ergo.costs.types import ObservationSpec
from django_ergo.costs.types import OperationSpec
from django_ergo.costs.types import code
from django_ergo.plugins.runpod.models import RunpodAccount

SOURCE = "runpod-native-v1"
COMPONENTS = ("compute", "storage")
_KINDS = {
    CostKind.ESTIMATE,
    CostKind.UNKNOWN,
    CostKind.PROVIDER_REPORTED,
    CostKind.FINAL,
}


def _owner(job):
    owner = get_user_model().objects.filter(pk=job.owner_id).first()
    if owner is None or str(owner.pk) != job.owner_id:
        msg = "RunPod job owner is not a known user"
        raise PermissionDenied(msg)
    if job.session_id and job.session.user_id != owner.pk:
        msg = "RunPod job session belongs to another owner"
        raise PermissionDenied(msg)
    return owner


def _operation(job, key, resource_id=""):
    return OperationSpec(
        provider="runpod",
        account_ref=job.account_id,
        operation_key=key,
        currency="USD",
        bot_name=job.bot_name,
        job_ref="" if resource_id else str(job.pk),
        resource_type="pod" if resource_id else "",
        resource_id=resource_id,
    )


def record_job_reservation(*, actor, job):
    """The admission reservation ceiling: a reservation, never a charge."""
    owner = _owner(job)
    return record_observation(
        actor=actor,
        owner=owner,
        operation=_operation(job, f"{job.pk}:admission"),
        observation=ObservationSpec(
            source=SOURCE,
            source_event_id=f"{job.pk}:reservation",
            kind=CostKind.RESERVATION,
            revision=1,
            occurred_at=_utc(job.created),
            amount=job.reservation_usd,
            provenance={"service": "runpod", "funding": "metered_api"},
        ),
    )


def _utc(epoch):
    moment = datetime.fromtimestamp(epoch, UTC)
    return moment if settings.USE_TZ else moment.replace(tzinfo=None)


def estimate_amount(hourly_usd, period_start, period_end):
    """rate x bounded period, or None when either is missing (unknown, not zero)."""
    if hourly_usd is None or period_start is None or period_end is None:
        return None
    seconds = Decimal(str((period_end - period_start).total_seconds()))
    return Decimal(hourly_usd) * seconds / Decimal(3600)


def _digest(*parts):
    return hashlib.sha256(
        json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _boundary(value):
    if not isinstance(value, datetime) or timezone.is_aware(value) != settings.USE_TZ:
        msg = "Event times must be datetimes matching Django USE_TZ"
        raise ValueError(msg)
    return (value.astimezone(UTC) if settings.USE_TZ else value).isoformat(
        timespec="microseconds"
    )


def record_component(  # noqa: PLR0913 - explicit evidence, no inference from lifecycle
    *,
    actor,
    job,
    component,
    resource_id,
    period_start,
    period_end,
    kind,
    revision,
    amount=None,
    charge_id="",
    evidence_ref="",
):
    """Record one compute or storage period for a pod.

    Identity is the exact half-open resource/component/period, independent of job.
    Different overlapping periods are rejected rather than prorated or guessed.
    Provider-reported/final amounts require an explicit provider charge id; that id
    cannot settle another operation. Stop/delete/cleanup are not money evidence.
    """
    kind = CostKind(kind)
    if component not in COMPONENTS or kind not in _KINDS:
        msg = "Unsupported RunPod cost component or kind"
        raise ValueError(msg)
    if not resource_id or period_start is None or period_end is None:
        msg = "A provider resource and bounded period are required"
        raise ValueError(msg)
    if kind in {CostKind.PROVIDER_REPORTED, CostKind.FINAL} and not charge_id:
        msg = "Provider-reported or final money needs a provider charge id"
        raise ValueError(msg)
    start, end = _boundary(period_start), _boundary(period_end)
    if period_end <= period_start:
        msg = "A nonempty provider period is required"
        raise ValueError(msg)
    if charge_id:
        code(charge_id)
    owner = _owner(job)
    _authorize(actor, owner)
    key = "period:" + _digest(component, resource_id, start, end)
    charge_ref = _digest(charge_id) if charge_id else ""
    event = (
        "charge:" + _digest(charge_ref, kind.value, revision)
        if charge_id
        else "observation:" + _digest(key, str(job.pk), kind.value, revision)
    )
    provenance = {"service": f"runpod_{component}", "funding": "metered_api"}
    if charge_ref:
        provenance["source_ref"] = charge_ref
    if evidence_ref:
        provenance["evidence_ref"] = evidence_ref
    with transaction.atomic():
        # Serialize all adapter checks for this account, including initially absent
        # periods and charge IDs. All writers must use this adapter.
        RunpodAccount.objects.select_for_update().get(pk=job.account_id)
        existing = CostObservation.objects.filter(
            provider="runpod",
            account_ref=job.account_id,
            source=SOURCE,
        )
        overlaps = existing.filter(
            resource_type="pod",
            resource_id=resource_id,
            provenance__service=f"runpod_{component}",
            period_start__lt=period_end,
            period_end__gt=period_start,
        ).exclude(operation__operation_key=key)
        if overlaps.exists():
            msg = "RunPod component periods overlap"
            raise CostConflictError(msg)
        if (
            charge_ref
            and existing.filter(provenance__source_ref=charge_ref)
            .exclude(operation__operation_key=key)
            .exists()
        ):
            msg = "RunPod charge already belongs to another component or period"
            raise CostConflictError(msg)
        return record_observation(
            actor=actor,
            owner=owner,
            operation=_operation(job, key, resource_id),
            observation=ObservationSpec(
                source=SOURCE,
                source_event_id=event,
                kind=kind,
                revision=revision,
                occurred_at=period_end,
                period_start=period_start,
                period_end=period_end,
                amount=amount,
                attempt_ref=str(job.pk),
                resource_type="pod",
                resource_id=resource_id,
                provenance=provenance,
            ),
        )
