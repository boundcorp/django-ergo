"""Read the exact six-shot archive as data; never execute or broadly extract it."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

ARCHIVE_SHA256 = "ca4adf0852b44eb7b88300e73ffd4a37afb0c614dbc29d2760afa191d9f00efe"
AUDIO_SHA256 = "a6656bd050cc85869b81924d146cf35a97e7fa3affb537bc802ab5e0277b0536"
SELECTED = ("MS-01", "MS-02-V2", "MS-03", "MS-04", "MS-05-V2", "MS-06-V2")
DURATIONS = (5, 5, 5, 5, 5, 4)
MAX_ENTRIES = 1000
MAX_ARCHIVE_BYTES = 100_000_000
MAX_MEMBER_BYTES = 50_000_000
INVENTORY_COUNT = 48


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _check_archive(archive):
    infos = archive.infolist()
    names = [info.filename for info in infos]
    _require(
        len(names) == len(set(names)) and len(names) <= MAX_ENTRIES,
        "duplicate or excessive archive entries",
    )
    _require(
        sum(info.file_size for info in infos) <= MAX_ARCHIVE_BYTES,
        "archive uncompressed size limit",
    )
    for info in infos:
        path = PurePosixPath(info.filename)
        kind = stat.S_IFMT(info.external_attr >> 16)
        _require(
            not path.is_absolute()
            and ".." not in path.parts
            and "\\" not in info.filename
            and kind in (0, stat.S_IFREG, stat.S_IFDIR)
            and not info.flag_bits & 1
            and info.file_size <= MAX_MEMBER_BYTES,
            "unsafe archive entry",
        )
    _require(archive.testzip() is None, "archive CRC mismatch")
    inventory = [
        json.loads(line)
        for line in archive.read("out/test-input-inventory.jsonl").splitlines()
    ]
    _require(
        len(inventory) == INVENTORY_COUNT
        and len({r["path"] for r in inventory}) == INVENTORY_COUNT,
        "expected 48 unique inventory entries",
    )
    for row in inventory:
        data = archive.read(row["path"])
        _require(
            len(data) == row["bytes"] and digest(data) == row["sha256"],
            "inventory content mismatch",
        )


def digest(content):
    return hashlib.sha256(content).hexdigest()


def read_frozen(parts):
    """Validate provenance and return only approved image/prompt/audio bytes.

    Archive statuses are preserved, not promoted into production approvals.
    CH1/CH2 planning cues are deliberately not executable input types.
    """
    content = b"".join(Path(path).read_bytes() for path in parts)
    _require(digest(content) == ARCHIVE_SHA256, "frozen archive SHA-256 mismatch")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        _check_archive(archive)
        rows = [
            json.loads(line) for line in archive.read("out/queue.jsonl").splitlines()
        ]
        _require(
            len(rows) == len(DURATIONS), "only frozen six-shot pilot is executable"
        )
        shots = []
        audio = None
        for index, row in enumerate(rows):
            shot = f"MS-{index + 1:02d}"
            prompt = archive.read(row["prompt_file"])
            image = archive.read(row["keyframe"]["path"])
            sidecar = json.loads(archive.read(row["sanitized_source_sidecar"]))
            wav = archive.read(row["source_audio"]["path"])
            _require(
                row["shot"] == shot
                and row["historical_request_shot"] == SELECTED[index]
                and row["duration_s"] == DURATIONS[index]
                and row["excerpt_start_s"] == sum(DURATIONS[:index])
                and row["excerpt_end_s"] == sum(DURATIONS[: index + 1])
                and digest(prompt) == row["prompt_sha256"]
                and digest(image) == row["keyframe"]["sha256"]
                and digest(wav) == AUDIO_SHA256
                and row["source_audio"]["sha256"] == AUDIO_SHA256
                and sidecar["video_request"]["prompt"] == prompt.decode().strip()
                and sidecar["video_settings"]["aspect_ratio"] == "9:16"
                and sidecar["request_evidence"]["request_shot"] == SELECTED[index],
                f"frozen shot contract mismatch: {shot}",
            )
            shots.append(
                {
                    "shot": shot,
                    "version": "v2" if "V2" in SELECTED[index] else "v1",
                    "historical_request": SELECTED[index],
                    "seconds": DURATIONS[index],
                    "prompt": prompt.decode().strip(),
                    "prompt_sha256": digest(prompt),
                    "image": image,
                    "image_sha256": digest(image),
                    "known_flags": row["known_flags"],
                }
            )
            audio = wav
        return shots, audio
