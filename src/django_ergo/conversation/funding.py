"""Shared spend-control validation, independent of routing/configuration."""

from decimal import Decimal

FUNDING_DECIMALS = 4


class VersionConflict(ValueError):  # noqa: N818
    """The authenticated write was based on stale inspected terms."""


def usd(value, what: str, *, required: bool = True) -> Decimal | None:
    """A finite, non-negative amount of US dollars (strings, ints, floats or
    Decimals), or ValueError."""
    from decimal import InvalidOperation

    if value is None or value == "":
        if required:
            msg = f"{what} is required"
            raise ValueError(msg)
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        msg = f"{what} must be a number of US dollars"
        raise ValueError(msg)  # noqa: TRY004
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        msg = f"{what} must be a number of US dollars"
        raise ValueError(msg) from exc
    if not amount.is_finite() or amount < 0:
        msg = f"{what} must be a finite amount, 0 or more"
        raise ValueError(msg)
    if amount.normalize().as_tuple().exponent < -FUNDING_DECIMALS:
        msg = (
            f"{what} must have at most 4 decimal places; ambiguous rounding is refused"
        )
        raise ValueError(msg)
    if amount >= Decimal("1e12"):
        msg = f"{what} is too large"
        raise ValueError(msg)
    return amount
