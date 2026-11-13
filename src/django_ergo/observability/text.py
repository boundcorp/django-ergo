"""Tool-facing text: provenance first, bounded, honest about absent vs zero."""

from __future__ import annotations

import math
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prometheus import QueryResult
    from .prometheus import Series
    from .timeutil import Window

MAX_TEXT_BYTES = 32 * 1024
SAMPLES_PER_SERIES = 12
SCRAPE_NOTE = (
    "Scrape health only says Prometheus could fetch the target's metrics endpoint "
    "(up=1). It does not prove the application is healthy, and a service with no "
    "target is a gap in monitoring, not a healthy service."
)


def trim(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    data = text.encode()
    if len(data) <= limit:
        return text
    cut = data[: limit - 80].decode(errors="ignore")
    return f"{cut}\n[truncated: output exceeded {limit} bytes]"


def fmt(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return f"{value:.6g}"


def _stamp(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(tz).isoformat(timespec="seconds")


def _labels(labels: dict) -> str:
    return "{" + ", ".join(f"{k}={v!r}" for k, v in labels.items()) + "}"


def _series(series: Series, kind: str, tz) -> list[str]:
    head = _labels(series.labels)
    if kind == "instant":
        ts, value = series.points[0]
        return [f"- {head} = {fmt(value)} @ {_stamp(ts, tz)}"]
    finite = [v for _, v in series.finite]
    notes = []
    if series.nan or series.pos_inf or series.neg_inf:
        notes.append(f"NaN={series.nan} +Inf={series.pos_inf} -Inf={series.neg_inf}")
    if series.gaps:
        notes.append(f"{series.gaps} gap(s): missing steps are not zero")
    stats = (
        f"min={fmt(min(finite))} max={fmt(max(finite))} avg={fmt(sum(finite) / len(finite))} "
        f"last={fmt(series.points[-1][1])}"
        if finite
        else "no finite samples"
    )
    line = f"- {head} n={len(series.points)} {stats}" + (
        f" [{'; '.join(notes)}]" if notes else ""
    )
    stride = max(1, len(series.points) // SAMPLES_PER_SERIES)
    picks = series.points[::stride][:SAMPLES_PER_SERIES]
    sample = ", ".join(f"{_stamp(ts, tz)}={fmt(v)}" for ts, v in picks)
    return [line, f"    samples: {sample}"]


def format_query(
    result: QueryResult,
    *,
    query: str,
    source: str,
    tz,
    window: Window | None = None,
    at: datetime | None = None,
    link: str = "",
) -> str:
    lines = [f"PromQL {result.kind} query: {query}", f"source: {source}"]
    if window:
        lines.append(window.describe())
    elif at:
        lines.append(
            f"evaluated at UTC {at.isoformat()}; local ({tz.key}) {at.astimezone(tz).isoformat()}"
        )
    lines.append(f"result type: {result.result_type}")
    if result.absent:
        lines.append(
            "ABSENT: the query matched no series. This is not zero and not healthy; "
            "the metric or label set may not exist, or the target may be missing."
        )
    elif result.string_value is not None:
        lines.append(f"string value: {result.string_value!r}")
    else:
        lines.append(f"{len(result.series)} series, {result.total_samples} samples")
        for series in result.series:
            lines += _series(series, result.kind, tz)
    if result.histogram_series:
        lines.append(
            f"UNSUPPORTED: {result.histogram_series} native-histogram series omitted "
            "(not plottable; query a derived value such as histogram_quantile())"
        )
    lines += [f"warning: {w}" for w in result.warnings]
    if link:
        lines.append(f"open in Grafana Explore: {link}")
    return trim("\n".join(lines))


def format_targets(
    info: dict, *, source: str, job: str | None, namespace: str | None, up: str = ""
) -> str:
    lines = [
        "Prometheus target health (current scrape status from /api/v1/targets)",
        f"source: {source}",
        SCRAPE_NOTE,
        f"filters: job={job or '*'} namespace={namespace or '*'}",
        f"{info['total']} active targets; health counts: {info['counts'] or 'none'}",
    ]
    if info["total"] == 0:
        lines.append(
            "NO MATCHING TARGETS: nothing is scraping this selector. That is a gap, not a healthy result."
        )
    for t in info["targets"]:
        where = " ".join(
            f"{k}={t[k]}" for k in ("job", "instance", "namespace", "pod") if t[k]
        )
        err = f" last_error={t['last_error']!r}" if t["last_error"] else ""
        lines.append(f"- {t['health']}: {where} last_scrape={t['last_scrape']}{err}")
    if info["truncated"]:
        lines.append("[target list truncated at 200; filter by job or namespace]")
    if up:
        lines += [
            "",
            "Separate: `up` sample values (scrape success history, not app health)",
            up,
        ]
    return trim("\n".join(lines))


def format_dashboard(summary: dict) -> str:
    lines = [
        f"Dashboard {summary['uid']}: {summary['title']}",
        f"folder: {summary['folder'] or '-'}  tags: {', '.join(summary['tags']) or '-'}  version: {summary['version']}",
        f"link: {summary['link'] or '(no public_url configured)'}",
    ]
    own = summary["ownership"]
    owner = own["owner"]
    owner_text = (
        owner
        if isinstance(owner, str)
        else ", ".join(f"{k}={v}" for k, v in owner.items())
    )
    lines += [
        f"provisioned: {own['provisioned']}  provisioned_file: {own['provisioned_file'] or '-'}",
        f"owner: {owner_text}",
        f"to change: {own['how_to_change']}",
        f"datasource uids used: {', '.join(summary['datasource_uids']) or '-'}",
    ]
    if summary["variables"]:
        lines.append("variables:")
        lines += [
            f"- {v['name']} ({v['type']}) query={v['query']!r}"
            for v in summary["variables"]
        ]
    lines.append(f"panels ({len(summary['panels'])}):")
    for p in summary["panels"]:
        lines.append(f"- id={p['id']} [{p['type']}] {p['title']}")
        lines += [
            f"    {t['ref']} ds={t['datasource'] or '-'}: {t['expr']}"
            for t in p["targets"]
        ]
    return trim("\n".join(lines))
