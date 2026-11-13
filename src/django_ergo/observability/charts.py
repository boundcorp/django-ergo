"""Data charts (matplotlib, Agg) and verification of server-rendered PNGs."""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from .errors import BOUNDS_EXCEEDED
from .errors import INVALID_QUERY
from .errors import NOT_CONFIGURED
from .errors import UPSTREAM_UNAVAILABLE
from .errors import ObservabilityError

if TYPE_CHECKING:
    from .prometheus import QueryResult
    from .timeutil import Window

MAX_WIDTH = 1600
MAX_HEIGHT = 900
MIN_WIDTH = 320
MIN_HEIGHT = 200
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_LEGEND = 10
LABEL = "data chart, not a Grafana panel screenshot"
TITLE_LABEL = LABEL[0].upper() + LABEL[1:]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG from its IHDR chunk, or None if it isn't one."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":  # noqa: PLR2004
        return None
    return struct.unpack(">II", data[16:24])


def check_size(width: int, height: int) -> tuple[int, int]:
    if not (MIN_WIDTH <= width <= MAX_WIDTH and MIN_HEIGHT <= height <= MAX_HEIGHT):
        raise ObservabilityError(
            INVALID_QUERY,
            f"chart size must be {MIN_WIDTH}x{MIN_HEIGHT} to {MAX_WIDTH}x{MAX_HEIGHT}",
        )
    return width, height


def verify_render(response, width: int, height: int) -> bytes:
    """Accept a server render only if it is a real PNG of exactly the requested size.

    Grafana answers 200 with a small "No image renderer available" PNG when no
    renderer exists, so status and content-type alone prove nothing.
    """
    data = response.body
    if len(data) > MAX_PNG_BYTES:
        raise ObservabilityError(BOUNDS_EXCEEDED, "rendered PNG is over 4 MiB")
    size = png_size(data)
    if size is None or "png" not in response.content_type.lower():
        raise ObservabilityError(UPSTREAM_UNAVAILABLE, "renderer did not return a PNG")
    if size != (width, height):
        raise ObservabilityError(
            UPSTREAM_UNAVAILABLE,
            f"renderer returned a {size[0]}x{size[1]} image, not the requested {width}x{height}: "
            "this is Grafana's 'no image renderer' placeholder or a failed render, not a panel",
        )
    if not (0 < size[0] <= MAX_WIDTH and 0 < size[1] <= MAX_HEIGHT):
        raise ObservabilityError(
            BOUNDS_EXCEEDED, "rendered PNG dimensions exceed the limit"
        )

    # Pillow's decoder loads pixel data, while explicit chunk checks ensure that
    # even a missing/truncated IEND or a bad CRC cannot pass as a complete PNG.
    try:
        from PIL import Image
    except ImportError:
        raise ObservabilityError(
            NOT_CONFIGURED,
            "Pillow is not installed; install django-ergo[observability]",
        ) from None
    try:
        view = memoryview(data)
        pos = len(PNG_MAGIC)
        has_data = False
        while True:
            if pos + 12 > len(data):
                raise ValueError("truncated PNG chunk")  # noqa: TRY301
            length = struct.unpack_from(">I", data, pos)[0]
            end = pos + 12 + length
            if end > len(data):
                raise ValueError("truncated PNG chunk")  # noqa: TRY301
            kind = data[pos + 4 : pos + 8]
            has_data = has_data or kind == b"IDAT"
            crc = struct.unpack_from(">I", data, end - 4)[0]
            if zlib.crc32(view[pos + 4 : end - 4]) != crc:
                raise ValueError("invalid PNG checksum")  # noqa: TRY301
            pos = end
            if kind == b"IEND":
                if length != 0 or pos != len(data) or not has_data:
                    raise ValueError("invalid PNG end")  # noqa: TRY301
                break
        with Image.open(io.BytesIO(data), formats=["PNG"]) as image:
            if image.size != size or getattr(image, "n_frames", 1) != 1:
                raise ValueError("invalid PNG dimensions or animation")  # noqa: TRY301
            image.verify()
        with Image.open(io.BytesIO(data), formats=["PNG"]) as image:
            image.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        IndexError,
        EOFError,
        OverflowError,
        struct.error,
        Image.DecompressionBombError,
    ):
        raise ObservabilityError(
            UPSTREAM_UNAVAILABLE, "renderer returned an invalid PNG"
        ) from None
    return data


@dataclass
class ChartMeta:
    query: str
    source: str
    window: Window
    link: str = ""


def _series_name(labels: dict) -> str:
    if not labels:
        return "{}"
    name = labels.get("__name__", "")
    rest = ", ".join(f"{k}={v}" for k, v in labels.items() if k != "__name__")
    return f"{name}{{{rest}}}" if rest else name


def _wrap(text: str, width: int = 150, max_lines: int = 3) -> str:
    lines = [text[i : i + width] for i in range(0, len(text), width)]
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1][: width - 1] + "…"]
    return "\n".join(lines)


def render_chart(
    result: QueryResult, meta: ChartMeta, *, width: int = 1200, height: int = 675
) -> bytes:
    """One PNG line chart. Absent data, instant results and histograms are not charted."""
    check_size(width, height)
    if result.kind != "range" or result.result_type != "matrix":
        raise ObservabilityError(
            INVALID_QUERY, "only range (matrix) results can be charted"
        )
    if result.histograms_only:
        raise ObservabilityError(
            INVALID_QUERY,
            "native histograms are unsupported for plotting; use histogram_quantile() or a classic bucket query",
        )
    if result.absent:
        raise ObservabilityError(
            INVALID_QUERY, "no data: the query matched no series (absent, not zero)"
        )
    try:
        import matplotlib as mpl

        mpl.use("Agg")
        from matplotlib import dates as mdates
        from matplotlib import pyplot as plt
    except ImportError:
        raise ObservabilityError(
            NOT_CONFIGURED,
            "matplotlib is not installed; install django-ergo[observability]",
        ) from None

    dpi = 100
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    try:
        ax = fig.add_axes([0.07, 0.24, 0.90, 0.64])
        tz = meta.window.tz
        step = meta.window.step
        for series in result.series:
            xs, ys = [], []
            previous = None
            for ts, value in series.points:
                if previous is not None and ts - previous > step * 1.5:
                    xs.append(datetime.fromtimestamp(previous + step, UTC))
                    ys.append(float("nan"))  # a gap stays a gap
                previous = ts
                xs.append(datetime.fromtimestamp(ts, UTC))
                ys.append(
                    value
                    if value not in {float("inf"), float("-inf")}
                    else float("nan")
                )
            ax.plot(
                xs,
                ys,
                linewidth=1.2,
                marker="o" if len(xs) == 1 else None,
                markersize=3,
                label=_series_name(series.labels),
            )
        ax.set_xlim(meta.window.start, meta.window.end)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=tz))
        ax.grid(alpha=0.3)
        ax.set_xlabel(f"time ({tz.key})")
        notes = []
        if result.histogram_series:
            notes.append("native histogram samples omitted")
        if len(result.series) <= MAX_LEGEND:
            ax.legend(fontsize=7, loc="upper left", framealpha=0.7)
        else:
            notes.append(f"{len(result.series)} series; legend omitted")
        nonfinite = sum(s.nan + s.pos_inf + s.neg_inf for s in result.series)
        if nonfinite:
            notes.append(f"{nonfinite} NaN/Inf samples not plotted")
        gaps = sum(s.gaps for s in result.series)
        if gaps:
            notes.append(f"{gaps} gap(s) left unfilled")
        ax.set_title(_wrap(meta.query, max_lines=2), fontsize=9, loc="left")
        footer = [
            TITLE_LABEL + (f" ({'; '.join(notes)})" if notes else ""),
            f"query: {meta.query}",
            f"range UTC {meta.window.start.isoformat()} .. {meta.window.end.isoformat()}  "
            f"tz {tz.key}  step {step}s",
            f"source: {meta.source}",
        ]
        fig.text(
            0.01,
            0.01,
            "\n".join(
                _wrap(line, width=int(width / 6.2), max_lines=2) for line in footer
            ),
            fontsize=7,
            va="bottom",
            ha="left",
            family="monospace",
        )
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi)
    finally:
        plt.close(fig)
    data = buf.getvalue()
    if len(data) > MAX_PNG_BYTES:
        raise ObservabilityError(BOUNDS_EXCEEDED, "chart PNG is over 4 MiB")
    return data
