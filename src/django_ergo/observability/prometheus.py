"""Bounded instant/range PromQL and target health, through Grafana or direct."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import MAXYEAR
from datetime import MINYEAR
from datetime import UTC
from datetime import datetime

from .errors import BOUNDS_EXCEEDED
from .errors import INVALID_QUERY
from .errors import UPSTREAM_UNAVAILABLE
from .errors import ObservabilityError
from .http import BoundedClient
from .http import Limits
from .profile import UID_RE
from .profile import Profile
from .timeutil import DEFAULT_MAX_POINTS
from .timeutil import MAX_POINTS
from .timeutil import Window
from .timeutil import resolve_instant
from .timeutil import resolve_window

MAX_EXPR_BYTES = 8 * 1024
MAX_SERIES = 100
MAX_SAMPLES = 100_000
PROM_TIMEOUT = "8s"
MAX_TARGETS = 200

_PROXY_PATHS = re.compile(
    r"/api/datasources/proxy/uid/[A-Za-z0-9_-]{1,128}/api/v1/(query|query_range|targets)"
)
_DIRECT_PATHS = re.compile(r"/api/v1/(query|query_range|targets)")


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]] = field(default_factory=list)  # (unix ts, value)
    nan: int = 0
    pos_inf: int = 0
    neg_inf: int = 0
    gaps: int = 0  # missing steps inside the series

    @property
    def finite(self) -> list[tuple[float, float]]:
        return [p for p in self.points if math.isfinite(p[1])]


@dataclass
class QueryResult:
    kind: str  # instant | range
    result_type: str  # scalar | string | vector | matrix
    series: list[Series] = field(default_factory=list)
    string_value: str | None = None
    histogram_series: int = 0
    warnings: list[str] = field(default_factory=list)
    total_samples: int = 0

    @property
    def absent(self) -> bool:
        """No series at all: the query matched nothing. This is not zero."""
        return (
            self.result_type in {"vector", "matrix"}
            and not self.series
            and not self.histogram_series
        )

    @property
    def histograms_only(self) -> bool:
        return bool(self.histogram_series) and not self.series


def _number(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ObservabilityError(
            UPSTREAM_UNAVAILABLE, "upstream sent a malformed sample value"
        ) from None


def _tally(series: Series, value: float) -> None:
    if math.isnan(value):
        series.nan += 1
    elif value == math.inf:
        series.pos_inf += 1
    elif value == -math.inf:
        series.neg_inf += 1


def _malformed() -> ObservabilityError:
    return ObservabilityError(
        UPSTREAM_UNAVAILABLE, "upstream sent an unexpected result shape"
    )


def _pair(raw):
    if not isinstance(raw, list) or len(raw) != 2:  # noqa: PLR2004
        raise _malformed()
    return raw


def _timestamp(raw) -> float:
    stamp = _number(raw)
    try:
        dt = datetime.fromtimestamp(stamp, UTC)
    except (ValueError, OverflowError, OSError):
        raise _malformed() from None
    # Leave room for conversion into any supported time zone.
    if dt.year in (MINYEAR, MAXYEAR):
        raise _malformed()
    return stamp


def _labels(raw) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise _malformed()
    return raw


def _warnings(payload: dict, http: BoundedClient) -> list[str]:
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise _malformed()
    return [http.sanitize(w, limit=200) for w in warnings[:10]]


def normalize(
    data: dict, kind: str, step: int | None = None, *, max_points: int = MAX_POINTS
) -> QueryResult:
    """Normalize a Prometheus ``data`` object, enforcing series/sample limits."""
    if not isinstance(data, dict) or not isinstance(data.get("resultType"), str):
        raise _malformed()
    rtype = data["resultType"]
    raw = data.get("result")
    result = QueryResult(kind=kind, result_type=rtype)
    if rtype == "scalar":
        ts, value = _pair(raw)
        series = Series({})
        series.points.append((_timestamp(ts), _number(value)))
        _tally(series, series.points[0][1])
        result.series.append(series)
        result.total_samples = 1
        return result
    if rtype == "string":
        ts, value = _pair(raw)
        _timestamp(ts)
        if not isinstance(value, str):
            raise _malformed()
        result.string_value = value
        return result
    if rtype not in {"vector", "matrix"} or not isinstance(raw, list):
        raise _malformed()
    if len(raw) > MAX_SERIES:
        raise _too_many_series()
    for entry in raw:
        if not isinstance(entry, dict):
            raise _malformed()
        labels = _labels(entry.get("metric", {}))
        float_key = "values" if rtype == "matrix" else "value"
        hist_key = "histograms" if rtype == "matrix" else "histogram"
        if float_key not in entry and hist_key not in entry:
            raise _malformed()
        values = entry.get(float_key, [] if rtype == "matrix" else None)
        histograms = entry.get(hist_key, [] if rtype == "matrix" else None)
        if rtype == "vector":
            values = [values] if float_key in entry else []
            histograms = [histograms] if hist_key in entry else []
        if not isinstance(values, list) or not isinstance(histograms, list):
            raise _malformed()
        count = len(values) + len(histograms)
        result.total_samples += count
        if result.total_samples > MAX_SAMPLES:
            raise ObservabilityError(
                BOUNDS_EXCEEDED,
                f"result has more than {MAX_SAMPLES} samples; narrow the window, raise the step or aggregate",
            )
        if count > max_points:
            raise ObservabilityError(
                BOUNDS_EXCEEDED, f"result has more than {max_points} points per series"
            )
        if hist_key in entry:
            result.histogram_series += 1
            for sample in histograms:
                ts, histogram = _pair(sample)
                _timestamp(ts)
                if not isinstance(histogram, dict):
                    raise _malformed()
        if float_key not in entry:
            continue
        series = Series(labels)
        previous = None
        for sample in values:
            ts, raw_value = _pair(sample)
            value = _number(raw_value)
            stamp = _timestamp(ts)
            if step and previous is not None and stamp - previous > step * 1.5:
                series.gaps += 1
            previous = stamp
            _tally(series, value)
            series.points.append((stamp, value))
        result.series.append(series)
    return result


def _too_many_series() -> ObservabilityError:
    return ObservabilityError(
        BOUNDS_EXCEEDED,
        f"result has more than {MAX_SERIES} series; aggregate (sum/topk/avg by) or add label matchers",
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class PromClient:
    """PromQL and target status for one profile and one allowlisted datasource."""

    def __init__(
        self,
        profile: Profile,
        datasource: str | None,
        *,
        token: str | None = None,
        limits: Limits | None = None,
        clock=lambda: datetime.now(UTC),
    ):
        self.profile = profile
        self.clock = clock
        if profile.mode == "direct":
            if len(profile.datasources) > 1:
                raise ObservabilityError(
                    INVALID_QUERY, "direct mode requires a single datasource identity"
                )
            self.datasource = (
                profile.check_datasource(datasource)
                if profile.datasources or datasource
                else ""
            )
            self.http = BoundedClient(
                profile.prometheus_url,
                allowed=[_DIRECT_PATHS],
                token=None,
                limits=limits,
            )
            self.base = "/api/v1"
        else:
            self.datasource = profile.check_datasource(datasource)
            if not UID_RE.fullmatch(self.datasource):
                raise ObservabilityError(INVALID_QUERY, "invalid datasource uid")
            self.http = BoundedClient(
                profile.grafana_url, allowed=[_PROXY_PATHS], token=token, limits=limits
            )
            self.base = f"/api/datasources/proxy/uid/{self.datasource}/api/v1"

    @property
    def source(self) -> str:
        via = (
            "direct prometheus"
            if self.profile.mode == "direct"
            else "grafana datasource proxy"
        )
        uid = f" datasource={self.datasource}" if self.datasource else ""
        return f"profile={self.profile.name} {via}{uid}"

    def _check_expr(self, expr: str) -> str:
        if not isinstance(expr, str) or not expr.strip():
            raise ObservabilityError(INVALID_QUERY, "expression is empty")
        if len(expr.encode()) > MAX_EXPR_BYTES:
            raise ObservabilityError(
                INVALID_QUERY, f"expression exceeds {MAX_EXPR_BYTES} bytes"
            )
        return expr

    def _call(self, endpoint: str, params: dict) -> dict:
        payload = self.http.get_json(f"{self.base}/{endpoint}", params)
        if not isinstance(payload, dict) or payload.get("status") != "success":
            detail = ""
            if isinstance(payload, dict):
                fields = (payload.get("errorType", ""), payload.get("error", ""))
                detail = self.http.sanitize(
                    ": ".join(field for field in fields if isinstance(field, str)),
                    limit=300,
                )
            raise ObservabilityError(
                INVALID_QUERY,
                f"query failed: {detail}" if detail else "query failed",
            )
        if not isinstance(payload.get("data"), dict):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "upstream sent an unexpected response"
            )
        return payload

    def _sanitize_result(self, result: QueryResult, payload: dict) -> None:
        result.warnings += _warnings(payload, self.http)
        if result.string_value is not None:
            result.string_value = self.http.sanitize(result.string_value, limit=32768)
        for series in result.series:
            series.labels = {
                self.http.sanitize(k, limit=32768): self.http.sanitize(v, limit=32768)
                for k, v in series.labels.items()
            }

    def instant(
        self, expr: str, *, tz: str | None, at: str | None = None
    ) -> tuple[QueryResult, datetime, object]:
        expr = self._check_expr(expr)
        when, zone = resolve_instant(tz=tz, at=at, now=self.clock())
        payload = self._call(
            "query",
            {"query": expr, "time": f"{when.timestamp():.3f}", "timeout": PROM_TIMEOUT},
        )
        result = normalize(payload["data"], "instant")
        self._sanitize_result(result, payload)
        return result, when, zone

    def range(
        self,
        expr: str,
        *,
        tz: str | None,
        window: str | int | None = None,
        start: str | None = None,
        end: str | None = None,
        step: str | int | None = None,
    ) -> tuple[QueryResult, Window]:
        expr = self._check_expr(expr)
        win = resolve_window(
            tz=tz, now=self.clock(), window=window, start=start, end=end, step=step
        )
        payload = self._call(
            "query_range",
            {
                "query": expr,
                "start": f"{win.start.timestamp():.3f}",
                "end": f"{win.end.timestamp():.3f}",
                "step": str(win.step),
                "timeout": PROM_TIMEOUT,
            },
        )
        max_points = DEFAULT_MAX_POINTS if win.step_was_default else MAX_POINTS
        result = normalize(payload["data"], "range", win.step, max_points=max_points)
        self._sanitize_result(result, payload)
        return result, win

    def targets(self, *, job: str | None = None, namespace: str | None = None) -> dict:
        """Current scrape status from ``/api/v1/targets``. Scrape success is not app health."""
        payload = self._call("targets", {"state": "active"})
        active = payload["data"].get("activeTargets", [])
        if not isinstance(active, list):
            raise _malformed()
        rows = []
        for target in active:
            if not isinstance(target, dict):
                raise _malformed()
            labels = _labels(target.get("labels", {}))
            for key in ("health", "lastScrape", "lastError"):
                if key in target and not isinstance(target[key], str):
                    raise _malformed()
            if job and labels.get("job") != job:
                continue
            if namespace and labels.get("namespace") != namespace:
                continue
            rows.append(
                {
                    "job": self.http.sanitize(labels.get("job", ""), limit=300),
                    "instance": self.http.sanitize(
                        labels.get("instance", ""), limit=300
                    ),
                    "namespace": self.http.sanitize(
                        labels.get("namespace", ""), limit=300
                    ),
                    "pod": self.http.sanitize(labels.get("pod", ""), limit=300),
                    "health": self.http.sanitize(
                        target.get("health", "unknown"), limit=300
                    ),
                    "last_scrape": self.http.sanitize(
                        target.get("lastScrape", ""), limit=300
                    ),
                    "last_error": self.http.sanitize(
                        target.get("lastError", ""), limit=300
                    ),
                }
            )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["health"]] = counts.get(row["health"], 0) + 1
        return {
            "total": len(rows),
            "counts": counts,
            "targets": rows[:MAX_TARGETS],
            "truncated": len(rows) > MAX_TARGETS,
        }

    def up_selector(self, job: str | None, namespace: str | None) -> str:
        matchers = []
        for name, value in (("job", job), ("namespace", namespace)):
            if value:
                matchers.append(f'{name}="{_escape(value)}"')
        return "up" + ("{" + ",".join(matchers) + "}" if matchers else "")
