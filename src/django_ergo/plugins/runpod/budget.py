"""Pure pre-rental cost admission; this module neither rents nor terminates resources.

All prices and project balances are exact USD Decimals. A reservation is a
conservative whole-cent ceiling, not an invoice or a guarantee: a failed or
late termination can incur charges beyond the authorized rental interval.
The caller must independently enforce termination and reconcile actual invoices.
"""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

HARD_AUTHORIZATION_USD = Decimal("3.00")
MAX_RENTAL_SECONDS = 3600


@dataclass(frozen=True)
class Evidence:
    """An independently verified nonnegative USD amount, or USD/hour rate.

    Zero is valid only when it too is verified; unknown is represented by None.
    """

    amount: Decimal | None
    verified: bool


@dataclass(frozen=True)
class RentalTimeline:
    """Sequential phase bounds within one absolute rental interval, in seconds.

    Unallocated time is allowed and is nevertheless billed as compute. All
    phases, including termination, must fit in rental_seconds; this is a plan,
    not evidence that termination will actually succeed.
    """

    rental_seconds: int
    startup_seconds: int
    download_seconds: int
    compile_seconds: int
    generation_seconds: int
    export_seconds: int
    termination_seconds: int


@dataclass(frozen=True)
class Admission:
    """Admission result; reservation_usd is a ceiling, never actual spend.

    Rejected requests with complete valid pricing retain their calculated
    reservation for explanation. Invalid or unknown inputs have no reservation.
    """

    allowed: bool
    reason: str
    reservation_usd: Decimal | None = None


def _amount(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def _evidenced(value: object) -> bool:
    return (
        isinstance(value, Evidence) and value.verified is True and _amount(value.amount)
    )


def admit_rental(  # noqa: PLR0911, PLR0913
    *,
    timeline: RentalTimeline,
    compute_hourly_usd: Evidence,
    storage_usd: Evidence,
    network_usd: Evidence,
    fees_usd: Evidence,
    project_cap_usd: Decimal,
    project_committed_usd: Decimal,
) -> Admission:
    """Evaluate a single rental against the $3 hard limit and project cap.

    The verified compute rate applies to the entire absolute rental interval;
    storage, network, and fees are separately verified upper bounds for that
    interval. project_committed_usd includes existing actual charges and all
    outstanding reservations, without double-counting reconciled reservations.
    The caller must atomically reserve the returned amount with other project
    admissions; this pure function cannot prevent concurrent overspending.

    Reject malformed, missing, unverified, negative, or nonfinite inputs. Sum
    exact rational values before rounding the reservation upward to whole cents,
    then compare that reservation with both caps. No invoice is calculated.
    """
    if not isinstance(timeline, RentalTimeline):
        return Admission(allowed=False, reason="invalid timeline")
    durations = (
        timeline.rental_seconds,
        timeline.startup_seconds,
        timeline.download_seconds,
        timeline.compile_seconds,
        timeline.generation_seconds,
        timeline.export_seconds,
        timeline.termination_seconds,
    )
    if (
        any(type(value) is not int or value < 0 for value in durations)
        or not 0 < timeline.rental_seconds <= MAX_RENTAL_SECONDS
        or sum(durations[1:]) > timeline.rental_seconds
    ):
        return Admission(allowed=False, reason="invalid timeline")
    if not all(
        _evidenced(value)
        for value in (compute_hourly_usd, storage_usd, network_usd, fees_usd)
    ):
        return Admission(allowed=False, reason="missing or unverified pricing")
    if not _amount(project_cap_usd) or not _amount(project_committed_usd):
        return Admission(allowed=False, reason="invalid project budget")

    total = (
        Fraction(compute_hourly_usd.amount) * timeline.rental_seconds / 3600
        + Fraction(storage_usd.amount)
        + Fraction(network_usd.amount)
        + Fraction(fees_usd.amount)
    )
    cents = (total.numerator * 100 + total.denominator - 1) // total.denominator
    # Construct directly, avoiding the caller's Decimal precision and rounding.
    reservation = Decimal((0, tuple(map(int, str(cents))), -2))
    if reservation > HARD_AUTHORIZATION_USD:
        return Admission(
            allowed=False,
            reason="hard authorization exceeded",
            reservation_usd=reservation,
        )
    if Fraction(project_committed_usd) + Fraction(reservation) > Fraction(
        project_cap_usd
    ):
        return Admission(
            allowed=False, reason="project cap exceeded", reservation_usd=reservation
        )
    return Admission(allowed=True, reason="admitted", reservation_usd=reservation)
