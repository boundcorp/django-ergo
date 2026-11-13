"""Bounded instant/range PromQL and target health, through Grafana or direct."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from dataclasses import field
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
    except (TypeError, ValueError):
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


def normalize(data: dict, kind: str, step: int | None = None) -> QueryResult:
    """Normalize a Prometheus ``data`` object, enforcing series/sample limits."""
    if not isinstance(data, dict) or "resultType" not in data:
        raise ObservabilityError(
            UPSTREAM_UNAVAILABLE, "upstream sent an unexpected result shape"
        )
    rtype = data["resultType"]
    raw = data.get("result")
    result = QueryResult(kind=kind, result_type=rtype)
    if rtype == "scalar":
        series = Series({})
        series.points.append((_number(raw[0]), _number(raw[1])))
        _tally(series, series.points[0][1])
        result.series.append(series)
        result.total_samples = 1
        return result
    if rtype == "string":
        result.string_value = str(raw[1])
        return result
    if rtype not in {"vector", "matrix"} or not isinstance(raw, list):
        raise ObservabilityError(
            UPSTREAM_UNAVAILABLE, f"unsupported result type {rtype!r}"
        )
    for entry in raw:
        labels = {str(k): str(v) for k, v in (entry.get("metric") or {}).items()}
        if "histogram" in entry or "histograms" in entry:
            # Native histograms are reported, never flattened into floats.
            result.histogram_series += 1
            if result.histogram_series + len(result.series) > MAX_SERIES:
                raise _too_many_series()
            continue
        values = entry["values"] if rtype == "matrix" else [entry["value"]]
        series = Series(labels)
        previous = None
        for ts, raw_value in values:
            value = _number(raw_value)
            stamp = _number(ts)
            if step and previous is not None and stamp - previous > step * 1.5:
                series.gaps += 1
            previous = stamp
            _tally(series, value)
            series.points.append((stamp, value))
        result.total_samples += len(series.points)
        if result.total_samples > MAX_SAMPLES:
            raise ObservabilityError(
                BOUNDS_EXCEEDED,
                f"result has more than {MAX_SAMPLES} samples; narrow the window, raise the step or aggregate",
            )
        result.series.append(series)
        if len(result.series) + result.histogram_series > MAX_SERIES:
            raise _too_many_series()
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
            self.datasource = datasource or ""
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
                detail = (
                    f"{payload.get('errorType', '')}: {payload.get('error', '')}".strip(
                        ": "
                    )
                )
            raise ObservabilityError(
                INVALID_QUERY,
                f"query failed: {detail[:300]}" if detail else "query failed",
            )
        if not isinstance(payload.get("data"), dict | list):
            raise ObservabilityError(
                UPSTREAM_UNAVAILABLE, "upstream sent an unexpected response"
            )
        return payload

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
        result.warnings += [str(w)[:200] for w in payload.get("warnings", [])][:10]
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
        result = normalize(payload["data"], "range", win.step)
        result.warnings += [str(w)[:200] for w in payload.get("warnings", [])][:10]
        return result, win

    def targets(self, *, job: str | None = None, namespace: str | None = None) -> dict:
        """Current scrape status from ``/api/v1/targets``. Scrape success is not app health."""
        payload = self._call("targets", {"state": "active"})
        active = payload["data"].get("activeTargets", [])
        rows = []
        for target in active:
            labels = target.get("labels") or {}
            if job and labels.get("job") != job:
                continue
            if namespace and labels.get("namespace") != namespace:
                continue
            rows.append(
                {
                    "job": labels.get("job", ""),
                    "instance": labels.get("instance", ""),
                    "namespace": labels.get("namespace", ""),
                    "pod": labels.get("pod", ""),
                    "health": target.get("health", "unknown"),
                    "last_scrape": target.get("lastScrape", ""),
                    "last_error": str(target.get("lastError", ""))[:300],
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
