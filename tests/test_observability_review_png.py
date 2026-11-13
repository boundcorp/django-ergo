"""Regression coverage for validating renderer PNGs before delivery."""

import io
import struct
import zlib

import pytest
from PIL import Image

from django_ergo.observability import charts
from django_ergo.observability.errors import ObservabilityError
from django_ergo.observability.http import Response


def png(width=1000, height=500):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(output, format="PNG")
    return output.getvalue()


def chunks(data):
    pos = 8
    while pos < len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        end = pos + 12 + length
        yield data[pos + 4 : pos + 8], data[pos:end]
        pos = end


def chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def response(data):
    return Response(200, "image/png", data)


def assert_invalid(data):
    with pytest.raises(ObservabilityError) as exc:
        charts.verify_render(response(data), 1000, 500)
    assert exc.value.code == "upstream_unavailable"


def test_real_png_decodes_and_is_returned():
    data = png()
    assert charts.verify_render(response(data), 1000, 500) == data


def test_truncated_pngs_and_missing_chunks_are_rejected():
    data = png()
    parts = list(chunks(data))
    assert_invalid(data[:24])
    assert_invalid(data[:-1])
    assert_invalid(data[:-12])  # Missing IEND, despite complete image data.
    assert_invalid(data[:8] + b"".join(part for kind, part in parts if kind != b"IDAT"))


def test_corrupt_same_size_png_is_rejected_even_with_valid_chunk_crc():
    data = png()
    parts = list(chunks(data))
    broken = data[:8] + b"".join(
        chunk(b"IDAT", b"not a zlib stream") if kind == b"IDAT" else part
        for kind, part in parts
    )
    assert_invalid(broken)
    corrupted_crc = bytearray(data)
    corrupted_crc[-1] ^= 1
    assert_invalid(bytes(corrupted_crc))


def test_placeholder_and_byte_bounds_remain_enforced():
    with pytest.raises(ObservabilityError) as exc:
        charts.verify_render(response(png(478, 208)), 1000, 500)
    assert exc.value.code == "upstream_unavailable"
    assert "478x208" in exc.value.message
    with pytest.raises(ObservabilityError) as exc:
        charts.verify_render(response(png() + b"x" * charts.MAX_PNG_BYTES), 1000, 500)
    assert exc.value.code == "bounds_exceeded"


def test_mixed_histogram_chart_labels_omitted_samples(monkeypatch):
    from datetime import UTC
    from datetime import datetime

    from matplotlib.figure import Figure

    from django_ergo.observability.prometheus import normalize
    from django_ergo.observability.timeutil import resolve_window

    result = normalize(
        {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {},
                    "values": [[60, "42"]],
                    "histograms": [[120, {"count": "0", "sum": "0"}]],
                }
            ],
        },
        "range",
        60,
    )
    window = resolve_window(
        tz="UTC", now=datetime.fromtimestamp(180, UTC), window="1h", step="60s"
    )
    texts = []
    original = Figure.text

    def spy(self, x, y, text, *args, **kwargs):
        texts.append(text)
        return original(self, x, y, text, *args, **kwargs)

    monkeypatch.setattr(Figure, "text", spy)
    charts.render_chart(result, charts.ChartMeta("up", "fixture", window))
    assert "native histogram samples omitted" in "\n".join(texts)
