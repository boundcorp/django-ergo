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


def _raster_rows(width: int, height: int, depth: int, color: int, interlace: int):
    """Yield encoded row lengths, including filter bytes, for every PNG pass."""
    depths = {
        0: (1, 2, 4, 8, 16),
        2: (8, 16),
        3: (1, 2, 4, 8),
        4: (8, 16),
        6: (8, 16),
    }
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    if color not in depths or depth not in depths[color] or interlace not in (0, 1):
        raise ValueError("invalid PNG raster format")
    passes = (
        ((0, 0, 1, 1),)
        if interlace == 0
        else (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    for x, y, dx, dy in passes:
        pass_width = max(0, (width - x + dx - 1) // dx)
        pass_height = max(0, (height - y + dy - 1) // dy)
        if pass_width and pass_height:
            row_length = 1 + (pass_width * channels[color] * depth + 7) // 8
            yield row_length, pass_height


def _validate_raster(compressed: bytes, rows) -> None:
    """Require exactly one complete zlib stream containing exactly the raster."""
    rows = list(rows)
    expected = sum(length * count for length, count in rows)
    decoder = zlib.decompressobj()
    raster = decoder.decompress(compressed, expected + 1)
    if (
        len(raster) != expected
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("invalid PNG compressed raster")
    pos = 0
    for length, count in rows:
        for _ in range(count):
            if raster[pos] > 4:  # noqa: PLR2004
                raise ValueError("invalid PNG filter")
            pos += length


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

    # Validate framing and raster independently of Pillow: its decoder can
    # accept truncated zlib streams and ignores data after the raster. Feed it
    # only pixel-relevant chunks so untrusted metadata is never decompressed.
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
        data_ended = False
        seen = set()
        compressed = bytearray()
        clean = bytearray(PNG_MAGIC)
        rows = None
        while True:
            if pos + 12 > len(data):
                raise ValueError("truncated PNG chunk")  # noqa: TRY301
            length = struct.unpack_from(">I", data, pos)[0]
            end = pos + 12 + length
            if end > len(data):
                raise ValueError("truncated PNG chunk")  # noqa: TRY301
            kind = data[pos + 4 : pos + 8]
            if (
                len(kind) != 4  # noqa: PLR2004
                or any(not (65 <= b <= 90 or 97 <= b <= 122) for b in kind)  # noqa: PLR2004
                or kind[2] & 32
            ):
                raise ValueError("invalid PNG chunk type")  # noqa: TRY301
            if pos == len(PNG_MAGIC) and kind != b"IHDR":
                raise ValueError("missing PNG header")  # noqa: TRY301
            if kind in (b"IHDR", b"PLTE", b"tRNS", b"IEND"):
                if kind in seen:
                    raise ValueError("duplicate PNG chunk")  # noqa: TRY301
                seen.add(kind)
            if kind == b"IHDR":
                if length != 13 or pos != len(PNG_MAGIC):  # noqa: PLR2004
                    raise ValueError("invalid PNG header")  # noqa: TRY301
                _, _, depth, color, compression, filtering, interlace = (
                    struct.unpack_from(">IIBBBBB", data, pos + 8)
                )
                if compression != 0 or filtering != 0:
                    raise ValueError("invalid PNG methods")  # noqa: TRY301
                rows = _raster_rows(*size, depth, color, interlace)
            elif kind == b"IDAT":
                if data_ended:
                    raise ValueError("noncontiguous PNG data")  # noqa: TRY301
                has_data = True
                compressed.extend(view[pos + 8 : end - 4])
            else:
                if has_data:
                    data_ended = True
                if kind in (b"PLTE", b"tRNS") and has_data:
                    raise ValueError("invalid PNG chunk order")  # noqa: TRY301
                if kind in (b"acTL", b"fcTL", b"fdAT"):
                    raise ValueError("animated PNG")  # noqa: TRY301
                if kind not in (b"PLTE", b"tRNS", b"IEND") and not kind[0] & 32:
                    raise ValueError("unknown critical PNG chunk")  # noqa: TRY301
            crc = struct.unpack_from(">I", data, end - 4)[0]
            if zlib.crc32(view[pos + 4 : end - 4]) != crc:
                raise ValueError("invalid PNG checksum")  # noqa: TRY301
            if kind in (b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"):
                clean.extend(view[pos:end])
            pos = end
            if kind == b"IEND":
                if length != 0 or pos != len(data) or not has_data:
                    raise ValueError("invalid PNG end")  # noqa: TRY301
                break
        _validate_raster(bytes(compressed), rows)
        with Image.open(io.BytesIO(clean), formats=["PNG"]) as image:
            if image.size != size or getattr(image, "n_frames", 1) != 1:
                raise ValueError("invalid PNG dimensions or animation")  # noqa: TRY301
            image.verify()
        with Image.open(io.BytesIO(clean), formats=["PNG"]) as image:
            image.load()
            pixels = image.convert("RGBA")
            # A fresh image carries no metadata, even for palette/transparency input.
            fresh = Image.frombytes("RGBA", size, pixels.tobytes())
            output = io.BytesIO()
            fresh.save(output, format="PNG")
    except (
        OSError,
        ValueError,
        SyntaxError,
        IndexError,
        EOFError,
        OverflowError,
        struct.error,
        zlib.error,
        Image.DecompressionBombError,
    ):
        raise ObservabilityError(
            UPSTREAM_UNAVAILABLE, "renderer returned an invalid PNG"
        ) from None
    result = output.getvalue()
    if len(result) > MAX_PNG_BYTES:
        raise ObservabilityError(BOUNDS_EXCEEDED, "rendered PNG is over 4 MiB")
    return result


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
