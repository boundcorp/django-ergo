"""Explicit, passive source adapters. Nothing here is scheduled or auto-invoked.

An adapter turns one already-durable local record into ledger observations. It
never sends, retries, cancels or alters the financial operation: a ledger failure
(permission, conflict, bad data) propagates to the caller and must not cause the
external operation to be repeated. Calls are idempotent by source identity.
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction

from .ledger import record_observation
from .types import CostKind
from .types import ObservationSpec
from .types import OperationSpec
from .types import normalize_tokens

INFERENCE_SOURCE = "inference-request-v1"


def require_owned(owner, *records):
    """Every referenced record must belong to the owner; opaque ids grant nothing."""
    for record in records:
        if record is not None and record.user_id != owner.pk:
            msg = "Referenced record belongs to another owner"
            raise PermissionDenied(msg)


def record_inference_request(*, actor, request):
    """Record one `InferenceRequest` as per-request observations.

    - reservation: `reserved_usd`, a ceiling, separate from spend
    - release: only when the request was refused before any HTTP request
    - estimate: priced `cost_usd` (list-price, never final), once finished
    - unknown: a finished attempt without a priced cost, not zero

    Only `billing_mode == "api"` rows are ledger-billable; others return [] (that is
    not evidence of free usage). Returns [(CostObservation, created)].
    """
    from django_ergo.conversation.models import InferenceOutcome

    owner = request.user
    if owner is None:
        msg = "A request without an owner cannot be attributed"
        raise PermissionDenied(msg)
    require_owned(owner, request.session, request.structured_call)
    if request.billing_mode != "api":
        return []
    operation = OperationSpec(
        provider=request.provider,
        account_ref=request.pool,
        operation_key=f"inference-request:{request.pk}",
        currency="USD",
        bot_name=request.bot_name,
    )
    session_ref = str(request.session_id) if request.session_id else ""
    attempt_ref = f"{request.pk}:{request.attempt}"
    provenance = {"model": request.model, "funding": "metered_api"}

    def spec(kind, suffix, amount, when, **extra):
        return ObservationSpec(
            source=INFERENCE_SOURCE,
            source_event_id=f"{request.pk}:{suffix}",
            kind=kind,
            revision=1,
            occurred_at=when,
            amount=amount,
            session_ref=session_ref,
            attempt_ref=attempt_ref,
            provenance=provenance,
            **extra,
        )

    specs = []
    if request.reserved_usd is not None:
        specs.append(
            spec(
                CostKind.RESERVATION,
                "reservation",
                request.reserved_usd,
                request.created_at,
            )
        )
    finished = request.finished_at
    if request.outcome == InferenceOutcome.NOT_SENT:
        if request.reserved_usd is not None:
            specs.append(
                spec(CostKind.RELEASE, "release", request.reserved_usd, finished)
            )
    elif request.outcome != InferenceOutcome.PENDING:
        # Prompt includes cache writes, but the source does not retain that
        # bucket. It cannot be subtracted to recover disjoint uncached input.
        tokens = normalize_tokens(
            input_tokens=request.prompt_tokens,
            input_semantics="total",
            cache_read=request.cached_tokens,
            output=request.output_tokens,
            reasoning=request.reasoning_tokens,
        )
        if request.cost_status == "priced" and request.cost_usd is not None:
            specs.append(
                spec(
                    CostKind.ESTIMATE,
                    "estimate",
                    request.cost_usd,
                    finished,
                    tokens=tokens,
                )
            )
        else:
            specs.append(
                spec(CostKind.UNKNOWN, "unknown", None, finished, tokens=tokens)
            )
    with transaction.atomic():
        return [
            record_observation(
                actor=actor, owner=owner, operation=operation, observation=s
            )
            for s in specs
        ]
