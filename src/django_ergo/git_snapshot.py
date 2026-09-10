"""Safe access to one allowed committed Git snapshot."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from django.conf import settings

from django_ergo.paths import validate_relative_path


def git_environment():
    return {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }


class GitSnapshotError(RuntimeError):
    """Raised when a repository snapshot cannot be read safely."""


def _git(repository: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=text,
        env=git_environment(),
        timeout=30,
    )
    if result.returncode:
        stderr = (
            result.stderr.decode()
            if isinstance(result.stderr, bytes)
            else result.stderr
        )
        raise GitSnapshotError(stderr.strip() or "git command failed")
    return result.stdout


def repository_for(source) -> Path:
    repositories = getattr(settings, "DJANGO_ERGO", {}).get(
        "KNOWLEDGE_REPOSITORIES", {}
    )
    try:
        repository = Path(repositories[source.repository_alias]).resolve()
    except KeyError as exc:
        raise GitSnapshotError(
            "Unknown knowledge repository alias: %s" % source.repository_alias
        ) from exc
    if not (repository / ".git").exists():
        raise GitSnapshotError(
            "Configured repository is not a Git checkout: %s" % repository
        )
    return repository


class GitSnapshot:
    """An immutable, allowed Git commit for one knowledge source."""

    def __init__(self, source, commit: str | None = None):
        self.source = source
        self.repository = repository_for(source)
        self.commit = self._resolve_commit(commit)

    def _resolve_commit(self, requested: str | None) -> str:
        target = requested or self.source.allowed_ref
        commit = str(
            _git(
                self.repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                "%s^{commit}" % target,
            )
        ).strip()
        allowed = str(
            _git(
                self.repository,
                "rev-parse",
                "--verify",
                "--end-of-options",
                "%s^{commit}" % self.source.allowed_ref,
            )
        ).strip()
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "merge-base",
                "--is-ancestor",
                commit,
                allowed,
            ],
            check=False,
            capture_output=True,
            env=git_environment(),
            timeout=30,
        )
        if result.returncode:
            raise GitSnapshotError(
                "Commit %s is outside the allowed ref %s."
                % (commit, self.source.allowed_ref)
            )
        return commit

    def paths(self, prefix: str = "") -> list[str]:
        args = ["ls-tree", "-r", "-z", "--name-only", self.commit, "--"]
        if prefix:
            args.append(prefix)
        output = _git(self.repository, *args)
        return [path for path in str(output).split("\0") if path]

    def blob_oid(self, path: str) -> str:
        path = validate_relative_path(path)
        return str(
            _git(self.repository, "rev-parse", "%s:%s" % (self.commit, path))
        ).strip()

    def read_bytes(self, path: str) -> bytes:
        path = validate_relative_path(path)
        metadata = str(
            _git(self.repository, "ls-tree", self.commit, "--", path)
        ).split()
        if (
            not metadata
            or metadata[0] not in {"100644", "100755"}
            or metadata[1] != "blob"
        ):
            raise GitSnapshotError("Source must be a regular Git blob")
        if (
            int(str(_git(self.repository, "cat-file", "-s", metadata[2])))
            > 16 * 1024 * 1024
        ):
            raise GitSnapshotError("Source blob exceeds 16 MiB")
        return bytes(_git(self.repository, "cat-file", "blob", metadata[2], text=False))

    def read_text(self, path: str) -> str:
        try:
            return self.read_bytes(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitSnapshotError("%s: content is not UTF-8" % path) from exc

    def rename_map(self, old_commit: str | None) -> dict[str, str]:
        if not old_commit:
            return {}
        output = _git(
            self.repository,
            "diff",
            "--name-status",
            "-M",
            old_commit,
            self.commit,
        )
        renames = {}
        for line in str(output).splitlines():
            fields = line.split("\t")
            if fields and fields[0].startswith("R") and len(fields) == 3:
                renames[validate_relative_path(fields[2])] = validate_relative_path(
                    fields[1]
                )
        return renames
