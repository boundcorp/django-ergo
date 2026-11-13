import io
import json
import zipfile

import pytest

from django_ergo.plugins.runpod import frozen
from django_ergo.plugins.runpod.runner import main


def archive_fixture(tmp_path, monkeypatch, *, selected=None, seconds=None):
    entries = {}
    rows = []
    audio = b"approved audio unchanged"
    monkeypatch.setattr(frozen, "AUDIO_SHA256", frozen.digest(audio))
    for index in range(6):
        name = f"MS-{index + 1:02d}"
        prompt = ("selected exact " + name).encode()
        image = ("image " + name).encode()
        base = f"out/{name}"
        entries[base + ".txt"] = prompt
        entries[base + ".png"] = image
        entries[base + ".wav"] = audio
        selected_name = frozen.SELECTED[index]
        entries[base + ".json"] = json.dumps(
            {
                "video_request": {"prompt": prompt.decode()},
                "video_settings": {"aspect_ratio": "9:16"},
                "request_evidence": {"request_shot": selected_name},
            }
        ).encode()
        rows.append(
            {
                "shot": name,
                "historical_request_shot": selected_name,
                "duration_s": frozen.DURATIONS[index],
                "excerpt_start_s": sum(frozen.DURATIONS[:index]),
                "excerpt_end_s": sum(frozen.DURATIONS[: index + 1]),
                "prompt_file": base + ".txt",
                "prompt_sha256": frozen.digest(prompt),
                "keyframe": {"path": base + ".png", "sha256": frozen.digest(image)},
                "source_audio": {"path": base + ".wav", "sha256": frozen.digest(audio)},
                "sanitized_source_sidecar": base + ".json",
                "known_flags": ["not final approved"],
            }
        )
    if selected is not None:
        rows[1]["historical_request_shot"] = selected
    if seconds is not None:
        rows[0]["duration_s"] = seconds
    for index in range(24):
        entries[f"out/context-{index}"] = b"provenance"
    inventory = [
        {"path": path, "bytes": len(data), "sha256": frozen.digest(data)}
        for path, data in entries.items()
    ]
    entries["out/test-input-inventory.jsonl"] = b"\n".join(
        json.dumps(row).encode() for row in inventory
    )
    entries["out/queue.jsonl"] = b"\n".join(json.dumps(row).encode() for row in rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    content = buffer.getvalue()
    monkeypatch.setattr(frozen, "ARCHIVE_SHA256", frozen.digest(content))
    first = tmp_path / "part001"
    second = tmp_path / "part002"
    first.write_bytes(content[:100])
    second.write_bytes(content[100:])
    return [first, second], audio


def test_portable_frozen_contract_preserves_versions_audio_and_known_flags(
    tmp_path, monkeypatch
):
    parts, approved = archive_fixture(tmp_path, monkeypatch)
    shots, audio = frozen.read_frozen(parts)
    assert [s["version"] for s in shots] == ["v1", "v2", "v1", "v1", "v2", "v2"]
    assert [s["seconds"] for s in shots] == [5, 5, 5, 5, 5, 4]
    assert audio == approved
    assert shots[0]["prompt"] == "selected exact MS-01"
    assert shots[0]["known_flags"] == ["not final approved"]


@pytest.mark.parametrize("change", [{"selected": "MS-02"}, {"seconds": 4}])
def test_historical_version_or_shortened_duration_cannot_silently_change(
    tmp_path, monkeypatch, change
):
    parts, _ = archive_fixture(tmp_path, monkeypatch, **change)
    with pytest.raises(ValueError, match="shot contract mismatch"):
        frozen.read_frozen(parts)


def test_archive_corruption_rejected_before_reading_any_inputs(tmp_path, monkeypatch):
    parts, _ = archive_fixture(tmp_path, monkeypatch)
    parts[0].write_bytes(b"different")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        frozen.read_frozen(parts)


@pytest.mark.parametrize("stage", ["ch01", "ch02"])
def test_chapters_remain_non_executable_without_reading_model_or_input(stage):
    with pytest.raises(SystemExit) as error:
        main(["work", "not-read", "--stage", stage])
    assert error.value.code == 2
