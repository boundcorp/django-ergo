"""One native polling step for the reviewed frozen H3 workload.

Reuses the original no-replay queue, H3 submissions, media validation and export
protocol. The Worker maintains authenticated, ownership-bound SSH access to the
rental's loopback listener without exposing a public inference port.
"""

# Existing ledger controllers intentionally share transaction primitives.
# ruff: noqa: SLF001
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from . import native
from . import offers
from .cleanup import RESTCleanup
from .experiment import Experiment
from .frozen import read_frozen
from .h3 import MODEL
from .h3 import H3Client
from .h3 import Submissions
from .h3 import file_record
from .h3 import request_body
from .native_tunnel import TunnelUnavailable
from .native_tunnel import ensure as ensure_tunnel
from .queue import Queue
from .runner import assemble
from .runner import process_shot
from .runner import snapshot
from .supervisor import CleanerSupervision
from .transport import OwnershipError
from .transport import ProviderError

# RunPod REST shares one small per-key budget (observed 11/minute, 225/hour)
# with maintenance. The Worker steps every few seconds, so it re-reads the owned
# Pod's ownership and rate at most once a minute and treats a failed read,
# including HTTP 429, as a wait. Rental deadlines still apply independently.
OWNED_POD_SECONDS = 60


def _owned_pod_throttled(ctx, job, provider):
    """Return ``(pod, wait_message)``; at most one provider read per interval."""
    now = time.time()
    cache = ctx.state.get("owned_pod") or {}
    if now < cache.get("next", 0):
        return cache.get("pod"), None
    try:
        pod = _owned_pod(job, provider)
    except OwnershipError:
        raise
    except ProviderError as error:
        wait = getattr(error, "retry_after", OWNED_POD_SECONDS)
        ctx.state["owned_pod"] = {**cache, "next": now + wait}
        if cache.get("pod") is None:
            return (
                None,
                f"Provider read deferred ({type(error).__name__}); retry in {wait}s",
            )
        return cache["pod"], None
    ctx.state["owned_pod"] = {"pod": pod, "next": now + OWNED_POD_SECONDS}
    return pod, None


def _owned_pod(job, provider):
    cleanup = RESTCleanup(job.ledger_path)
    with cleanup.experiment._tx() as db:
        profile = cleanup._profile(db)
        receipts = [
            r[0] for r in db.execute("SELECT resource_id FROM experiment_receipts")
        ]
    # Recover a response-lost create only by exact durable ownership, never name.
    pods = [provider.get_pod(receipt) for receipt in receipts]
    if not receipts:
        pods = provider.list_pods()
    matches = [pod for pod in pods if pod and profile.matches(pod, request_bound=True)]
    for pod in matches:
        cleanup.experiment.receipt(pod["id"])
        rate = Decimal(str(pod.get("costPerHr", "NaN")))
        approved = Decimal(
            job.profile["config"]["pricing"]["compute_hourly_usd"]["amount"]
        )
        if not rate.is_finite() or rate < 0 or rate > approved:
            native.stop(job, "failed")
            message = "Observed compute rate is missing or above the approved quote"
            raise ValueError(message)
    if len(matches) > 1:
        native.stop(job, "failed")
        msg = "Multiple owned resources; independent cleanup remains pending"
        raise ValueError(msg)
    if receipts and len(matches) != len(receipts):
        native.stop(job, "failed")
        msg = "Rental missing or ownership unverified"
        raise ValueError(msg)
    return matches[0] if matches else None


def _publish_manifest(job, queue, output, *, assembly=None):
    manifest = snapshot(queue, output, {}, assembly=assembly)
    manifest["job_id"] = str(job.pk)
    manifest["reservation_usd"] = str(job.reservation_usd)
    manifest["actual_usd"] = None
    job.manifest = manifest
    job.save(update_fields=["manifest"])
    temporary = output / "manifest.next"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    temporary.replace(output / "manifest.json")
    return manifest


def _export(job, output, *, final=False, retained=False):
    experiment = Experiment(job.ledger_path)
    files = sorted(
        p
        for p in output.iterdir()
        if p.suffix in (".mp4", ".mov", ".wav") or (final and p.name == "manifest.json")
    )
    total = sum(p.stat().st_size for p in files)
    if total > job.profile["max_output_bytes"]:
        msg = "Native outputs exceed approved storage limit"
        raise ValueError(msg)
    items = Queue(job.ledger_path).jobs()
    for path in files:
        record = file_record(path)
        experiment.export(
            now=time.time(),
            name=path.name,
            content=path.read_bytes(),
            retained=retained,
            metadata={
                "job_id": str(job.pk),
                "artifact": record,
                "item_index": next(
                    (
                        index
                        for index, item in enumerate(items)
                        if path.name.startswith(item["submission_id"])
                    ),
                    len(items),
                ),
            },
        )


def retain_outputs(job):
    """Archive already-off-Pod partial outputs without contacting the rental."""
    output = Path(job.ledger_path).parent / "outputs"
    if output.is_dir() and not job.manifest.get("complete"):
        _publish_manifest(job, Queue(job.ledger_path), output)
        _export(job, output, final=True, retained=True)


def initialize(job, shots):
    """Commit original positions before publishing the Worker or any create."""
    queue = Queue(job.ledger_path)
    for shot in shots:
        queue.append(
            project="frozen-test29",
            request_key=shot["shot"] + "-" + shot["version"],
            shot=shot["shot"],
            version=shot["version"],
            phase=0,
            spec={key: value for key, value in shot.items() if key != "image"},
        )
    output = Path(job.ledger_path).parent / "outputs"
    output.mkdir(exist_ok=True)
    _publish_manifest(job, queue, output)


def step(ctx, job):  # noqa: C901, PLR0911, PLR0912, PLR0915 — one bounded native lifecycle step
    experiment = Experiment(job.ledger_path)
    state = experiment.status()
    cutoff = state["deadline"] - 120
    if time.time() >= cutoff:
        native.stop(job, "failed")
        return "Early work cutoff reached; independent cleanup pending"
    if native.invalid_owner(job) or ctx.stopping:
        native.stop(job, "worker_lost")
        return "Owner or Worker cancelled; cleanup pending"
    try:
        CleanerSupervision(job.ledger_path).require_fresh(now=time.time())
    except ValueError:
        return "Waiting for an independent executing cleaner; no new provider operation"
    provider = native.client(job)
    if state["phase"] == "reserved":
        # Live stock is read and the best offer inside the admitted envelope is
        # created on; proven no-capacity refusals fall through to the next one.
        # Live calls additionally enforce the independent cleaner heartbeat gate.
        return offers.launch(job, provider, stop=native.stop)
    pod, deferred = _owned_pod_throttled(ctx, job, provider)
    if deferred:
        return deferred
    if pod is None:
        return "Create result unknown; inventory inspection only, no replacement"
    startup_deadline = (
        state["created"]
        + job.profile["config"]["timeline"]["startup_seconds"]
        + job.profile["config"]["timeline"]["download_seconds"]
    )
    if state["phase"] != "ready" and time.time() >= startup_deadline:
        native.stop(job, "failed")
        return "Startup deadline reached; independent cleanup pending"
    try:
        base_url = ensure_tunnel(job, provider, pod, deadline=cutoff)
    except TunnelUnavailable:
        if state["phase"] == "ready":
            raise
        if time.time() >= startup_deadline:
            native.stop(job, "failed")
            return "Startup deadline reached; independent cleanup pending"
        return "Waiting for authenticated direct SSH access to the owned rental"
    h3 = H3Client(base_url)
    if state["phase"] != "ready":
        try:
            healthy = h3.call("GET", "/health", deadline=cutoff).get("status") == "ok"
        except OSError:
            healthy = False
        if not healthy:
            if not ctx.state.get("tunnel_notice"):
                ctx.tell(
                    "RunPod is provisioning. The Worker maintains authenticated "
                    "SSH access to this rental's loopback model listener; there "
                    "is no public inference port or automatic replacement."
                )
                ctx.state["tunnel_notice"] = True
            if time.time() >= startup_deadline:
                native.stop(job, "failed")
                return "Startup deadline reached; independent cleanup pending"
            return "Waiting for model readiness through the approved rental tunnel"
        # Record the fixed early work deadline durably, not in Worker state.
        with experiment._tx() as db:
            row = experiment._running(db, time.time())
            if row is None:
                return "Stop requested; cleanup pending"
            db.execute(
                "UPDATE experiment SET phase='ready',work_deadline=? WHERE id=1",
                (cutoff,),
            )
    shots, audio = read_frozen(job.profile["parts"])
    queue = Queue(job.ledger_path)
    output = Path(job.ledger_path).parent / "outputs"
    output.mkdir(exist_ok=True)
    jobs = queue.jobs()
    current = next((item for item in jobs if item["status"] == "running"), None)
    if current is None:
        current = queue.claim(
            now=time.time(), lease_seconds=max(0.01, cutoff - time.time())
        )
    if current is None:
        assembly = assemble(queue, output, audio, cutoff=cutoff)
        _publish_manifest(job, queue, output, assembly=assembly)
        _export(job, output, final=True)
        with experiment._tx() as db:
            experiment._stop(db, "success")
        return "All six ordered outputs and approved audio exported; cleanup pending"
    shot = next(s for s in shots if s["shot"] == current["shot"])
    submissions = Submissions(job.ledger_path)
    identity = current["submission_id"]
    record = submissions.inspect(identity)
    if current["submission_state"] == "not_sent":
        # Stop is rechecked just before authorizing the outbound side effect.
        if experiment.status()["stop_reason"] or ctx.stopping:
            native.stop(job)
            return "Cancelled; cleanup pending"
        queue.prepare_submission(current["id"], current["epoch"], time.time())
        record = submissions.submit_once(
            identity, request_body(shot), client=h3, deadline=cutoff
        )
    if not record or record["state"] != "accepted":
        msg = "H3 submission unknown; no generation replay is permitted"
        raise ValueError(msg)
    queue.record_receipt(current["id"], record["receipt"])
    if current["submission_state"] != "accepted":
        queue.observe_submission(
            current["id"], current["epoch"], time.time(), state="accepted"
        )
    remote = h3.call("GET", "/v1/videos/" + record["receipt"], deadline=cutoff)
    if remote.get("status") == "failed":
        msg = "H3 generation failed; no paid retry"
        raise ValueError(msg)
    if remote.get("status") != "completed":
        return f"Generating {shot['shot']} {shot['version']}; {sum(j['status'] == 'succeeded' for j in jobs)}/6 exported"
    # This path inspects an already-completed receipt and reuses actual media
    # validation/conforming; it never submits or loops over unfinished work.
    current = next(j for j in queue.jobs() if j["id"] == current["id"])
    process_shot(
        queue,
        current,
        shot,
        client=h3,
        submissions=submissions,
        output=output,
        cutoff=cutoff,
    )
    _publish_manifest(job, queue, output)
    _export(job, output)
    return f"Exported {shot['shot']} {shot['version']}"


def generation_ledger(job):
    """Portable Storytime provenance; shared rental liability is never per-output spend."""
    rows = []
    for index, item in enumerate(Queue(job.ledger_path).jobs()):
        if item["submission_state"] == "not_sent" or item["status"] not in (
            "succeeded",
            "failed",
            "cancelled",
        ):
            continue
        spec = json.loads(item["spec"])["spec"]
        result = json.loads(item["result"]) if item["result"] else {}
        record = {
            "job": f"runpod:{job.pk}",
            "entry_id": str(index),
            "project": job.profile.get("project", "frozen-test29"),
            "kind": "video",
            "provider": "runpod",
            "model": MODEL,
            "shot": item["shot"],
            "prompt": spec["prompt"],
            "status": "succeeded" if item["status"] == "succeeded" else "failed",
            "details": {
                "rental_job_id": str(job.pk),
                "item_index": index,
                "reservation_usd": str(job.reservation_usd),
                "cost_scope": "shared rental reservation, not a per-output charge",
                "historical_request": spec["historical_request"],
                "known_flags": spec["known_flags"],
                "artifact": result.get("conformed"),
            },
        }
        if result.get("conformed"):
            record["output"] = result["conformed"]["name"]
        if item["status"] != "succeeded":
            record["error"] = item["status"]
        rows.append(json.dumps(record, sort_keys=True))
    return "\n".join(rows)
