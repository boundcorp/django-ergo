"""Native Worker lifecycle over the existing bounded single-rental controllers.

Admission/ownership lives in Django; each immutable attempt uses the existing
Experiment/Queue/RESTCleanup ledger on a durable, host-shared filesystem. All
controllers MUST share that filesystem and its POSIX locks. No subprocess CLI
bridge, bot schedule, or chat is the rental owner.
"""

# Existing controllers deliberately expose their shared ledger transaction API.
# ruff: noqa: SLF001
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from decimal import Decimal
from decimal import DecimalException
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from django_ergo.conversation.attachments import guess_media_type
from django_ergo.conversation.attachments import kind_for_media_type
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import Worker
from django_ergo.protected_env import is_protected_env

from .budget import admit_rental
from .cleanup import RESTCleanup
from .create import prepare
from .create import rental_budget
from .experiment import Experiment
from .frozen import read_frozen
from .h3 import H3Client
from .models import RunpodAccount
from .models import RunpodArtifact
from .models import RunpodJob
from .queue import Conflict
from .queue import Queue
from .supervisor import CleanerSupervision
from .transport import RunpodREST

FRESH_SECONDS = 180
OWNER_STALE_SECONDS = 180
MAX_REQUEST_KEY = 200
MAX_OUTPUT_BYTES = 2_000_000_000


def _account_policies():
    path = getattr(settings, "RUNPOD_ACCOUNTS_FILE", "")
    if path:
        try:
            policies = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            msg = "RunPod host policy file is missing or invalid"
            raise ValueError(msg) from None
    else:
        policies = getattr(settings, "RUNPOD_ACCOUNTS", {})
    if not isinstance(policies, dict):
        msg = "RunPod host policies must be a mapping"
        raise TypeError(msg)
    return policies


def account_policy(key):
    policy = _account_policies().get(key)
    if policy is None:
        msg = "RunPod account is not installed"
        raise ValueError(msg)
    if not isinstance(policy, dict):
        msg = "RunPod account policy must be a mapping"
        raise TypeError(msg)
    return policy


def authorize(policy, bot_name, user):
    if (
        not policy.get("enabled", False)
        or user is None
        or not user.is_active
        or not user.is_superuser
        or user.get_username() not in policy.get("allowed_users", [])
        or bot_name not in policy.get("allowed_bots", [])
    ):
        msg = "RunPod spending principal or bot is not authorized"
        raise PermissionError(msg)


def credential_name(policy):
    name = policy.get("api_key_env", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or is_protected_env(name):
        msg = "Invalid RunPod credential binding"
        raise ValueError(msg)
    return name


def client(job):
    # Fixed host-owned reference, never ToolContext's user-dependent fallback.
    name = credential_name(job.profile["binding"])
    return RunpodREST(lambda: os.environ.get(name), timeout=5)


def profiles(account, selected, ctx):
    result = []
    for name in selected:
        definition = {}
        reason = ""
        try:
            policy = account_policy(account)
            definition = policy.get("profiles", {}).get(name, {})
            authorize(policy, ctx.bot.name, ctx.user)
            binding = RunpodAccount.objects.filter(pk=account).first()
            if binding is None:
                reason = "Independent RunPod maintenance is not installed"
            else:
                _admission_config(binding, policy, definition)
        except (ValueError, PermissionError) as exc:
            reason = str(exc)
        except (KeyError, TypeError, DecimalException):
            reason = "Invalid host RunPod profile"
        result.append(
            {
                "id": name,
                "image": definition.get("config", {}).get("image"),
                "usable": not reason,
                "reason": reason or "Human approval and atomic admission required",
                "maximum_rental_seconds": 3600,
                "maximum_planned_usd": "3.00",
            }
        )
    return result


def _reconciled_cost(job, state, item):
    """Operator-reviewed conservative cost, never an inferred invoice."""
    message = "Invalid reconciled Runpod cost evidence"
    if not isinstance(item, dict):
        raise TypeError(message)
    observed = item.get("observed_at")
    if (
        state["phase"] != "terminated"
        or item.get("basis") != "conservative_elapsed_rate"
        or not isinstance(item.get("evidence"), str)
        or not item["evidence"]
        or type(observed) not in (int, float)
        or not job.created <= observed <= time.time()
    ):
        raise ValueError(message)
    try:
        amount = Decimal(item["amount_usd"])
    except (KeyError, TypeError, ValueError, DecimalException):
        raise ValueError(message) from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(message)
    return amount


def _committed_reservations(jobs, policy):
    reviewed = policy.get("reconciled_costs", {})
    if not isinstance(reviewed, dict):
        msg = "Invalid reconciled Runpod cost mapping"
        raise TypeError(msg)
    committed = Decimal(0)
    # Only actual or possibly active rentals carry liability. A cleaned-up
    # attempt that never started a create costs nothing, whatever its phase;
    # its row and lifetime count stay immutable. Created attempts stay at their
    # reservation until an operator-reviewed conservative cost replaces it.
    for job in jobs:
        if not job.cleanup_complete:
            committed += job.reservation_usd
            continue
        state = Experiment(job.ledger_path).status()
        if state["create_started"] == 0:
            continue
        if str(job.pk) in reviewed:
            committed += _reconciled_cost(job, state, reviewed[str(job.pk)])
        else:
            committed += job.reservation_usd
    return committed


def _admission_config(binding, policy, definition):
    """Validate and price a new intent while the account admission lock is held."""
    now = time.time()
    if definition.get("adapter") != "frozen_h3":
        msg = "Unsupported reviewed workload adapter"
        raise ValueError(msg)
    H3Client(definition["base_url"])
    limit = definition["max_output_bytes"]
    if type(limit) is not int or not 0 < limit <= MAX_OUTPUT_BYTES:
        msg = "Invalid approved output storage limit"
        raise ValueError(msg)
    if (
        binding.maintenance_at is None
        or not 0 <= now - binding.maintenance_at < FRESH_SECONDS
        or binding.maintenance_error
    ):
        msg = "Independent RunPod maintenance is missing, stale or failing"
        raise ValueError(msg)
    if not 0 <= now - definition.get("quote_observed_at", 0) < FRESH_SECONDS:
        msg = "RunPod price evidence is stale"
        raise ValueError(msg)
    if not os.environ.get(credential_name(policy)):
        msg = "RunPod credential binding is missing"
        raise ValueError(msg)
    jobs = RunpodJob.objects.filter(account=binding)
    if jobs.count() >= policy.get("max_rentals", 1):
        message = "RunPod lifetime rental limit reached; no replacement or top-up"
        raise ValueError(message)
    if jobs.filter(cleanup_complete=False).count() >= policy.get("max_concurrent", 1):
        msg = "RunPod unresolved rental concurrency limit reached"
        raise ValueError(msg)
    committed = _committed_reservations(jobs, policy)
    config = json.loads(json.dumps(definition["config"]))
    config["pricing"]["project_committed_usd"] = str(
        committed + Decimal(policy.get("prior_committed_usd", "0"))
    )
    config["pricing"]["project_cap_usd"] = str(Decimal(policy.get("budget_usd", "3")))
    admission = admit_rental(**rental_budget(config))
    if not admission.allowed:
        raise ValueError(admission.reason)
    return config


def start(ctx, account, selected, profile, request_key):
    policy = account_policy(account)
    authorize(policy, ctx.bot.name, ctx.user)
    if ctx.session is None or ctx.session.bot_name != ctx.bot.name:
        msg = "RunPod requires this bot's own chat"
        raise PermissionError(msg)
    if profile not in selected or profile not in policy.get("profiles", {}):
        msg = "RunPod profile is not allowed"
        raise PermissionError(msg)
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= MAX_REQUEST_KEY:
        msg = "A stable request_key of at most 200 characters is required"
        raise ValueError(msg)
    definition = policy["profiles"][profile]
    if definition.get("adapter") != "frozen_h3":
        msg = "Unsupported reviewed workload adapter"
        raise ValueError(msg)
    # Inputs are administrator-bound frozen archives, not arbitrary model paths.
    shots, _ = read_frozen(definition["parts"])
    name = credential_name(policy)
    now = time.time()
    spec_hash = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in definition.items()
                if key != "quote_observed_at"
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with transaction.atomic():
        binding, _ = RunpodAccount.objects.get_or_create(key=account)
        binding = RunpodAccount.objects.select_for_update().get(pk=binding.pk)
        old = RunpodJob.objects.filter(
            account=binding,
            bot_name=ctx.bot.name,
            owner_id=str(ctx.user.pk),
            request_key=request_key,
        ).first()
        if old:
            if old.spec_hash != spec_hash or old.session_identity != ctx.session.pk:
                msg = "Request key already binds a different specification or chat"
                raise Conflict(msg)
            return status(old)
        config = _admission_config(binding, policy, definition)
        snapshot = {
            **definition,
            "config": config,
            "binding": {"api_key_env": name},
            "profile_id": profile,
        }
        job = RunpodJob(
            account=binding,
            bot_name=ctx.bot.name,
            owner_id=str(ctx.user.pk),
            session=ctx.session,
            session_identity=ctx.session.pk,
            request_key=request_key,
            spec_hash=spec_hash,
            profile=snapshot,
            created=now,
            deadline=now + config["timeline"]["rental_seconds"],
        )
        _bind_job(job, ctx, policy, shots)
    return status(job)


def _bind_job(job, ctx, policy, shots):
    """Bind reusable rental primitives before the transaction publishes work."""
    from .native_h3 import initialize

    root = Path(policy["ledger_root"]).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / str(job.account.installation) / str(job.pk)
    directory.mkdir(parents=True, mode=0o700)
    job.ledger_path = str(directory / "rental.sqlite3")
    state = prepare(job.ledger_path, job.profile["config"], now=job.created)
    Path(job.ledger_path).chmod(0o600)
    CleanerSupervision(job.ledger_path).configure(
        interval_seconds=60,
        operation_timeout=15,
        freshness_seconds=180,
    )
    job.reservation_usd = Decimal(state["reservation"])
    job.save()
    initialize(job, shots)
    job.worker = ctx.workers.start(
        "runpod:watch",
        title=f"RunPod {job.profile['profile_id']}",
        job_id=str(job.pk),
    )
    job.save(update_fields=["worker"])


def owned(ctx, account, job_id):
    try:
        job = RunpodJob.objects.get(
            pk=job_id,
            account_id=account,
            bot_name=ctx.bot.name,
            owner_id=str(ctx.user.pk),
            session_identity=ctx.session.pk,
        )
    except (RunpodJob.DoesNotExist, ValueError, AttributeError, ValidationError):
        msg = "RunPod job is not owned by this bot, principal and chat"
        raise PermissionError(msg) from None
    return job


@contextmanager
def operation(job):
    # Nonblocking cross-process exclusion. Unlike a renewable DB lease, a dead
    # controller releases this lock without discarding a late provider receipt.
    with Path(job.ledger_path + ".native.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def status(job):
    state = Experiment(job.ledger_path).status()
    return {
        "job_id": str(job.pk),
        "worker_id": str(job.worker_id) if job.worker_id else None,
        **{
            key: state[key]
            for key in (
                "phase",
                "stop_reason",
                "outcome",
                "cleanup_error",
                "deadline",
                "create_started",
            )
        },
        "cleanup_complete": state["phase"] == "terminated",
        "reservation_usd": str(job.reservation_usd),
        "actual_usd": None,
        "cost_basis": "planned reservation; invoice unavailable",
        "manifest": job.manifest,
        "error": job.error,
    }


def stop(job, reason="cancelled"):
    # Never wait on an in-flight Worker before making stop durable.
    Experiment(job.ledger_path).stop(reason)
    Queue(job.ledger_path).cancel()


def invalid_owner(job):
    if not job.session_id or not job.worker_id:
        return True
    if not ConversationSession.objects.filter(
        pk=job.session_id,
        status="active",
        bot_name=job.bot_name,
    ).exists():
        return True
    if not Worker.objects.filter(
        pk=job.worker_id,
        status__in=["queued", "running"],
    ).exists():
        return True
    user = get_user_model().objects.filter(pk=job.owner_id).first()
    try:
        policy = account_policy(job.account_id)
        authorize(policy, job.bot_name, user)
        if job.profile["profile_id"] not in policy.get("profiles", {}):
            return True
    except (ValueError, TypeError, PermissionError):
        return True
    return False


def _owner_stale(job, now):
    updated = (
        Worker.objects.filter(pk=job.worker_id)
        .values_list("updated_at", flat=True)
        .first()
    )
    if updated is None:
        return True
    if timezone.is_naive(updated):
        updated = timezone.make_aware(updated, timezone.get_default_timezone())
    return now - updated.timestamp() >= OWNER_STALE_SECONDS


def reconcile_job(job, *, now):
    experiment = Experiment(job.ledger_path)
    with experiment._tx() as db:
        state = experiment._expire(db, experiment._row(db), now)
    if not state["stop_reason"]:
        if invalid_owner(job) or _owner_stale(job, now):
            stop(job, "worker_lost")
        elif state["phase"] != "ready" and now >= (
            job.created
            + job.profile["config"]["timeline"]["startup_seconds"]
            + job.profile["config"]["timeline"]["download_seconds"]
        ):
            stop(job, "failed")
    if experiment.status()["stop_reason"]:
        Queue(job.ledger_path).cancel()
    with operation(job) as acquired:
        if not acquired:
            return False
        # Maintenance, including bounded final cleanup, never extends work clocks.
        CleanerSupervision(job.ledger_path)
        with experiment._tx() as db:
            experiment._expire(db, experiment._row(db), now)
            db.execute(
                "UPDATE cleaner_supervision SET heartbeat=?,process_id=?,executing=1,tick_error=NULL WHERE id=1",
                (now, os.getpid()),
            )
        state = RESTCleanup(job.ledger_path).reconcile_once(now=now, client=client(job))
        if state["stop_reason"]:
            from .native_h3 import retain_outputs

            try:
                retain_outputs(job)
            except Exception:  # noqa: BLE001 — stopping spend precedes artifact recovery
                RunpodJob.objects.filter(pk=job.pk).update(
                    error="partial_export_failed"
                )
        complete = state["phase"] == "terminated"
        RunpodJob.objects.filter(pk=job.pk).update(cleanup_complete=complete)
        return not state["cleanup_error"]


def attempt_cleanup(job):
    """Try bounded cleanup, retaining a safe retry error instead of suppressing it."""
    try:
        return reconcile_job(job, now=time.time())
    except Exception:  # noqa: BLE001 — no secret-bearing provider exception text
        with Experiment(job.ledger_path)._tx() as db:
            db.execute(
                "UPDATE experiment SET cleanup_error='maintenance_failed' WHERE id=1"
            )
        return False


def reconcile_once(*, now=None):
    """Independent host maintenance: no bot registry, plugin instance or user ctx."""
    now = time.time() if now is None else now
    try:
        policies = _account_policies()
    except (ValueError, TypeError):
        # Revoking or losing admission policy must not disable existing cleanup.
        policies = {}
    keys = set(policies) | set(RunpodAccount.objects.values_list("key", flat=True))
    results = {}
    for key in sorted(keys):
        binding, _ = RunpodAccount.objects.get_or_create(key=key)
        ok = True
        # Include terminated ledgers: late receipts can reopen liability.
        for job in RunpodJob.objects.filter(account=binding).select_related(
            "session", "worker"
        ):
            try:
                ok = reconcile_job(job, now=now) and ok
            except Exception:  # noqa: BLE001 — keep cleanup durable; never leak credentials
                RunpodJob.objects.filter(pk=job.pk).update(error="maintenance_failed")
                ok = False
        RunpodAccount.objects.filter(pk=key).update(
            maintenance_at=now if ok else binding.maintenance_at,
            maintenance_error="" if ok else "cleanup_pending_or_failed",
        )
        results[key] = ok
    return results


def _attachment(job, entry):
    # The standard save_session_file helper allocates a fresh storage name. A
    # deterministic plugin-owned key instead recovers a crash after storage save
    # but before the attachment transaction commits, without orphaning copies.
    identity = f"{job.pk}:{entry['name']}:{entry['sha256']}"
    # ConversationAttachment.file has the standard 100-character storage key.
    key = "ergo/runpod/" + hashlib.sha256(identity.encode()).hexdigest()
    content = entry["content"]
    if hashlib.sha256(content).hexdigest() != entry["sha256"]:
        message = "Durable artifact verification failed"
        raise Conflict(message)
    if default_storage.exists(key):
        checksum = hashlib.sha256()
        with default_storage.open(key, "rb") as existing:
            for chunk in iter(lambda: existing.read(65536), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != entry["sha256"]:
            message = "Artifact storage identity conflict"
            raise Conflict(message)
    else:
        saved = default_storage.save(key, ContentFile(content))
        if saved != key:
            default_storage.delete(saved)
            message = "Artifact storage could not preserve its stable identity"
            raise Conflict(message)
    media_type = guess_media_type(entry["name"])
    return ConversationAttachment.objects.create(
        session=job.session,
        filename=entry["name"],
        file=key,
        source="bot",
        kind=kind_for_media_type(media_type),
        media_type=media_type,
        size=entry["size"],
        sha256=entry["sha256"],
        metadata={"runpod_job": str(job.pk), **json.loads(entry["metadata"])},
    )


def collect(job):
    """Publish only ledger-verified off-Pod outputs; repeated calls reuse rows."""
    from .native_h3 import generation_ledger

    experiment = Experiment(job.ledger_path)
    if not job.session_id:
        return {**status(job), "output_unavailable": "chat was deleted"}
    with experiment._tx() as db:
        exports = db.execute(
            "SELECT * FROM experiment_exports ORDER BY name"
        ).fetchall()
    for entry in exports:
        with transaction.atomic():
            row, _ = RunpodArtifact.objects.get_or_create(
                job=job,
                name=entry["name"],
                defaults={"sha256": entry["sha256"]},
            )
            row = RunpodArtifact.objects.select_for_update().get(pk=row.pk)
            if row.sha256 != entry["sha256"]:
                msg = "Durable artifact hash changed"
                raise Conflict(msg)
            if row.attachment_id:
                continue
            attachment = _attachment(job, entry)
            row.attachment = attachment
            row.save(update_fields=["attachment"])
    return {
        **status(job),
        "generations_jsonl": generation_ledger(job),
        "attachments": [
            {
                "name": row.name,
                "sha256": row.sha256,
                "attachment_id": str(row.attachment_id),
            }
            for row in sorted(
                RunpodArtifact.objects.filter(
                    job=job, attachment__isnull=False
                ).select_related("attachment"),
                key=lambda row: (
                    row.attachment.metadata.get("item_index", 6),
                    row.name,
                ),
            )
        ],
    }
