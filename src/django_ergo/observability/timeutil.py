"""Time windows and steps: explicit timezone, resolved once, bounded."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from .errors import INVALID_QUERY
from .errors import ObservabilityError

MAX_WINDOW = timedelta(days=7)
MIN_STEP = 15
DEFAULT_MAX_POINTS = 1000
MAX_POINTS = 2000

_DURATION = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _bad(message: str, **kw) -> ObservabilityError:
    return ObservabilityError(INVALID_QUERY, message, **kw)


def load_tz(name: str | None) -> ZoneInfo:
    """An IANA zone. A missing or unknown name is an error: there is no default."""
    if not name or not isinstance(name, str):
        raise _bad(
            "tz is required: pass an IANA timezone such as 'UTC' or 'Europe/Berlin'"
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise _bad(f"unknown IANA timezone {name!r}") from None


def parse_duration(text: str | float, *, what: str = "duration") -> int:
    """Seconds from ``90``, ``"90"``, ``"30s"``, ``"15m"``, ``"6h"``, ``"2d"``, ``"1w"``."""
    if isinstance(text, bool):
        raise _bad(f"invalid {what}")
    if isinstance(text, int | float):
        seconds = float(text)
    else:
        raw = str(text).strip().lower()
        match = _DURATION.match(raw)
        if match:
            seconds = int(match.group(1)) * _UNITS[match.group(2)]
        else:
            try:
                seconds = float(raw)
            except ValueError:
                raise _bad(
                    f"invalid {what} {text!r}: use e.g. 30s, 15m, 6h, 2d"
                ) from None
    if not math.isfinite(seconds) or seconds <= 0 or seconds != int(seconds):
        raise _bad(
            f"invalid {what} {text!r}: must be a positive whole number of seconds"
        )
    return int(seconds)


def parse_instant(text: str, tz: ZoneInfo) -> datetime:
    """An aware UTC datetime from ISO 8601.

    A string with an offset (or ``Z``) is taken as is. A naive string is local time in
    ``tz``; one that is ambiguous (DST fold) or doesn't exist (DST gap) is rejected,
    so the caller must add an explicit offset.
    """
    raw = str(text).strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise _bad(
            f"invalid time {text!r}: use ISO 8601, e.g. 2025-03-09T14:30:00"
        ) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    first = parsed.replace(tzinfo=tz, fold=0)
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != parsed:
        raise _bad(
            f"{text!r} does not exist in {tz.key} (clocks skip it); pick another time or add an offset"
        )
    if first.utcoffset() != parsed.replace(tzinfo=tz, fold=1).utcoffset():
        raise _bad(
            f"{text!r} is ambiguous in {tz.key} (clocks repeat); add an explicit UTC offset"
        )
    return first.astimezone(UTC)


def minimum_step(span_seconds: float, max_points: int) -> int:
    """Smallest whole step giving at most ``max_points`` points over the span."""
    if span_seconds <= 0:
        return MIN_STEP
    return max(MIN_STEP, math.floor(span_seconds / max_points) + 1)


def point_count(span_seconds: float, step: int) -> int:
    return math.floor(span_seconds / step) + 1


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC
    end: datetime  # UTC
    tz: ZoneInfo
    step: int  # seconds
    points: int
    step_was_default: bool

    @property
    def span(self) -> float:
        return (self.end - self.start).total_seconds()

    def describe(self) -> str:
        local = f"{self.start.astimezone(self.tz).isoformat()} .. {self.end.astimezone(self.tz).isoformat()}"
        utc = f"{self.start.isoformat()} .. {self.end.isoformat()}"
        return (
            f"range UTC {utc}; local ({self.tz.key}) {local}; "
            f"step {self.step}s ({'default' if self.step_was_default else 'explicit'}); "
            f"{self.points} points/series"
        )


def resolve_instant(
    *, tz: str | None, at: str | None, now: datetime
) -> tuple[datetime, ZoneInfo]:
    zone = load_tz(tz)
    when = parse_instant(at, zone) if at else now.astimezone(UTC)
    if when > now:
        raise _bad("time is in the future")
    return when, zone


def resolve_window(
    *,
    tz: str | None,
    now: datetime,
    window: str | int | None = None,
    start: str | None = None,
    end: str | None = None,
    step: str | int | None = None,
) -> Window:
    """Resolve a range once. Either ``window`` (relative, ending at ``end`` or ``now``)
    or ``start`` + ``end``. Never coarsens an explicit step."""
    zone = load_tz(tz)
    now = now.astimezone(UTC)
    if window is not None and start is not None:
        raise _bad("give either window or start/end, not both")
    end_dt = parse_instant(end, zone) if end else now
    if window is not None:
        start_dt = end_dt - timedelta(seconds=parse_duration(window, what="window"))
    elif start is not None:
        start_dt = parse_instant(start, zone)
    else:
        raise _bad("give window (e.g. '6h') or start and end")
    if end_dt > now:
        raise _bad("end is in the future")
    if end_dt < start_dt:
        raise _bad("end is before start")
    span = (end_dt - start_dt).total_seconds()
    if span > MAX_WINDOW.total_seconds():
        raise _bad("window exceeds the 7 day maximum; narrow it")
    if step is None:
        seconds = minimum_step(span, DEFAULT_MAX_POINTS)
        default = True
    else:
        seconds = parse_duration(step, what="step")
        default = False
        if seconds < MIN_STEP:
            raise _bad(
                f"step {seconds}s is below the {MIN_STEP}s minimum",
                minimum_step=MIN_STEP,
            )
        needed = minimum_step(span, MAX_POINTS)
        if point_count(span, seconds) > MAX_POINTS:
            raise _bad(
                f"step {seconds}s would return {point_count(span, seconds)} points per series "
                f"(max {MAX_POINTS}); use a step of at least {needed}s",
                minimum_step=needed,
            )
    return Window(start_dt, end_dt, zone, seconds, point_count(span, seconds), default)
