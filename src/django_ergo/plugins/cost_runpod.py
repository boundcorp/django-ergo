"""Passive RunPod cost projection adapter over the durable `RunpodJob` index.

Reads nothing from provider APIs or rental files and edits no lifecycle code: the
caller supplies evidence it already holds. Compute and storage are separate
operations. Termination, cleanup and the REST request budget are never money
evidence; only an explicit provider charge settles an estimate. Never retries or
alters the rental.
"""

from datetime import UTC
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from django_ergo.costs.ledger import record_observation
from django_ergo.costs.types import CostKind
from django_ergo.costs.types import ObservationSpec
from django_ergo.costs.types import OperationSpec

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
        job_ref=str(job.pk),
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

    Operation = job/component/resource/period start, so a provider charge for the
    same period supersedes the estimate and storage never merges with compute.
    `kind` ESTIMATE/UNKNOWN describe our own knowledge; PROVIDER_REPORTED/FINAL need
    an explicit `amount` and `charge_id` from provider evidence. UNKNOWN takes no
    amount. The caller must not use stop/delete/cleanup as reconciliation evidence.
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
    owner = _owner(job)
    start = period_start.astimezone(UTC) if period_start.tzinfo else period_start
    key = f"{job.pk}:{component}:{resource_id}:{start:%Y%m%dT%H%M%SZ}"
    event = f"{key}:{charge_id}" if charge_id else f"{key}:{kind.value}:{revision}"
    provenance = {"service": f"runpod_{component}", "funding": "metered_api"}
    if evidence_ref:
        provenance["evidence_ref"] = evidence_ref
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
            resource_type="pod",
            resource_id=resource_id,
            provenance=provenance,
        ),
    )
