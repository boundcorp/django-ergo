"""Executable OFF-Pod frozen six-shot worker; no rental or model download.

Run validate without a ledger/GPU. Run work only with an already-bound ledger,
fresh independent cleaner, local SGLang tunnel and durable local export path.
"""

# Shares the existing ledger transaction for fixed nonrenewable work cutoff.
# ruff: noqa: SLF001, T201
from __future__ import annotations

import argparse
import io
import json
import os
import time
import wave
from pathlib import Path

from .experiment import Experiment
from .frozen import AUDIO_SHA256
from .frozen import digest
from .frozen import read_frozen
from .h3 import MODEL_REVISION
from .h3 import SGLANG_REVISION
from .h3 import H3Client
from .h3 import Submissions
from .h3 import conform
from .h3 import expired
from .h3 import file_record
from .h3 import probe
from .h3 import reject
from .h3 import request_body
from .h3 import run_media
from .queue import Queue
from .supervisor import CleanerSupervision

PILOT_SHOTS = 6
MASTER_FRAMES = 696
PCM_BYTES_PER_SAMPLE = 2


def immutable_write(path, data):
    """Never replace original bytes, including a conflicting retry's artifacts."""
    if path.exists():
        if path.read_bytes() != data:
            reject("export identity conflict: " + str(path))
        return
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def snapshot(queue, output, timings, *, assembly=None):
    jobs = queue.jobs()
    items = []
    for index, job in enumerate(jobs):
        partial = [
            file_record(p) for p in sorted(output.glob(job["submission_id"] + ".*.mp4"))
        ]
        result = json.loads(job["result"]) if job["result"] else {}
        items.append(
            {
                "index": index,
                "shot": job["shot"],
                "version": job["version"],
                "original_id": job["original_id"],
                "status": job["status"],
                "submission_id": job["submission_id"],
                "result": result,
                "exported_artifacts": partial,
            }
        )
    manifest = {
        "model_revision": MODEL_REVISION,
        "sglang_revision": SGLANG_REVISION,
        "task": "fl2va",
        "historical_baseline": "Sume/minimax-h3-max (not equivalent to open H3-Base)",
        "items": items,
        "timings": timings,
        "assembly": assembly,
        "complete": bool(jobs)
        and all(j["status"] == "succeeded" for j in jobs)
        and assembly is not None,
    }
    data = json.dumps(manifest, indent=2, sort_keys=True).encode()
    temporary = output / "manifest.next"
    temporary.write_bytes(data)
    temporary.replace(output / "manifest.json")
    return manifest


def process_shot(queue, job, shot, *, client, submissions, output, cutoff):  # noqa: C901, PLR0913 — one serialized submission and export.
    body = request_body(shot)
    started = time.time()
    identity = job["submission_id"]
    record = submissions.inspect(identity)
    if job["submission_state"] == "not_sent":
        queue.prepare_submission(job["id"], job["epoch"], time.time())
        record = submissions.submit_once(identity, body, client=client, deadline=cutoff)
    if not record or record["state"] != "accepted":
        reject("unknown H3 submission; inspect tombstone, never replay")
    started = record["started"]
    queue.record_receipt(job["id"], record["receipt"])
    if job["submission_state"] != "accepted":
        queue.observe_submission(job["id"], job["epoch"], time.time(), state="accepted")
    receipt = record["receipt"]
    while True:
        state = client.call("GET", "/v1/videos/" + receipt, deadline=cutoff)
        if state["status"] == "completed":
            break
        if state["status"] == "failed":
            reject("H3 generation failed; no paid retry")
        if time.time() + 1 >= cutoff:
            expired("generation reached early work cutoff")
        time.sleep(1)
    generated = time.time()
    native = output / (identity + ".native.mp4")
    if not native.exists():
        content = client.call(
            "GET", "/v1/videos/" + receipt + "/content", deadline=cutoff, binary=True
        )
        immutable_write(native, content)
    downloaded = time.time()
    # A partial native export survives validation/conform failure in the manifest.
    native_record = file_record(native)
    conformed = output / (identity + ".conformed.mp4")
    if not conformed.exists():
        conform(native, conformed, seconds=shot["seconds"], deadline=cutoff)
    else:
        video, streams = probe(conformed, deadline=cutoff)
        if int(video.get("nb_read_frames", 0)) != shot["seconds"] * 24 or any(
            s["codec_type"] == "audio" for s in streams
        ):
            reject("existing conformed export failed validation")
    result = {
        "native": native_record,
        "conformed": file_record(conformed),
        "seconds": shot["seconds"],
        "prompt_sha256": shot["prompt_sha256"],
        "image_sha256": shot["image_sha256"],
        "historical_request": shot["historical_request"],
        "known_flags": shot["known_flags"],
        "timings": {
            "generation_seconds": generated - started,
            "export_seconds": downloaded - generated,
            "conform_seconds": time.time() - downloaded,
        },
    }
    queue.finish(
        job["id"], job["epoch"], time.time(), status="succeeded", result=result
    )


def assemble(queue, output, audio, *, cutoff):
    jobs = queue.jobs()
    if len(jobs) != PILOT_SHOTS or any(j["status"] != "succeeded" for j in jobs):
        reject("29-second assembly requires all six successful original shots")
    listing = output / "concat.txt"
    # Generated submission IDs are lowercase hex: no caller-controlled ffconcat syntax.
    immutable_write(
        listing,
        "".join(f"file '{j['submission_id']}.conformed.mp4'\n" for j in jobs).encode(),
    )
    wav = output / "approved.wav"
    immutable_write(wav, audio)
    if digest(wav.read_bytes()) != AUDIO_SHA256:
        reject("approved WAV changed")
    master = output / "comparison29s.mov"
    if not master.exists():
        run_media(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(listing),
                "-i",
                str(wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "pcm_s16le",
                "-t",
                "29",
                str(master),
            ],
            deadline=cutoff,
        )
    video, streams = probe(master, deadline=cutoff)
    if (
        int(video.get("nb_read_frames", 0)) != MASTER_FRAMES
        or video["r_frame_rate"] != "24/1"
        or len([s for s in streams if s["codec_type"] == "audio"]) != 1
    ):
        reject("assembled29s24fps contract failed")
    # Verify the PCM stream itself, not just the unchanged WAV sidecar.
    pcm = run_media(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(master),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-",
        ],
        deadline=cutoff,
    )
    with wave.open(io.BytesIO(audio)) as original:
        if (
            original.getsampwidth() != PCM_BYTES_PER_SAMPLE
            or pcm != original.readframes(original.getnframes())
        ):
            reject("assembled PCM differs from approved WAV")
    return {
        "master": file_record(master),
        "approved_audio": file_record(wav),
        "frames": 696,
        "fps": 24,
        "seconds": 29,
    }


def work(*, ledger, parts, output, base_url, setup_timings):  # noqa: C901, PLR0912, PLR0915 — single bounded pilot lifecycle.
    shots, audio = read_frozen(parts)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    experiment = Experiment(ledger)
    status = experiment.status()
    setup = json.loads(Path(setup_timings).read_text())
    if (
        setup.get("model_revision") != MODEL_REVISION
        or setup.get("sglang_revision") != SGLANG_REVISION
        or setup.get("precision") != "kitchen_int8"
        or setup.get("deadline") != status["deadline"]
    ):
        reject(
            "setup timing/profile does not match pinned runtime and immutable ledger deadline"
        )
    if len(Queue(ledger).jobs()) not in (0, PILOT_SHOTS):
        reject("pilot ledger must contain exactly six original shots or be empty")
    cutoff = min(status["deadline"] - 120, status["created"] + 3480)
    if status["stop_reason"] or time.time() >= cutoff:
        reject("experiment stopped or early work cutoff passed")
    cleaner = CleanerSupervision(ledger)
    cleaner.require_fresh(now=time.time())
    queue = Queue(ledger)
    for shot in shots:
        spec = {key: value for key, value in shot.items() if key != "image"}
        queue.append(
            project="frozen-test29",
            request_key=shot["shot"] + "-" + shot["version"],
            shot=shot["shot"],
            version=shot["version"],
            phase=0,
            spec=spec,
        )
    if any(
        j["phase"] != 0
        or j["original_id"] is not None
        or j["project"] != "frozen-test29"
        or j["status"] in ("failed", "cancelled")
        for j in queue.jobs()
    ):
        reject("only six immutable pilot originals may run; no chapter or paid retry")
    client = H3Client(base_url)
    submissions = Submissions(ledger)
    timings = {
        key: setup.get(key)
        for key in ("download_seconds", "compile_seconds", "startup_seconds")
    }
    if any(
        not isinstance(value, int | float) or value < 0 for value in timings.values()
    ):
        reject("finished download/startup/compile timing evidence is required")
    timings.update(
        {
            "image_startup_seconds": setup["started"] - status["created"],
            "assembly_seconds": None,
            "export_seconds": None,
        }
    )
    started = time.time()
    snapshot(queue, output, timings)
    try:
        if client.call("GET", "/health", deadline=cutoff).get("status") != "ok":
            reject("H3 model is not ready")
        timings["health_check_seconds"] = time.time() - started
        # Fixed cutoff includes startup/download/compile since reservation, not readiness.
        with experiment._tx() as db:
            db.execute("UPDATE experiment SET work_deadline=? WHERE id=1", (cutoff,))
        while True:
            cleaner.require_fresh(now=time.time())
            status = experiment.status()
            if status["stop_reason"] or time.time() >= cutoff:
                expired("experiment stopped or early work cutoff reached")
            job = queue.claim(
                now=time.time(), lease_seconds=max(0.01, cutoff - time.time())
            )
            if job is None:
                break
            shot = next(s for s in shots if s["shot"] == job["shot"])
            process_shot(
                queue,
                job,
                shot,
                client=client,
                submissions=submissions,
                output=output,
                cutoff=cutoff,
            )
            snapshot(queue, output, timings)
        started = time.time()
        assembly = assemble(queue, output, audio, cutoff=cutoff)
        timings["assembly_seconds"] = time.time() - started
        timings["export_seconds"] = sum(
            json.loads(j["result"])["timings"]["export_seconds"] for j in queue.jobs()
        )
        snapshot(queue, output, timings, assembly=assembly)
        # Success stops compute only after all artifacts + PCM are validated off-Pod.
        with experiment._tx() as db:
            experiment._stop(db, "success")
    except Exception as error:
        # Preserve hashes of any real partial outputs; never turn fixture media into success.
        for job in queue.jobs():
            if job["status"] == "running" and time.time() < job["lease_until"]:
                files = [
                    file_record(p) for p in output.glob(job["submission_id"] + ".*.mp4")
                ]
                queue.finish(
                    job["id"],
                    job["epoch"],
                    time.time(),
                    status="failed",
                    result={"error": str(error), "partial_artifacts": files},
                )
        snapshot(queue, output, timings)
        experiment.stop("failed")
        raise
    return output / "manifest.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "work"))
    parser.add_argument("parts", nargs="+")
    parser.add_argument("--ledger")
    parser.add_argument("--output")
    parser.add_argument(
        "--setup-timings", help="off-Pod copy of launcher h3-timings.json"
    )
    parser.add_argument(
        "--stage", choices=("test29s", "ch01", "ch02"), default="test29s"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:30010")
    args = parser.parse_args(argv)
    if args.stage != "test29s":
        parser.error(
            "CH1 has no approved meaningful-motion shotplan; CH2 script/audio/art are not approved/ready"
        )
    if args.command == "validate":
        shots, audio = read_frozen(args.parts)
        print(
            json.dumps(
                {
                    "shots": [
                        {k: v for k, v in s.items() if k != "image"} for s in shots
                    ],
                    "audio_sha256": digest(audio),
                    "total_seconds": 29,
                    "execution_authorized": False,
                },
                indent=2,
            )
        )
    else:
        if not args.ledger or not args.output or not args.setup_timings:
            parser.error("work requires --ledger, off-Pod --output and --setup-timings")
        print(
            work(
                ledger=args.ledger,
                parts=args.parts,
                output=args.output,
                base_url=args.base_url,
                setup_timings=args.setup_timings,
            )
        )


if __name__ == "__main__":
    main()
