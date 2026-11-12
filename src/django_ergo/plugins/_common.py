"""Helpers the official plugins share. Grep here before writing another."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

MAX_OUTPUT_CHARS = 20_000
TOKEN_FIELDS = ("input", "cache_write", "cache_read", "output", "reasoning")


def trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and the end of long output, where the useful parts are."""
    if len(text) <= limit:
        return text
    head = text[: limit // 4]
    tail = text[-(limit - len(head)) :]
    skipped = len(text) - len(head) - len(tail)
    return f"{head}\n[... {skipped} characters skipped ...]\n{tail}"


def clip(text: str, limit: int, *, one_line: bool = True) -> str:
    """``text`` cut to ``limit`` characters with an ellipsis; ``one_line``
    first collapses its whitespace."""
    text = " ".join(str(text).split()) if one_line else str(text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_secret(name: str) -> bool:
    """A file name that looks like a key or credentials, never shown or sent."""
    name = name.lower()
    return (
        name.startswith((".env", "id_rsa", "id_ed25519"))
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or "secret" in name
        or "credential" in name
    )


def fetch_file(  # noqa: PLR0913
    root: str,
    path: str,
    *,
    host: str,
    limit: int,
    timeout: int,
    scope: str = "worktree",
    secure: bool = False,
) -> tuple[str, bytes]:
    """Read a bounded file under a configured local or SSH root."""
    import posixpath
    import shlex
    import subprocess

    from django_ergo.protected_env import scrubbed_env

    target = posixpath.normpath(
        path if path.startswith("/") else posixpath.join(root, path)
    )
    if not target.startswith(root.rstrip("/") + "/"):
        msg = f"{path} is outside the {scope}"
        raise ValueError(msg)
    if is_secret(posixpath.basename(target)):
        msg = f"{posixpath.basename(target)} looks like a secret; it can't be attached"
        raise ValueError(msg)
    if secure:
        # Open each canonical path component without following symlinks. This
        # prevents a renamed symlink between containment validation and read.
        reader = """
import os, stat, sys
root = os.path.realpath(sys.argv[1], strict=True)
path = os.path.realpath(sys.argv[2], strict=True)
limit = int(sys.argv[3])
if os.path.commonpath((root, path)) != root or path == root:
    sys.exit("outside the configured artifact root")
name = os.path.basename(path).lower()
if (name.startswith((".env", "id_rsa", "id_ed25519")) or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or "secret" in name or "credential" in name):
    sys.exit("secret-looking file")
parts = path.strip("/").split("/")
fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
try:
    for part in parts[:-1]:
        new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = new
    file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(file_fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            sys.exit("not a file")
        sys.stdout.buffer.write(os.fsencode(path) + b"\\0")
        sys.stdout.buffer.write(f.read(limit + 1))
finally:
    os.close(fd)
"""
        script = " ".join(
            [
                "python3",
                "-c",
                shlex.quote(reader),
                shlex.quote(root),
                shlex.quote(target),
                str(limit),
            ]
        )
    else:
        script = (
            f"set -e; r=$(realpath -e -- {shlex.quote(root)}); f=$(realpath -e -- {shlex.quote(target)}); "
            f'case "$f" in "$r"/*) ;; *) echo "outside the {scope}" >&2; exit 3;; esac; '
            'test -f "$f" || { echo "not a file" >&2; exit 4; }; '
            f'printf "%s\\0" "$f"; head -c {limit + 1} -- "$f"'
        )
    argv = (
        ["ssh", "-o", "BatchMode=yes", host, script] if host else ["sh", "-c", script]
    )
    proc = subprocess.run(  # noqa: S603 — fixed script, quoted paths and configured host
        argv, capture_output=True, timeout=timeout, check=False, env=scrubbed_env()
    )
    if proc.returncode:
        msg = f"Couldn't read {path}: {proc.stderr.decode(errors='replace').strip()[:300]}"
        raise ValueError(msg)
    resolved, separator, data = proc.stdout.partition(b"\0")
    if not separator or len(data) > limit:
        msg = f"File is invalid or larger than {limit} bytes"
        raise ValueError(msg)
    resolved_path = resolved.decode(errors="strict")
    if is_secret(posixpath.basename(resolved_path)):
        msg = f"{path} points at a secret-looking file; it can't be attached"
        raise ValueError(msg)
    return resolved_path, data


def record_agent_usage(  # noqa: PLR0913
    worker,
    *,
    session,
    source: str,
    agent: str,
    models: dict[str, dict],
    first_at: datetime | None = None,
    last_at: datetime | None = None,
    replace: bool = False,
) -> None:
    """Store a coding agent's token totals per model on ``worker``.

    ``models`` maps a model to its totals (``input``, ``cache_write``,
    ``cache_read``, ``output``, ``reasoning``, ``requests``, and optionally its
    own ``first_at``/``last_at``). ``replace`` drops the worker's rows for
    models no longer listed.
    """
    from django_ergo.conversation.models import AgentUsage

    for model, usage in models.items():
        AgentUsage.objects.update_or_create(
            worker=worker,
            model=model,
            defaults={
                "session": session,
                "bot_name": worker.bot_name,
                "source": source,
                "agent": agent,
                **{
                    f"{field}_tokens": int(usage.get(field) or 0)
                    for field in TOKEN_FIELDS
                },
                "requests": int(usage.get("requests") or 0),
                "first_at": usage.get("first_at", first_at),
                "last_at": usage.get("last_at", last_at),
            },
        )
    if replace:
        AgentUsage.objects.filter(worker=worker).exclude(
            model__in=list(models)
        ).delete()
