"""Read a host-selected Git snapshot; YAML and paths are adapter concerns only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from pathlib import PurePosixPath

from .schema import MAX_CORPUS_BYTES
from .schema import MAX_DOCUMENTS
from .schema import CorpusError
from .schema import Snapshot
from .schema import require


def _relative(path):
    require(
        isinstance(path, str) and bool(path), "A repository-relative path is required"
    )
    parsed = PurePosixPath(path)
    require(
        not parsed.is_absolute()
        and not any(part in {"", ".", ".."} for part in path.split("/"))
        and "\\" not in path
        and "\x00" not in path,
        "Unsafe repository-relative path",
    )
    return path


class GitCorpus:
    """Map committed authored records and Markdown bodies to the logical schema."""

    def __init__(
        self, repository, *, ref, collection_id, scope, manifest="ergo-source.yaml"
    ):
        self.repository = Path(repository)
        self.ref = ref
        self.collection_id = collection_id
        self.scope = scope
        self.manifest = _relative(manifest)

    def _git(self, *arguments):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository), *arguments],
                capture_output=True,
                check=False,
                timeout=30,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("GIT_")
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = "Git is unavailable or timed out"
            raise CorpusError(message) from exc
        if result.returncode:
            message = "Cannot read configured Git corpus"
            raise CorpusError(message)
        return result.stdout

    def _commit(self, ref):
        return (
            self._git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            .decode()
            .strip()
        )

    def _read(self, commit, path):
        path = _relative(path)
        metadata = self._git("ls-tree", commit, "--", path).split()
        require(
            metadata and metadata[0] == b"100644" and metadata[1] == b"blob",
            "Corpus content must be a regular non-executable file",
        )
        object_id = metadata[2].decode()
        size = int(self._git("cat-file", "-s", object_id))
        require(size <= MAX_CORPUS_BYTES, "Git content exceeds size limit")
        return self._git("cat-file", "blob", object_id).decode("utf-8")

    def load(self):
        try:
            import yaml
        except ImportError as exc:
            message = "GitCorpus requires the django-ergo[filesystem] extra"
            raise CorpusError(message) from exc

        commit = self._commit(self.ref)
        try:
            payload = yaml.safe_load(self._read(commit, self.manifest))
            require(
                isinstance(payload, dict)
                and isinstance(payload.get("documents"), list),
                "Invalid authored corpus",
            )
            require(
                len(payload["documents"]) <= MAX_DOCUMENTS,
                "Too many document revisions",
            )
            captured_bytes = 0
            for record in payload["documents"]:
                require(isinstance(record, dict), "Document must be an object")
                if "content_path" in record:
                    require(
                        "content" not in record,
                        "Content and content_path cannot compete",
                    )
                    source_commit = self._commit(record.pop("content_commit", commit))
                    self._git("merge-base", "--is-ancestor", source_commit, commit)
                    record["content"] = self._read(
                        source_commit, record.pop("content_path")
                    )
                    captured_bytes += len(record["content"].encode("utf-8"))
                    require(
                        captured_bytes <= MAX_CORPUS_BYTES, "Corpus exceeds size limit"
                    )
            return Snapshot.from_dict(payload)
        except (yaml.YAMLError, UnicodeError, ValueError) as exc:
            message = "Invalid Git corpus"
            raise CorpusError(message) from exc
