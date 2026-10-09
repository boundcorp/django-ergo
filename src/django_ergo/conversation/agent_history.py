"""Native coding-agent session collection, attribution and private artifact storage."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import transaction

from django_ergo.pricing import price_for
from django_ergo.settings import api_settings


@dataclass(frozen=True)
class ProjectIdentity:
    project_key: str
    canonical_remote: str
    repo_root: str
    worktree_path: str
    confidence: str
    error: str = ""


def canonical_remote(remote: str) -> str:
    """Normalize a git remote without preserving credentials or a .git suffix."""
    remote = remote.strip()
    if re.match(r"^[^/@:]+@[^/:]+:.+", remote):
        user_host, path = remote.split(":", 1)
        return f"ssh://{user_host.split('@', 1)[1].lower()}/{path.rstrip('/').removesuffix('.git')}"
    parsed = urlsplit(remote)
    if not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/").removesuffix(".git")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed executable, cwd is an argument.
        ["git", "-C", cwd, *args],  # noqa: S607 — resolved from trusted PATH.
        capture_output=True,
        check=False,
        text=True,
    ).stdout.strip()


def derive_project_identity(cwd: str, host_namespace: str = "local") -> ProjectIdentity:
    """Snapshot repository identity for a cwd without inventing a shared project row."""
    root = _git(cwd, "rev-parse", "--show-toplevel")
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if root:
        remotes = _git(cwd, "remote").splitlines()
        selected = (
            "origin" if "origin" in remotes else (sorted(remotes)[0] if remotes else "")
        )
        remote = (
            canonical_remote(_git(cwd, "remote", "get-url", selected))
            if selected
            else ""
        )
        repo_root = str(Path(common or root).resolve())
        if remote:
            digest = hashlib.sha256(remote.encode()).hexdigest()[:24]
            return ProjectIdentity(
                f"remote:{digest}", remote, repo_root, root, "remote"
            )
        digest = hashlib.sha256(f"{host_namespace}:{repo_root}".encode()).hexdigest()[
            :24
        ]
        return ProjectIdentity(f"local:{digest}", "", repo_root, root, "local")
    local = str(Path(cwd).resolve())
    digest = hashlib.sha256(f"{host_namespace}:{local}".encode()).hexdigest()[:24]
    return ProjectIdentity(
        f"local:{digest}", "", local, local, "unresolved", "not a git repository"
    )


def estimate_cost(
    model: str, usage: dict[str, int | None]
) -> tuple[float | None, dict]:
    """Return a list-price API-equivalent estimate and the immutable rate snapshot."""
    price = price_for(model)
    if price is None:
        return None, {}
    values = {
        key: int(usage.get(key) or 0)
        for key in ("input", "cache_write", "cache_read", "output")
    }
    rates = {
        "input": price.input,
        "cache_write": price.cache_write,
        "cache_read": price.cache_read,
        "output": price.output,
        "currency": "USD",
        "basis": "api_equivalent_list_price",
    }
    return sum(values[key] * rates[key] / 1_000_000 for key in values), rates


def transcript_storage():
    """Configured private history storage; ``default`` is the safe local fallback."""
    return storages[api_settings.AGENT_HISTORY_STORAGE]


def store_transcript(session, content: bytes, *, classification: str, source_key: str):
    """Store immutable transcript bytes and return the idempotent metadata row."""
    from django_ergo.conversation.models import AgentTranscriptArtifact

    digest = hashlib.sha256(content).hexdigest()
    filename = f"agent-history/{session.id}/{source_key}-{digest}.jsonl"
    row, created = AgentTranscriptArtifact.objects.get_or_create(
        session=session,
        source_key=source_key,
        classification=classification,
        defaults={
            "storage_key": filename,
            "format": "native-jsonl",
            "sha256": digest,
            "byte_count": len(content),
            "upload_state": "pending",
        },
    )
    if created:
        row.storage_key = transcript_storage().save(filename, ContentFile(content))
        row.upload_state = "verified"
        row.save(update_fields=["storage_key", "upload_state", "updated_at"])
    return row


def redact_transcript(content: bytes) -> bytes:
    """Versioned redaction seam for routine views; raw archives are never transformed."""
    text = content.decode("utf-8", errors="replace")
    text = re.sub(
        r"(?i)(api[_-]?key|token|password)(\s*[=:]\s*)\S+", r"\1\2[REDACTED]", text
    )
    return text.encode()


def _at(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def omp_session_rows(raw: bytes) -> tuple[dict, list[dict]]:
    """Read omp JSONL, locating its header after an optional mutable title slot."""
    rows: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return next((row for row in rows if row.get("type") == "session"), {}), rows


def ingest_omp_content(
    raw: bytes,
    *,
    source_name: str,
    worker=None,
    host_namespace: str = "local",
    profile_namespace: str = "default",
):
    """Idempotently ingest local or transported omp JSONL bytes."""
    from django_ergo.conversation.models import AgentRunSession
    from django_ergo.conversation.models import AgentSession
    from django_ergo.conversation.models import AgentSessionThreadLink
    from django_ergo.conversation.models import AgentUsageEvent
    from django_ergo.conversation.models import AgentWorkspaceObservation

    header, rows = omp_session_rows(raw)
    native_id = str(header.get("id") or source_name)
    cwd = str(header.get("cwd") or "")
    with transaction.atomic():
        session, _ = AgentSession.objects.get_or_create(
            host_namespace=host_namespace,
            cli="omp",
            profile_namespace=profile_namespace,
            native_session_id=native_id,
            defaults={"initial_cwd": cwd, "transcript_completeness": "complete"},
        )
        run_session = None
        if worker is not None:
            run_session, _ = AgentRunSession.objects.get_or_create(
                worker=worker,
                session=session,
                launch_key=str(worker.id),
                segment_key="native",
                defaults={"relation": "primary"},
            )
            AgentSessionThreadLink.objects.get_or_create(
                session=session,
                conversation=worker.session,
                defaults={
                    "worker": worker,
                    "owner": worker.session.user,
                    "title": worker.title,
                },
            )
        observation = None
        if cwd:
            identity = derive_project_identity(cwd, host_namespace)
            observation, _ = AgentWorkspaceObservation.objects.get_or_create(
                session=session,
                source_event_key="session-header",
                defaults={
                    "run_session": run_session,
                    "observed_cwd": cwd,
                    "repo_root": identity.repo_root,
                    "worktree_path": identity.worktree_path,
                    "canonical_remote": identity.canonical_remote,
                    "common_git_dir": identity.repo_root,
                    "project_key": identity.project_key,
                    "confidence": identity.confidence,
                    "resolution_error": identity.error,
                    "evidence_kind": "session_metadata",
                },
            )
        for index, row in enumerate(rows):
            message = row.get("message") or {}
            usage = message.get("usage") or {}
            if (
                row.get("type") != "message"
                or message.get("role") != "assistant"
                or not usage
            ):
                continue
            event_key = str(
                row.get("id") or message.get("id") or f"{source_name}:{index}"
            )
            counts = {
                "input": usage.get("input"),
                "cache_write": usage.get("cacheWrite"),
                "cache_read": usage.get("cacheRead"),
                "output": usage.get("output"),
                "reasoning": usage.get("reasoningTokens"),
            }
            estimate, snapshot = estimate_cost(str(message.get("model") or ""), counts)
            AgentUsageEvent.objects.update_or_create(
                session=session,
                source_event_key=event_key,
                defaults={
                    "native_request_id": str(message.get("requestId") or ""),
                    "occurred_at": _at(row.get("timestamp")),
                    "model": str(message.get("model") or ""),
                    "input_tokens": counts["input"],
                    "cache_write_tokens": counts["cache_write"],
                    "cache_read_tokens": counts["cache_read"],
                    "output_tokens": counts["output"],
                    "reasoning_tokens": counts["reasoning"],
                    "requests": 1,
                    "billing_mode": "subscription",
                    "estimated_usd": estimate,
                    "reported_usd": None,
                    "cost_status": "estimated" if estimate is not None else "missing",
                    "price_snapshot": snapshot,
                    "workspace_observation": observation,
                    "run_session": run_session,
                    "parser_version": "omp-jsonl-v1",
                },
            )
        store_transcript(
            session, raw, classification="raw", source_key=f"{source_name}:raw"
        )
        store_transcript(
            session,
            redact_transcript(raw),
            classification="redacted",
            source_key=f"{source_name}:redacted",
        )
    return session


def ingest_omp_file(
    path: Path,
    *,
    worker=None,
    host_namespace: str = "local",
    profile_namespace: str = "default",
):
    """Ingest one local omp file through the bytes collector."""
    return ingest_omp_content(
        path.read_bytes(),
        source_name=path.name,
        worker=worker,
        host_namespace=host_namespace,
        profile_namespace=profile_namespace,
    )
