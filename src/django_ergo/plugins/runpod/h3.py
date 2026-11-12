"""Pinned H3 FL2VA client with durable send tombstones and no replay.

The controller runs OFF the rented Pod. Only image-conditioned generation is
supported. A lost POST response remains unknown: SGLang has no documented
idempotent create/lookup-by-client-ID contract. Never retry such a submission.
"""

# Fixed local executables, bounded response sizes, trusted loopback service.
# ruff: noqa: S603
from __future__ import annotations

import base64
import json
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.request import urlopen

from .frozen import digest

MODEL = "MiniMaxAI/MiniMax-H3"
MODEL_REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"
SGLANG_REVISION = "2511743bd784e69e5a81ca3d926a000711dae4ab"


def reject(message):
    raise ValueError(message)


def expired(message):
    raise TimeoutError(message)


def request_body(shot):
    """Request a ceiling lattice duration; trim real extra frames, never pad.

    5s ->124 frames (17*7+5), 4s ->107 frames (17*6+5).
    """
    seconds = shot["seconds"]
    if seconds not in (4, 5) or not shot.get("image"):
        reject("only frozen 4/5-second image-conditioned shots are supported")
    return {
        "model": MODEL,
        "prompt": shot["prompt"],
        "task": "fl2va",
        "seconds": seconds,
        "target": {
            "short_edge": 768,
            "aspect_ratio": "9:16",
            "duration_seconds": seconds,
        },
        "conditions": [
            {
                "type": "image",
                "role": "keyframe",
                "uri": "data:image/png;base64,"
                + base64.b64encode(shot["image"]).decode(),
                "frame_index": 0,
            }
        ],
        "num_outputs_per_prompt": 1,
        "num_inference_steps": 20,
        "flow_shift": 12.0,
        "audio_flow_shift": 3.0,
        "seed": 1101,
    }


class H3Client:
    def __init__(self, base_url="http://127.0.0.1:30010", *, opener=urlopen):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            reject("H3 endpoint must be loopback HTTP; use an authorized SSH tunnel")
        self.base_url = base_url.rstrip("/")
        self.opener = opener

    def call(self, method, route, *, deadline, body=None, binary=False):
        remaining = deadline - time.time()
        if remaining <= 0:
            expired("work cutoff reached")
        data = None if body is None else json.dumps(body).encode()
        request = Request(  # noqa: S310 — loopback HTTP validated in __init__.
            self.base_url + route,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        limit = 512_000_000 if binary else 1_000_000
        with self.opener(request, timeout=min(10, remaining)) as response:
            chunks = []
            size = 0
            while True:
                if time.time() >= deadline:
                    expired("work cutoff reached during response")
                chunk = response.read(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    reject("H3 response exceeds size limit")
        content = b"".join(chunks)
        return content if binary else json.loads(content)


class Submissions:
    """Permanent local tombstones; unknown/failed identities never become absent."""

    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS h3_submissions (identity TEXT PRIMARY KEY, request_hash TEXT NOT NULL, receipt TEXT, state TEXT NOT NULL, started REAL NOT NULL)"
            )

    def inspect(self, identity):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM h3_submissions WHERE identity=?", (identity,)
            ).fetchone()
            return dict(row) if row else None

    def submit_once(self, identity, body, *, client, deadline):
        request_hash = digest(json.dumps(body, sort_keys=True).encode())
        with sqlite3.connect(self.path) as db:
            existing = db.execute(
                "SELECT request_hash FROM h3_submissions WHERE identity=?", (identity,)
            ).fetchone()
            if existing:
                if existing[0] != request_hash:
                    reject("submission identity already binds a different request")
                return self.inspect(identity)
            db.execute(
                "INSERT INTO h3_submissions VALUES (?, ?, NULL, 'unknown', ?)",
                (identity, request_hash, time.time()),
            )
        # Intent committed before network send. Crash/timeout can never replay it.
        result = client.call("POST", "/v1/videos", body=body, deadline=deadline)
        receipt = result.get("id")
        if (
            not isinstance(receipt, str)
            or not receipt
            or any(
                c
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in receipt
            )
        ):
            reject("invalid H3 receipt; inspect unknown tombstone, do not replay")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE h3_submissions SET receipt=?, state='accepted' WHERE identity=?",
                (receipt, identity),
            )
        return self.inspect(identity)


def run_media(argv, *, deadline):
    """Kill the entire subprocess group at cutoff, including ffmpeg children."""
    remaining = deadline - time.time()
    if remaining <= 0:
        expired("work cutoff reached")
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        stdout, stderr = process.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        expired("work cutoff killed media subprocess")
    if process.returncode:
        reject("media command failed: " + stderr.decode(errors="replace")[-2000:])
    return stdout


def probe(path, *, deadline, run=run_media):
    result = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            deadline=deadline,
        )
    )
    video = [s for s in result["streams"] if s["codec_type"] == "video"]
    if len(video) != 1:
        reject("expected exactly one video stream")
    return video[0], result["streams"]


def conform(native, output, *, seconds, deadline, run=run_media):
    video, _ = probe(native, deadline=deadline, run=run)
    wanted = seconds * 24
    if (video["width"], video["height"], video["r_frame_rate"]) != (
        768,
        1344,
        "24/1",
    ) or int(video.get("nb_read_frames", 0)) < wanted:
        reject(
            "native clip lacks required portrait24fps real frames; no padding permitted"
        )
    run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-i",
            str(native),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"trim=end_frame={wanted},setpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            str(output),
        ],
        deadline=deadline,
    )
    video, streams = probe(output, deadline=deadline, run=run)
    if int(video.get("nb_read_frames", 0)) != wanted or any(
        s["codec_type"] == "audio" for s in streams
    ):
        reject("conformed clip frame/audio contract failed")


def file_record(path):
    path = Path(path)
    data = path.read_bytes()
    return {"name": path.name, "size": len(data), "sha256": digest(data)}
