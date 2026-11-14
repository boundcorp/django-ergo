"""Transactional passive ingestion and owner-scoped, currency-isolated projections."""

import hashlib
import json
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from decimal import localcontext

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Case
from django.db.models import F
from django.db.models import Q
from django.db.models import Value
from django.db.models import When
from django.db.models import Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from .models import CostObservation
from .models import CostOperation
from .types import CostConflictError
from .types import CostKind
from .types import ObservationSpec
from .types import OperationSpec

_MONETARY = [
    CostKind.UNKNOWN,
    CostKind.ESTIMATE,
    CostKind.PROVIDER_REPORTED,
    CostKind.FINAL,
]
_SEPARATE = [
    CostKind.RESERVATION,
    CostKind.RELEASE,
    CostKind.API_EQUIVALENT,
    CostKind.SUBSCRIPTION,
]


def _decimal_text(value):
    text = format(value, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) if value else "0"


def _timestamp(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or timezone.is_aware(value) != settings.USE_TZ:
        msg = "Event times must be datetimes matching Django USE_TZ"
        raise ValueError(msg)
    return value.astimezone(UTC) if settings.USE_TZ else value


def _canonical(value):
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    msg = "Unsupported ledger value"
    raise TypeError(msg)


def _authorize(actor, owner):
    if (
        not actor
        or not actor.is_authenticated
        or not actor.is_active
        or not actor.pk
        or not owner
        or not owner.pk
        or not owner.is_active
        or (actor.pk != owner.pk and not actor.is_superuser)
    ):
        msg = "Cost ledger ownership required"
        raise PermissionDenied(msg)


def _resource_identity(economic, data):
    if not data["resource_id"]:
        return
    identity = (data["resource_type"], data["resource_id"])
    if economic.resource_id:
        known = (economic.resource_type, economic.resource_id)
    else:
        known = (
            economic.observations.exclude(resource_id="")
            .values_list(
                "resource_type",
                "resource_id",
            )
            .first()
        )
    if known is not None and known != identity:
        msg = "Economic operation cannot change provider resource"
        raise CostConflictError(msg)


def record_observation(
    *, actor, operation: OperationSpec, observation: ObservationSpec, owner=None
):
    """Return (immutable observation, created). Never send or retry financial work.

    A trusted adapter supplies the already-authorized principal as actor. HTTP/tool
    callers must derive it from authentication, not a request-body owner identifier.
    A conflict rolls back the entire transaction, including a newly created operation.
    """
    owner = actor if owner is None else owner
    _authorize(actor, owner)
    operation_data = operation.payload()
    data = observation.payload()
    for key in ("occurred_at", "period_start", "period_end"):
        data[key] = _timestamp(data[key])
    # Canonicalize numeric representations; Decimal('1.0') and Decimal('1') replay.
    data["quantities"] = {
        key: None if value is None else _decimal_text(Decimal(value))
        for key, value in data["quantities"].items()
    }
    identity = {
        name: operation_data[name]
        for name in ("provider", "account_ref", "operation_key")
    }
    with transaction.atomic():
        economic, _ = CostOperation.objects.get_or_create(
            **identity,
            defaults={"owner": owner, **operation_data},
        )
        economic = CostOperation.objects.select_for_update().get(pk=economic.pk)
        if economic.owner_id != owner.pk or any(
            getattr(economic, key) != value for key, value in operation_data.items()
        ):
            msg = "Economic identity conflicts with existing attribution"
            raise CostConflictError(msg)
        _resource_identity(economic, data)
        digest = hashlib.sha256(
            json.dumps(
                {"operation": str(economic.pk), **data},
                sort_keys=True,
                separators=(",", ":"),
                default=_canonical,
            ).encode()
        ).hexdigest()
        source_identity = {
            "provider": economic.provider,
            "account_ref": economic.account_ref,
            "source": data["source"],
            "source_event_id": data["source_event_id"],
        }
        existing = CostObservation.objects.filter(**source_identity).first()
        if existing is not None:
            if existing.payload_sha256 != digest:
                msg = "Source replay conflicts with existing observation"
                raise CostConflictError(msg)
            return existing, False
        try:
            # Savepoint keeps the outer transaction usable for a uniqueness race.
            with transaction.atomic():
                result = CostObservation.objects.create(
                    operation=economic,
                    provider=economic.provider,
                    account_ref=economic.account_ref,
                    payload_sha256=digest,
                    **data,
                )
        except IntegrityError:
            existing = CostObservation.objects.filter(**source_identity).first()
            if existing is not None and existing.payload_sha256 == digest:
                return existing, False
            msg = "Source identity or economic revision conflicts"
            raise CostConflictError(msg) from None
        return result, True


def latest_observations(actor):
    """One row per (operation, kind); explicitly ordered revisions, not arrival."""
    candidates = (
        CostObservation.objects.for_owner(actor)
        .annotate(
            _position=Window(
                expression=RowNumber(),
                partition_by=[F("operation_id"), F("kind")],
                order_by=F("revision").desc(),
            ),
        )
        .filter(_position=1)
    )
    return CostObservation.objects.for_owner(actor).filter(
        pk__in=candidates.values("pk")
    )


def effective_observations(actor):
    """One financial status per operation. Never add this to source/aggregate rows."""
    priority = Case(
        When(kind=CostKind.FINAL, then=Value(4)),
        When(kind=CostKind.PROVIDER_REPORTED, then=Value(3)),
        When(kind=CostKind.ESTIMATE, then=Value(2)),
        default=Value(1),
    )
    candidates = (
        CostObservation.objects.for_owner(actor)
        .filter(kind__in=_MONETARY)
        .annotate(
            _position=Window(
                expression=RowNumber(),
                partition_by=F("operation_id"),
                order_by=[priority.desc(), F("revision").desc()],
            ),
        )
        .filter(_position=1)
    )
    # Encapsulate ranking so a caller's kind/revision filter cannot resurrect history.
    return CostObservation.objects.for_owner(actor).filter(
        pk__in=candidates.values("pk")
    )


def _window_filter(queryset, since, until):
    """Window by the selected row's own event time, after ranking and ownership.

    A row with a bounded resource period is in the window when the period overlaps
    it (its amount is NOT prorated; see `partial`). Otherwise `occurred_at` decides.
    Rows with neither are undated: never silently assigned to any window.
    """
    dated = Q(period_start__isnull=False, period_start__lte=until, period_end__gt=since)
    dated |= Q(
        period_start__isnull=True, occurred_at__gte=since, occurred_at__lte=until
    )
    undated = queryset.filter(period_start__isnull=True, occurred_at__isnull=True)
    return queryset.filter(dated), undated


def summarize_costs(  # noqa: C901 - one pass keeps ranking, window and counts together
    actor, *, bot_name=None, project_ref=None, since=None, until=None
):
    """Return category/currency buckets, including unknown counts, never FX totals.

    known_amount is a subtotal, NOT a complete billed/spent total when unknown_count
    is nonzero. Reservation/release/API-equivalent/subscription buckets are separate
    informational observations, not additional spend. No permission decisions here.

    With `since` and `until` (aware datetimes) each bucket also carries
    `partial_period_count` (resource period crossing a window edge, amount whole) and
    `undated_count` (selected rows with no event time, excluded from the other counts).
    """
    windowed = since is not None or until is not None
    if windowed and (since is None or until is None or since > until):
        msg = "A window needs both bounds in order"
        raise ValueError(msg)
    filters = {}
    if bot_name is not None:
        filters["operation__bot_name"] = bot_name
    if project_ref is not None:
        filters["operation__project_ref"] = project_ref
    rows = [
        effective_observations(actor).filter(**filters),
        latest_observations(actor).filter(kind__in=_SEPARATE, **filters),
    ]
    buckets = {}

    def bucket_for(row):
        key = (row.kind, row.operation.currency)
        return buckets.setdefault(
            key,
            {
                "kind": row.kind,
                "currency": row.operation.currency,
                "known_amount": None,
                "known_count": 0,
                "unknown_count": 0,
                "operation_count": 0,
                **({"partial_period_count": 0, "undated_count": 0} if windowed else {}),
            },
        )

    with localcontext() as context:
        context.prec = 80
        for ranked in rows:
            queryset = ranked
            if windowed:
                queryset, undated = _window_filter(ranked, since, until)
                for row in undated.select_related("operation").iterator():
                    bucket_for(row)["undated_count"] += 1
            for row in queryset.select_related("operation").iterator():
                bucket = bucket_for(row)
                bucket["operation_count"] += 1
                if (
                    windowed
                    and row.period_start is not None
                    and (row.period_start < since or row.period_end > until)
                ):
                    bucket["partial_period_count"] += 1
                if row.amount is None:
                    bucket["unknown_count"] += 1
                else:
                    bucket["known_count"] += 1
                    bucket["known_amount"] = (
                        bucket["known_amount"] or Decimal("0")
                    ) + row.amount
    return [buckets[key] for key in sorted(buckets)]
