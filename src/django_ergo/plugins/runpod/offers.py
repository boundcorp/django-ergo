"""Launch-time RunPod offer selection, no-capacity fallback and wait-for-stock.

The admitted profile is an envelope (``config.hardware`` plus the hourly
ceiling in ``config.pricing.compute_hourly_usd``). At launch the Worker reads
live Pod stock, filters it by that envelope, orders it by GPU preference then
price, and creates on the best offer. An explicit no-capacity refusal that
inventory proves created nothing releases the attempt for the next offer in
the same admission; an uncertain create is never retried. Every poll and
attempt is recorded in the rental ledger for status.
"""

# The rental ledger's transaction primitives are shared on purpose.
# ruff: noqa: SLF001
from __future__ import annotations

import json
import time
from decimal import Decimal
from decimal import InvalidOperation

from . import hardware
from .cleanup import RESTCleanup
from .create import bind_offer
from .experiment import Experiment
from .transport import ProviderError

AVAILABILITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
SEEN_LIMIT = 20


def stock_query(hw):
    """Catalog query for live Pod availability in the profile's countries."""
    query = {
        "include": "AVAILABILITY",
        "product": "POD",
        "cloud": hw["cloud_type"],
        "count": str(hw["gpu_count"]),
    }
    if hw["countries"]:
        query["countryCodes"] = ",".join(hw["countries"])
    if hw["min_cuda_version"]:
        query["minCudaVersion"] = hw["min_cuda_version"]
    return query


def _price(gpu, hw):
    try:
        unit = Decimal(str(gpu["price"][hw["cloud_type"].lower()]))
    except (KeyError, TypeError, InvalidOperation):
        return None
    if not unit.is_finite() or unit <= 0:
        return None
    return unit * hw["gpu_count"]


def _gpu_reason(gpu, hw, gpu_catalog, cap):  # noqa: PLR0911 -- one reason per rule
    """Why a whole GPU type is outside the envelope, or None."""
    entry = gpu_catalog.get(gpu.get("id"))
    if entry is None:
        return "not in gpu_catalog (compute capability unknown)"
    if gpu.get("manufacturer", "NVIDIA") != "NVIDIA":
        return "not an NVIDIA CUDA GPU"
    memory = gpu.get("memory", entry["vram_gb"])
    if not isinstance(memory, int | float) or memory < hw["min_vram_gb"]:
        return f"{memory} GB VRAM is below {hw['min_vram_gb']} GB"
    if (
        hw["compute_capabilities"]
        and entry["compute_capability"] not in hw["compute_capabilities"]
    ):
        return f"compute {entry['compute_capability']} is not supported by the image"
    if gpu.get(hw["cloud_type"].lower()) is False:
        return f"not offered on {hw['cloud_type']} cloud"
    price = _price(gpu, hw)
    if price is None:
        return "no list price"
    if price > cap:
        return f"${price}/h is above the ${cap}/h ceiling"
    if gpu.get("availability", "NONE") == "NONE" or not gpu.get("dataCenters"):
        return "out of stock"
    return None


def evaluate(gpus, hw, gpu_catalog, cap):  # noqa: C901 -- one filter per requirement
    """Return (offers best-first, seen) for one catalog snapshot.

    ``seen`` explains every considered GPU type and data center so a human can
    see why nothing launches. GPU types neither preferred nor allowed by
    ``any_gpu`` are only counted.
    """
    preference = {gpu: index for index, gpu in enumerate(hw["gpus"])}
    offers, seen, ignored = [], [], 0
    for gpu in gpus:
        gpu_id = gpu.get("id")
        if not isinstance(gpu_id, str):
            continue
        if gpu_id not in preference and not hw["any_gpu"]:
            ignored += 1
            continue
        price = _price(gpu, hw)
        row = {
            "gpu_type": gpu_id,
            "hourly_usd": None if price is None else str(price),
            "availability": gpu.get("availability", "NONE"),
        }
        reason = _gpu_reason(gpu, hw, gpu_catalog, cap)
        if reason is not None:
            seen.append({**row, "eligible": False, "reason": reason})
            continue
        centers = []
        for center in gpu["dataCenters"]:
            center_id = center.get("id") if isinstance(center, dict) else None
            level = center.get("availability") if center_id else None
            if not isinstance(center_id, str):
                continue
            if hw["data_centers"] != "any" and center_id not in hw["data_centers"]:
                why = "data center not allowed"
            elif center_id in hw["exclude_data_centers"]:
                why = "data center excluded"
            elif level not in AVAILABILITY_RANK:
                why = "out of stock"
            else:
                why = None
                offers.append(
                    {
                        "gpu_type": gpu_id,
                        "data_center_id": center_id,
                        "hourly_usd": str(price),
                        "availability": level,
                        "_rank": (
                            preference.get(gpu_id, len(preference)),
                            price,
                            AVAILABILITY_RANK[level],
                            hw["data_centers"].index(center_id)
                            if hw["data_centers"] != "any"
                            else 0,
                            center_id,
                        ),
                    }
                )
            centers.append({"id": center_id, "availability": level, "skip": why})
        seen.append({**row, "eligible": True, "data_centers": centers})
    offers.sort(key=lambda offer: offer["_rank"])
    for offer in offers:
        del offer["_rank"]
    seen.sort(key=lambda row: preference.get(row["gpu_type"], len(preference)))
    return offers, {"gpus": seen[:SEEN_LIMIT], "ignored_gpu_types": ignored}


def _tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS offer_attempts (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            gpu_type TEXT NOT NULL, data_center_id TEXT NOT NULL,
            hourly_usd TEXT NOT NULL, availability TEXT NOT NULL,
            quoted_at REAL NOT NULL, outcome TEXT NOT NULL, finished_at REAL
        );
        CREATE TABLE IF NOT EXISTS stock_watch (
            id INTEGER PRIMARY KEY CHECK(id=1),
            first_poll REAL NOT NULL, last_poll REAL NOT NULL,
            polls INTEGER NOT NULL, eligible INTEGER NOT NULL,
            error TEXT, seen TEXT NOT NULL
        );
    """)


def history(path):
    """Ledger record of stock polls and every offer attempt, for status."""
    experiment = Experiment(path)
    with experiment._tx() as db:
        _tables(db)
        watch = db.execute("SELECT * FROM stock_watch WHERE id=1").fetchone()
        attempts = [
            dict(row) for row in db.execute("SELECT * FROM offer_attempts ORDER BY seq")
        ]
    chosen = next(
        (a for a in reversed(attempts) if a["outcome"] in ("created", "uncertain")),
        None,
    )
    return {
        "offer": chosen,
        "offer_attempts": attempts,
        "stock": None
        if watch is None
        else {
            "first_poll": watch["first_poll"],
            "last_poll": watch["last_poll"],
            "polls": watch["polls"],
            "eligible_offers": watch["eligible"],
            "error": watch["error"],
            "seen": json.loads(watch["seen"]),
        },
    }


def _record_poll(experiment, now, offers, seen, error):
    with experiment._tx() as db:
        _tables(db)
        db.execute(
            """INSERT INTO stock_watch(id, first_poll, last_poll, polls, eligible, error, seen)
            VALUES (1, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_poll=excluded.last_poll,
            polls=polls+1, eligible=excluded.eligible, error=excluded.error,
            seen=excluded.seen""",
            (now, now, len(offers), error, json.dumps(seen, sort_keys=True)),
        )
        return db.execute("SELECT * FROM stock_watch WHERE id=1").fetchone()


def _summary(seen):
    parts = []
    for row in seen["gpus"]:
        if not row["eligible"]:
            parts.append(f"{row['gpu_type']}: {row['reason']}")
            continue
        centers = ", ".join(
            f"{c['id']} {c['availability'] or 'NONE'}"
            + (f" ({c['skip']})" if c["skip"] else "")
            for c in row["data_centers"]
        )
        parts.append(f"{row['gpu_type']} ${row['hourly_usd']}/h: {centers}")
    return "; ".join(parts) or "no matching GPU types listed"


def _attempt(experiment, offer, now, outcome, seq=None):
    with experiment._tx() as db:
        _tables(db)
        if seq is not None:
            db.execute(
                "UPDATE offer_attempts SET outcome=?, finished_at=? WHERE seq=?",
                (outcome, now, seq),
            )
            return seq
        return db.execute(
            """INSERT INTO offer_attempts
            (gpu_type, data_center_id, hourly_usd, availability, quoted_at, outcome)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                offer["gpu_type"],
                offer["data_center_id"],
                offer["hourly_usd"],
                offer["availability"],
                now,
                outcome,
            ),
        ).lastrowid


def _refusals(experiment):
    with experiment._tx() as db:
        _tables(db)
        return db.execute(
            "SELECT COUNT(*) FROM offer_attempts WHERE outcome='no_capacity'"
        ).fetchone()[0]


def ceiling(config):
    return Decimal(config["pricing"]["compute_hourly_usd"]["amount"])


def _create_on(job, provider, offer, now):
    """Bind and create on one offer; returns the recorded attempt outcome."""
    config = job.profile["config"]
    experiment = Experiment(job.ledger_path)
    seq = _attempt(experiment, offer, now, "bound")
    try:
        bind_offer(job.ledger_path, config, offer)
    except ValueError as exc:
        # A policy/catalog problem with this offer, not stock: try the next.
        outcome = f"invalid: {exc}"[:200]
    else:
        state = RESTCleanup(job.ledger_path).create_once(
            now=time.time(), client=provider, live_execute=config["live_execute"]
        )
        if state["create_started"]:
            with experiment._tx() as db:
                received = db.execute("SELECT 1 FROM experiment_receipts").fetchone()
            outcome = "created" if received else "uncertain"
        elif state["stop_reason"]:
            outcome = "not_sent"
        else:
            outcome = "no_capacity"
    _attempt(experiment, offer, time.time(), outcome, seq)
    return outcome


def launch(job, provider, *, stop, now=None):
    """One launch step for a reserved job; returns Worker progress text.

    ``stop(job, reason)`` persists a stop intent. Polls are rate-limited by
    ``stock_poll_seconds``; the rental clock is restarted while nothing exists
    so waiting never spends the rental interval.
    """
    from .models import RunpodJob

    now = time.time() if now is None else now
    config = job.profile["config"]
    hw = hardware.requirements(config)
    path = job.ledger_path
    experiment = Experiment(path)
    cleanup = RESTCleanup(path)
    with experiment._tx() as db:
        _tables(db)
        watch = db.execute("SELECT * FROM stock_watch WHERE id=1").fetchone()
    if watch is not None and now - watch["last_poll"] < hw["stock_poll_seconds"]:
        wait = int(hw["stock_poll_seconds"] - (now - watch["last_poll"]))
        return (
            f"Waiting for stock matching profile ({watch['polls']} polls); "
            f"next check in {wait}s"
        )
    # A request bound but never POSTed (crash between bind and send) is released.
    cleanup.retire_unsent()
    state = experiment.rebase(now=now)
    if state is None:
        return "Stop requested before create; no rental was sent"
    RunpodJob.objects.filter(pk=job.pk).update(deadline=state["deadline"])
    try:
        gpus = provider.gpu_stock(stock_query(hw))
        error = None
    except (ProviderError, ValueError):
        gpus, error = [], "stock query failed"
    gpu_catalog = config.get("gpu_catalog") or hardware.catalog()
    offers, seen = evaluate(gpus, hw, gpu_catalog, ceiling(config))
    watch = _record_poll(experiment, now, offers, seen, error)
    refused = _refusals(experiment)
    for offer in offers:
        if refused >= hw["max_create_attempts"]:
            break
        outcome = _create_on(job, provider, offer, now)
        if outcome in ("created", "uncertain"):
            return (
                f"Rental create sent once: {offer['gpu_type']} in "
                f"{offer['data_center_id']} at ${offer['hourly_usd']}/h; "
                "inspecting durable receipt"
            )
        if outcome == "not_sent":
            return "Stop requested before create; no rental was sent"
        refused += outcome == "no_capacity"
    seen_text = _summary(seen)
    if refused >= hw["max_create_attempts"]:
        reason = f"{refused} no-capacity refusals"
    elif now - watch["first_poll"] >= hw["stock_wait_seconds"]:
        reason = f"waited {int(now - watch['first_poll'])}s"
    else:
        wait = hw["stock_poll_seconds"]
        return (
            f"No stock matching profile yet ({error or seen_text}); "
            f"rechecking every {wait}s for up to {hw['stock_wait_seconds']}s"
        )
    RunpodJob.objects.filter(pk=job.pk).update(error="no_stock")
    stop(job, "no_stock")
    return f"No stock matching profile after {reason}; no rental was created. Seen: {seen_text}"
