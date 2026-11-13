"""Provider-neutral values. No network, credentials, pricing, or dispatch."""

import re
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class CostKind(StrEnum):
    RESERVATION = "reservation"
    RELEASE = "release"
    LIFECYCLE = "lifecycle"
    USAGE = "usage"
    UNKNOWN = "unknown"
    ESTIMATE = "estimate"
    API_EQUIVALENT = "api_equivalent"
    SUBSCRIPTION = "subscription"
    PROVIDER_REPORTED = "provider_reported"
    FINAL = "final"


class CostConflictError(ValueError):
    """An identity was replayed with different economic facts; nothing is changed."""


_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_CURRENCY = re.compile(r"[A-Z][A-Z0-9_]{1,15}\Z")
_PROVENANCE = {
    "model",
    "region",
    "service",
    "price_reference",
    "evidence_ref",
    "funding",
    "lifecycle",
    "source_model",
    "source_ref",
    "parser_version",
}
_FUNDING = {"metered_api", "subscription", "external_shared", "unknown", "promo_credit"}
DECIMAL_PLACES = 18
INTEGER_DIGITS = 20
MAX_TOKENS = 9_223_372_036_854_775_807
MAX_REVISION = 2_147_483_647
MAX_UNITS = 24
MAX_PROVENANCE_CODE = 128


def code(value: str, *, empty=False, limit=200) -> str:
    """Only bounded codes/opaque references, never arbitrary text or URLs."""
    if not isinstance(value, str):
        msg = "Expected a safe code"
        raise TypeError(msg)
    if empty and value == "":
        return value
    if (
        len(value) > limit
        or not _CODE.fullmatch(value)
        or "://" in value
        or value.lower().startswith(("sk-", "bearer", "ghp_", "github_pat_"))
    ):
        msg = "Expected a safe code"
        raise ValueError(msg)
    return value


def decimal_value(value, *, nonnegative=False):
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        msg = "Use finite Decimal values, not floats"
        raise TypeError(msg)
    # Match storage exactly: reject lossy rounding and overflowing SQL numerics.
    if (
        value.as_tuple().exponent < -DECIMAL_PLACES
        or value.adjusted() >= INTEGER_DIGITS
    ):
        msg = "Decimal exceeds ledger precision (38 digits, 18 decimals)"
        raise ValueError(msg)
    if nonnegative and value < 0:
        msg = "Measured quantities cannot be negative"
        raise ValueError(msg)
    return value


@dataclass(frozen=True)
class TokenUsage:
    """Input buckets are disjoint; reasoning is already included in output."""

    uncached_input: int | None = None
    cache_write: int | None = None
    cache_read: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def __post_init__(self):
        for value in asdict(self).values():
            if value is not None and (
                type(value) is not int or value < 0 or value > MAX_TOKENS
            ):
                msg = "Token counts must be nonnegative signed-bigint integers"
                raise ValueError(msg)
        if (
            self.reasoning is not None
            and self.output is not None
            and self.reasoning > self.output
        ):
            msg = "Reasoning must be a subset of output"
            raise ValueError(msg)

    @property
    def total(self):
        parts = (self.uncached_input, self.cache_write, self.cache_read, self.output)
        return None if any(v is None for v in parts) else sum(parts)


def normalize_tokens(  # noqa: PLR0913 - explicit provider token partition and semantics
    *,
    input_tokens: int | None,
    input_semantics: str,
    cache_write: int | None = None,
    cache_read: int | None = None,
    output: int | None = None,
    reasoning: int | None = None,
) -> TokenUsage:
    """Do not assume absent provider buckets are zero; adapters must state zero."""
    validated = TokenUsage(input_tokens, cache_write, cache_read, output, reasoning)
    if input_semantics == "uncached":
        return validated
    if input_semantics != "total":
        msg = "Input semantics must be total or uncached"
        raise ValueError(msg)
    if (
        input_tokens is not None
        and sum(value for value in (cache_write, cache_read) if value is not None)
        > input_tokens
    ):
        msg = "Cache buckets exceed total input"
        raise ValueError(msg)
    uncached = None
    if input_tokens is not None and cache_write is not None and cache_read is not None:
        uncached = input_tokens - cache_write - cache_read
    return TokenUsage(uncached, cache_write, cache_read, output, reasoning)


@dataclass(frozen=True)
class OperationSpec:
    provider: str
    account_ref: str
    operation_key: str
    currency: str
    bot_name: str = ""
    project_ref: str = ""
    job_ref: str = ""
    resource_type: str = ""
    resource_id: str = ""

    def payload(self):
        result = asdict(self)
        for name, value in result.items():
            if name != "currency":
                code(
                    value,
                    empty=name not in {"provider", "account_ref", "operation_key"},
                )
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            msg = "Explicit currency/unit code required; no implicit FX"
            raise ValueError(msg)
        if bool(self.resource_type) != bool(self.resource_id):
            msg = "Resource type and provider resource ID must appear together"
            raise ValueError(msg)
        return result


def _quantities(values):
    if not isinstance(values, dict) or len(values) > MAX_UNITS:
        msg = "Expected at most 24 measured units"
        raise ValueError(msg)
    result = {}
    for unit, value in values.items():
        code(unit, limit=64)
        quantity = decimal_value(value, nonnegative=True)
        result[unit] = None if quantity is None else str(quantity)
    return result


def _provenance(values):
    if not isinstance(values, dict) or not values.keys() <= _PROVENANCE:
        msg = "Only allowlisted code-valued provenance is accepted"
        raise ValueError(msg)
    result = {}
    for key, value in values.items():
        if (
            key == "model"
            and isinstance(value, str)
            and len(value) <= MAX_PROVENANCE_CODE
        ):
            for segment in value.split("/"):
                code(segment, limit=MAX_PROVENANCE_CODE)
            result[key] = value
        else:
            result[key] = code(value, limit=MAX_PROVENANCE_CODE)
    if "funding" in result and result["funding"] not in _FUNDING:
        msg = "Unknown funding classification"
        raise ValueError(msg)
    return result


@dataclass(frozen=True)
class ObservationSpec:
    source: str
    source_event_id: str
    kind: CostKind
    revision: int
    occurred_at: datetime | None = None
    amount: Decimal | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    session_ref: str = ""
    worker_ref: str = ""
    tool_ref: str = ""
    attempt_ref: str = ""
    quantities: dict[str, Decimal | None] = field(default_factory=dict)
    resource_type: str = ""
    resource_id: str = ""
    tokens: TokenUsage = field(default_factory=TokenUsage)
    provenance: dict[str, str] = field(default_factory=dict)

    def payload(self):
        for name in (
            "source",
            "source_event_id",
            "session_ref",
            "worker_ref",
            "tool_ref",
            "attempt_ref",
            "resource_type",
            "resource_id",
        ):
            code(getattr(self, name), empty=name not in {"source", "source_event_id"})
        if bool(self.resource_type) != bool(self.resource_id):
            msg = "Resource type and provider resource ID must appear together"
            raise ValueError(msg)
        kind = CostKind(self.kind)
        if type(self.revision) is not int or not 1 <= self.revision <= MAX_REVISION:
            msg = "Revision must be an explicit positive integer"
            raise ValueError(msg)
        amount = decimal_value(self.amount)
        if (
            kind
            in {
                CostKind.UNKNOWN,
                CostKind.USAGE,
                CostKind.LIFECYCLE,
                CostKind.SUBSCRIPTION,
            }
            and amount is not None
        ):
            msg = "This kind is nonmonetary; use a separate invoice operation"
            raise ValueError(msg)
        if kind in {CostKind.FINAL, CostKind.PROVIDER_REPORTED} and amount is None:
            msg = "Reported/settled money requires an evidenced amount"
            raise ValueError(msg)
        if (self.period_start is None) != (self.period_end is None):
            msg = "Observed resource periods must be bounded"
            raise ValueError(msg)
        if self.period_start is not None and self.period_end < self.period_start:
            msg = "Period end precedes start"
            raise ValueError(msg)
        if not isinstance(self.tokens, TokenUsage):
            msg = "Expected TokenUsage"
            raise TypeError(msg)
        quantities = _quantities(self.quantities)
        provenance = _provenance(self.provenance)
        return {
            "source": self.source,
            "source_event_id": self.source_event_id,
            "kind": kind.value,
            "revision": self.revision,
            "occurred_at": self.occurred_at,
            "amount": amount,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "session_ref": self.session_ref,
            "worker_ref": self.worker_ref,
            "tool_ref": self.tool_ref,
            "attempt_ref": self.attempt_ref,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "quantities": quantities,
            "provenance": provenance,
            **{f"tokens_{key}": value for key, value in asdict(self.tokens).items()},
        }
