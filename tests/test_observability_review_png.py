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


def test_real_png_decodes_and_is_reencoded():
    data = png()
    result = charts.verify_render(response(data), 1000, 500)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (1000, 500)
        assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def replace_idat(data, payload):
    return data[:8] + b"".join(
        chunk(b"IDAT", payload) if kind == b"IDAT" else part
        for kind, part in chunks(data)
    )


def test_zlib_stream_must_end_exactly_at_raster():
    data = png()
    compressed = b"".join(part[8:-4] for kind, part in chunks(data) if kind == b"IDAT")
    raster = zlib.decompress(compressed)
    for payload in (
        compressed[:-1],
        compressed + b"hidden",
        compressed + zlib.compress(b"hidden"),
        zlib.compress(raster + b"hidden"),
        zlib.compress(raster[:-1]),
        zlib.compress(raster[:3001]),  # One complete scanline, not the full image.
        zlib.compress(bytes([5]) + raster[1:]),
        zlib.compress(raster + b"x" * (10 * 1024 * 1024)),
    ):
        assert_invalid(replace_idat(data, payload))


def test_metadata_is_not_returned_or_decompressed():
    data = png()
    metadata = (
        chunk(b"tEXt", b"secret\x00sensitive text")
        + chunk(b"zTXt", b"secret\x00\x00" + zlib.compress(b"x" * (10 * 1024 * 1024)))
        + chunk(b"iTXt", b"key\x00\x00\x00\x00\x00private")
    )
    data = data[:33] + metadata + data[33:]
    result = charts.verify_render(response(data), 1000, 500)
    assert [kind for kind, _ in chunks(result)] == [b"IHDR", b"IDAT", b"IEND"]
    assert b"secret" not in result
    with Image.open(io.BytesIO(result)) as image:
        assert image.info == {}
        assert image.getpixel((0, 0)) == (255, 0, 0, 255)


def test_palette_transparency_survives_reencoding_without_metadata():
    image = Image.new("P", (1000, 500), 0)
    image.putpalette([12, 34, 56] + [0, 0, 0] * 255)
    output = io.BytesIO()
    image.save(output, format="PNG", transparency=bytes([42]))
    result = charts.verify_render(response(output.getvalue()), 1000, 500)
    with Image.open(io.BytesIO(result)) as decoded:
        assert decoded.info == {}
        assert decoded.getpixel((0, 0)) == (12, 34, 56, 42)


def test_noncontiguous_idat_and_animation_are_rejected():
    data = png()
    parts = list(chunks(data))
    compressed = next(part[8:-4] for kind, part in parts if kind == b"IDAT")
    split = len(compressed) // 2
    data = data[:8] + b"".join(
        (
            chunk(b"IDAT", compressed[:split])
            + chunk(b"tEXt", b"key\x00value")
            + chunk(b"IDAT", compressed[split:])
        )
        if kind == b"IDAT"
        else part
        for kind, part in parts
    )
    assert_invalid(data)
    data = png()
    assert_invalid(data[:33] + chunk(b"acTL", struct.pack(">II", 1, 0)) + data[33:])


def test_adam7_raster_is_validated_pass_by_pass():
    width, height = 1000, 500
    passes = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    raster = bytearray()
    for x, y, dx, dy in passes:
        w = max(0, (width - x + dx - 1) // dx)
        h = max(0, (height - y + dy - 1) // dy)
        if w and h:
            raster.extend((b"\x00" + b"\xff\x00\x00" * w) * h)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 1)
    data = (
        charts.PNG_MAGIC
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raster))
        + chunk(b"IEND", b"")
    )
    result = charts.verify_render(response(data), width, height)
    with Image.open(io.BytesIO(result)) as image:
        assert image.getpixel((999, 499)) == (255, 0, 0, 255)
    assert_invalid(replace_idat(data, zlib.compress(raster[:-1])))
    assert_invalid(replace_idat(data, zlib.compress(raster + b"x")))


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
