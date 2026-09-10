"""Build and validate portable filesystem knowledge bases from Git repositories."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import typing
import uuid
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from django_ergo.filesystem_support import yaml

FORMAT_VERSION = "django-ergo-filesystem-kb/v1"
_CITED_SOURCE_FORMAT = "django-ergo-cited-source-snapshot/v2"
_KB_NAMESPACE = uuid.UUID("149dd65c-dd0b-4e24-8731-f0d58e4b7cf3")
_GIT_BINARY = shutil.which("git")
_GIT_HISTORY_FIELDS = 7
_MAX_SESSION_CANDIDATES = 200
_MAX_TEXT_BYTES = 2_000_000
_MAX_CAPTURED_SOURCE_BYTES = 20_000_000
_PACKAGE_COMPONENT_DEPTH = 2
_SRC_COMPONENT_DEPTH = 3
_LANGUAGE_BY_SUFFIX = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".css": "CSS",
    ".go": "Go",
    ".h": "C/C++ header",
    ".hpp": "C++ header",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".md": "Markdown",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".sql": "SQL",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".txt": "Text",
    ".yaml": "YAML",
    ".yml": "YAML",
}
_DECISION_RE = re.compile(
    r"\b(add|adopt|architect|change|choose|chose|decid|design|drop|fix|migrat|"
    r"move|refactor|reject|remov|replac|switch)\w*\b",
    re.IGNORECASE,
)
_SESSION_DECISION_RE = re.compile(
    r"\b(because|choose|chose|decision|decided|must|rejected|should|trade-?off)\b",
    re.IGNORECASE,
)
_SOURCE_CITATION_FIELDS = (
    "unit_id",
    "commit",
    "path",
    "symbol",
    "role",
    "content_hash",
)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class FilesystemKBError(RuntimeError):
    """Raised when a filesystem KB cannot be built or validated."""


def _fail(message: str) -> typing.NoReturn:
    error = FilesystemKBError(message)
    raise error


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    blob_oid: str
    size: int
    language: str


@dataclass(frozen=True)
class Component:
    name: str
    files: int
    python_modules: int
    classes: int
    functions: int
    dependencies: tuple[str, ...]


class RepositoryCorpus:
    """Read one immutable commit from a local Git checkout."""

    def __init__(self, repository: Path | str, commit: str = "HEAD"):
        requested = Path(repository).resolve()
        try:
            top_level = self._run(requested, "rev-parse", "--show-toplevel")
        except FilesystemKBError as exc:
            error = FilesystemKBError(f"Not a Git checkout: {requested}")
            raise error from exc
        self.repository = Path(top_level.strip()).resolve()
        self.commit = self._run(
            self.repository,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{commit}^{{commit}}",
        ).strip()

    @staticmethod
    def _run(repository: Path, *args: str, text: bool = True) -> str | bytes:
        if not _GIT_BINARY:
            _fail("git executable is not available")
        result = subprocess.run(  # noqa: S603 - argv is passed without a shell.
            [_GIT_BINARY, "-C", str(repository), *args],
            check=False,
            capture_output=True,
            text=text,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            timeout=30,
        )
        if result.returncode:
            stderr = (
                result.stderr.decode(errors="replace")
                if isinstance(result.stderr, bytes)
                else result.stderr
            )
            _fail(stderr.strip() or "git command failed")
        return result.stdout

    def files(self) -> list[RepositoryFile]:
        output = self._run(
            self.repository,
            "ls-tree",
            "-r",
            "-z",
            "--long",
            self.commit,
        )
        files = []
        for record in str(output).split("\0"):
            if not record:
                continue
            metadata, path = record.split("\t", 1)
            _mode, object_type, oid, size = metadata.split()
            if object_type != "blob":
                continue
            suffix = PurePosixPath(path).suffix.lower()
            files.append(
                RepositoryFile(
                    path=path,
                    blob_oid=oid,
                    size=int(size),
                    language=_LANGUAGE_BY_SUFFIX.get(suffix, "Other"),
                )
            )
        return sorted(files, key=lambda item: item.path)

    def read_bytes(self, path: str) -> bytes:
        return bytes(
            self._run(
                self.repository,
                "show",
                f"{self.commit}:{path}",
                text=False,
            )
        )

    def read_blob(self, commit: str, path: str) -> tuple[str, bytes]:
        """Read one path and its blob identity from a resolved commit."""
        blob_oid = str(
            self._run(
                self.repository,
                "rev-parse",
                f"{commit}:{path}",
            )
        ).strip()
        content = bytes(
            self._run(
                self.repository,
                "show",
                f"{commit}:{path}",
                text=False,
            )
        )
        return blob_oid, content

    def read_text(self, path: str) -> str | None:
        try:
            return self.read_bytes(path).decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _history_entry(self, fields: list[str]) -> dict:
        if len(fields) != _GIT_HISTORY_FIELDS:
            _fail("Could not parse Git history record")
        oid, parents, authored_at, author_name, author_email, subject, body = fields
        changed = str(
            self._run(
                self.repository,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-M",
                oid,
            )
        ).splitlines()
        return {
            "oid": oid,
            "parents": parents.split(),
            "authored_at": authored_at,
            "author": {"name": author_name, "email": author_email},
            "subject": subject,
            "body": body.strip(),
            "changes": changed,
        }

    def history(
        self,
        limit: int,
        *,
        include: typing.Iterable[str] = (),
    ) -> list[dict]:
        if limit < 1:
            _fail("history_limit must be at least 1")
        format_arg = "--pretty=format:%H%x1f%P%x1f%aI%x1f%an%x1f%ae%x1f%s%x1f%b%x1e"
        output = str(
            self._run(
                self.repository,
                "log",
                f"--max-count={limit}",
                "--date=iso-strict",
                format_arg,
                self.commit,
            )
        )
        commits = [
            self._history_entry(record.split("\x1f", 6))
            for raw_record in output.split("\x1e")
            if (record := raw_record.strip("\n"))
        ]
        known = {commit["oid"] for commit in commits}
        for requested in sorted(set(include)):
            oid = str(
                self._run(
                    self.repository,
                    "rev-parse",
                    f"{requested}^{{commit}}",
                )
            ).strip()
            if oid in known:
                continue
            try:
                self._run(
                    self.repository,
                    "merge-base",
                    "--is-ancestor",
                    oid,
                    self.commit,
                )
            except FilesystemKBError as exc:
                error = FilesystemKBError(
                    f"Cited commit is not an ancestor of the KB snapshot: {oid}"
                )
                raise error from exc
            record = str(
                self._run(
                    self.repository,
                    "show",
                    "-s",
                    "--date=iso-strict",
                    format_arg,
                    oid,
                )
            ).strip("\n\x1e")
            commits.append(self._history_entry(record.split("\x1f", 6)))
            known.add(oid)
        return commits


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _component_name(path: str) -> str:
    parts = PurePosixPath(path).parts
    if not parts:
        return "root"
    if parts[0] in {"src", "lib"} and len(parts) >= _SRC_COMPONENT_DEPTH:
        package = parts[1]
        child = parts[2]
        if "." not in child and child not in {"__pycache__"}:
            return f"{package}.{child}"
        return f"{package}.core"
    if (
        parts[0] in {"app", "apps", "packages"}
        and len(parts) >= _PACKAGE_COMPONENT_DEPTH
    ):
        return ".".join(parts[:_PACKAGE_COMPONENT_DEPTH])
    if len(parts) == 1:
        return "root"
    return parts[0]


def _python_module_name(path: str) -> str | None:
    parts = list(PurePosixPath(path).parts)
    if not parts or PurePosixPath(path).suffix != ".py":
        return None
    if parts[0] in {"src", "lib"}:
        parts = parts[1:]
    parts[-1] = PurePosixPath(parts[-1]).stem
    if parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom) or not node.module or node.level:
        return []
    return [
        node.module if alias.name == "*" else f"{node.module}.{alias.name}"
        for alias in node.names
    ]


def _resolve_local_component(
    module: str,
    module_components: dict[str, str],
    component_names: set[str],
) -> str | None:
    candidates = [
        (local_module, component)
        for local_module, component in module_components.items()
        if module == local_module or module.startswith(f"{local_module}.")
    ]
    if candidates:
        return max(candidates, key=lambda item: len(item[0]))[1]
    root_component = f"{module.split('.', 1)[0]}.core"
    return root_component if root_component in component_names else None


def _analyze_components(
    corpus: RepositoryCorpus, files: typing.Iterable[RepositoryFile]
):
    files = list(files)
    counters = defaultdict(Counter)
    dependencies = defaultdict(set)
    entrypoints = []
    parse_failures = []
    module_components = {
        module: _component_name(item.path)
        for item in files
        if (module := _python_module_name(item.path))
    }
    component_names = {_component_name(item.path) for item in files}
    for item in files:
        component = _component_name(item.path)
        counters[component]["files"] += 1
        path = PurePosixPath(item.path)
        if path.name in {"manage.py", "__main__.py", "cli.py"} or (
            "management" in path.parts
            and "commands" in path.parts
            and path.name != "__init__.py"
        ):
            entrypoints.append(item.path)
        if item.language != "Python" or item.size > _MAX_TEXT_BYTES:
            continue
        text = corpus.read_text(item.path)
        if text is None:
            parse_failures.append(item.path)
            continue
        try:
            tree = ast.parse(text, filename=item.path)
        except SyntaxError:
            parse_failures.append(item.path)
            continue
        counters[component]["python_modules"] += 1
        counters[component]["classes"] += sum(
            isinstance(node, ast.ClassDef) for node in ast.walk(tree)
        )
        counters[component]["functions"] += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(tree)
        )
        for node in ast.walk(tree):
            for module in _imported_modules(node):
                target = _resolve_local_component(
                    module, module_components, component_names
                )
                if target and target != component:
                    dependencies[component].add(target)
    components = [
        Component(
            name=name,
            files=values["files"],
            python_modules=values["python_modules"],
            classes=values["classes"],
            functions=values["functions"],
            dependencies=tuple(sorted(dependencies[name])),
        )
        for name, values in sorted(counters.items())
    ]
    return components, sorted(entrypoints), sorted(parse_failures)


def _frontmatter(project: str, path: str, title: str, page_type: str, sources):
    return {
        "id": str(uuid.uuid5(_KB_NAMESPACE, f"{project}:{path}")),
        "title": title,
        "type": page_type,
        "status": "current",
        "project": project,
        "sources": list(sources),
    }


def _render_page(frontmatter: dict, body: str) -> str:
    encoded = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    return f"---\n{encoded}\n---\n\n{body.rstrip()}\n"


def _write_page(root: Path, path: str, frontmatter: dict, body: str) -> None:
    target = root / "wiki" / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_page(frontmatter, body), encoding="utf-8")


def _render_system_overview(  # noqa: PLR0913 - report inputs are independent.
    project: str,
    corpus: RepositoryCorpus,
    files: list[RepositoryFile],
    components: list[Component],
    entrypoints: list[str],
    parse_failures: list[str],
) -> str:
    languages = Counter(item.language for item in files)
    lines = [
        f"# {project} system overview",
        "",
        "## Snapshot",
        "",
        f"This analysis describes Git commit `{corpus.commit}`. It covers "
        f"{len(files)} tracked files across {len(components)} inferred components.",
        "",
        "| Language | Files |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {language} | {count} |" for language, count in languages.most_common()
    )
    lines.extend(
        [
            "",
            "## Components",
            "",
            "Components are inferred from repository paths. Counts come from the committed tree; Python symbol counts come from AST parsing.",
            "",
            "| Component | Files | Python modules | Classes | Functions |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| `{component.name}` | {component.files} | {component.python_modules} | "
        f"{component.classes} | {component.functions} |"
        for component in components
    )
    lines.extend(["", "## Entrypoints", ""])
    if entrypoints:
        lines.extend(f"- `{path}`" for path in entrypoints)
    else:
        lines.append("- No conventional entrypoints detected.")
    lines.extend(["", "## Analysis limits", ""])
    lines.append(
        "This page reports structural facts; it does not infer runtime behavior or intent from names alone."
    )
    if parse_failures:
        lines.append(
            f"Python AST parsing failed or was skipped for {len(parse_failures)} file(s); see the raw repository manifest."
        )
    return "\n".join(lines)


def _render_architecture(components: list[Component]) -> str:
    lines = [
        "# Component architecture",
        "",
        "## Dependency view",
        "",
        "Internal Python dependencies are statically inferred from import statements between tracked modules.",
        "",
        "| Component | Depends on |",
        "| --- | --- |",
    ]
    for component in components:
        rendered = ", ".join(f"`{item}`" for item in component.dependencies) or "—"
        lines.append(f"| `{component.name}` | {rendered} |")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- Directory boundaries are evidence, not proof of deployment or ownership boundaries.",
            "- Dynamic imports, reflection, templates, configuration, and non-Python calls are not represented.",
            "- Use this map to choose evidence to inspect; do not treat it as a generated design decision.",
        ]
    )
    return "\n".join(lines)


def _render_history(history: list[dict], documented_decisions: list[str]) -> str:
    candidates = [
        commit
        for commit in history
        if _DECISION_RE.search(f"{commit['subject']}\n{commit['body']}")
    ]
    lines = [
        "# Repository decision history",
        "",
        "## Documented decision sources",
        "",
    ]
    if documented_decisions:
        lines.extend(f"- `{path}`" for path in documented_decisions)
    else:
        lines.append("- No decision- or architecture-named documents detected.")
    lines.extend(
        [
            "",
            "## Decision-bearing commit candidates",
            "",
            "These commits are selected by subject/body keywords. They are historical evidence candidates, not inferred rationale.",
            "",
        ]
    )
    if not candidates:
        lines.append("- No candidate commits in the captured history window.")
    for commit in candidates:
        short = commit["oid"][:12]
        changed = len(commit["changes"])
        lines.append(
            f"- `{short}` ({commit['authored_at']}): {commit['subject']} ({changed} changed paths)"
        )
        if commit["body"]:
            first = " ".join(commit["body"].splitlines()).strip()
            lines.append(f"  - Commit body: {first}")
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "A commit records what changed, not necessarily why. Validate rationale against cited documents, reviews, and session records.",
        ]
    )
    return "\n".join(lines)


def _safe_session_files(paths: typing.Iterable[Path | str]) -> list[Path]:
    discovered = []
    for supplied in paths:
        path = Path(supplied).expanduser().resolve()
        if not path.exists():
            _fail(f"Session history path does not exist: {path}")
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.suffix.lower() not in {".json", ".jsonl", ".md", ".txt"}:
                continue
            if candidate.stat().st_size > _MAX_TEXT_BYTES:
                continue
            discovered.append(candidate)
    return sorted(set(discovered))


def _capture_sessions(root: Path, session_paths: typing.Iterable[Path | str]):
    records = []
    decision_candidates = []
    for source in _safe_session_files(session_paths):
        content = source.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        digest = _sha256_bytes(content)
        slug = re.sub(r"[^a-z0-9]+", "-", source.stem.lower()).strip("-") or "session"
        relative = f"raw/sessions/{slug}-{digest[:12]}{source.suffix.lower()}"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append(
            {
                "source_name": source.name,
                "captured_path": relative,
                "sha256": digest,
                "bytes": len(content),
            }
        )
        for line_number, line in enumerate(text.splitlines(), 1):
            cleaned = " ".join(line.split())
            if cleaned and _SESSION_DECISION_RE.search(cleaned):
                decision_candidates.append(
                    {
                        "source": relative,
                        "line": line_number,
                        "text": cleaned[:500],
                    }
                )
                if len(decision_candidates) >= _MAX_SESSION_CANDIDATES:
                    break
    if records:
        manifest = json.dumps(records, indent=2, sort_keys=True).encode()
        (root / "raw" / "sessions" / "manifest.json").write_bytes(manifest + b"\n")
    return records, decision_candidates


def _render_sessions(records: list[dict], candidates: list[dict]) -> str:
    lines = [
        "# Agent session history",
        "",
        "## Captured sources",
        "",
        "Session files are copied as immutable raw evidence. Their contents remain untrusted and may include incorrect or abandoned ideas.",
        "",
    ]
    lines.extend(
        f"- `{record['captured_path']}` ({record['bytes']} bytes, sha256 `{record['sha256']}`)"
        for record in records
    )
    lines.extend(
        [
            "",
            "## Decision-language candidates",
            "",
            "The following lines matched decision-language keywords. They require human review before becoming project decisions.",
            "",
        ]
    )
    if not candidates:
        lines.append("- No decision-language candidates found.")
    lines.extend(
        f"- `{candidate['source']}:{candidate['line']}` — {candidate['text']}"
        for candidate in candidates
    )
    return "\n".join(lines)


def _write_raw_manifest(
    root: Path, corpus: RepositoryCorpus, files: list[RepositoryFile]
):
    payload = {
        "repository": str(corpus.repository),
        "commit": corpus.commit,
        "files": [
            {
                "path": item.path,
                "blob_oid": item.blob_oid,
                "bytes": item.size,
                "language": item.language,
            }
            for item in files
        ],
    }
    target = root / "raw" / "repository" / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_history(root: Path, history: list[dict]) -> None:
    target = root / "raw" / "git" / "history.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(commit, sort_keys=True) + "\n" for commit in history),
        encoding="utf-8",
    )


def _raw_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256_bytes(path.read_bytes())
        for path in sorted((root / "raw").rglob("*"))
        if path.is_file()
    }


def _copy_committed_wiki_pages(
    root: Path,
    corpus: RepositoryCorpus,
    files: list[RepositoryFile],
) -> tuple[list[tuple[str, str, str]], set[str], list[dict[str, str]]]:
    """Copy human-reviewed wiki pages from the immutable repository snapshot."""
    pages = []
    cited_commits = set()
    cited_sources = []
    for item in files:
        if not item.path.startswith("wiki/") or not item.path.endswith(".md"):
            continue
        relative = _safe_relative_reference(
            item.path.removeprefix("wiki/"),
            field="committed wiki page",
        )
        if relative == "index.md":
            continue
        content = corpus.read_text(item.path)
        if content is None:
            _fail(f"Committed wiki page is not UTF-8: {item.path}")
        target = root / "wiki" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        metadata, _body = _parse_page(target)
        title = str(metadata.get("title", "")).strip()
        page_type = str(metadata.get("type", "")).strip()
        if not title or not page_type:
            _fail(f"{item.path}: title and type are required")
        sources = metadata.get("sources", [])
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict) or not source.get("commit"):
                    continue
                commit = str(source["commit"])
                cited_commits.add(commit)
                if source.get("kind") == "source-unit":
                    cited_sources.append(
                        {
                            "page": f"wiki/{relative}",
                            **{
                                field: str(source.get(field, ""))
                                for field in _SOURCE_CITATION_FIELDS
                            },
                        }
                    )
        pages.append((relative, title, page_type))
    return pages, cited_commits, cited_sources


def _capture_cited_sources(
    root: Path,
    corpus: RepositoryCorpus,
    history: list[dict],
    cited_sources: list[dict[str, str]],
) -> None:
    """Materialize the bounded source files supporting structured citations."""
    if not cited_sources:
        return
    captured_commits = {str(item["oid"]) for item in history}
    captured_bytes = 0
    entries = []
    cited_files = sorted(
        {(source["commit"], source["path"]) for source in cited_sources}
    )
    for commit, path in cited_files:
        if commit not in captured_commits:
            _fail(f"Cited source commit is not captured: {commit}")
        source_path = _safe_relative_reference(path, field="cited source path")
        blob_oid, content = corpus.read_blob(commit, source_path)
        if len(content) > _MAX_TEXT_BYTES:
            _fail(
                f"Cited source exceeds {_MAX_TEXT_BYTES} bytes: {commit}:{source_path}"
            )
        captured_bytes += len(content)
        if captured_bytes > _MAX_CAPTURED_SOURCE_BYTES:
            _fail(f"Cited source snapshot exceeds {_MAX_CAPTURED_SOURCE_BYTES} bytes")
        captured_path = (
            PurePosixPath("raw") / "repository" / "citations" / commit / source_path
        ).as_posix()
        target = root / captured_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entries.append(
            {
                "commit": commit,
                "path": source_path,
                "captured_path": captured_path,
                "blob_oid": blob_oid,
                "sha256": _sha256_bytes(content),
                "bytes": len(content),
            }
        )
    captured_paths = {
        (entry["commit"], entry["path"]): entry["captured_path"] for entry in entries
    }
    citations = []
    for source in sorted(
        cited_sources,
        key=lambda item: tuple(
            item[field] for field in ("page", *_SOURCE_CITATION_FIELDS)
        ),
    ):
        citation = dict(source)
        citation["captured_path"] = captured_paths[(source["commit"], source["path"])]
        citations.append(citation)
    target = root / "raw" / "repository" / "citations" / "manifest.json"
    target.write_text(
        json.dumps(
            {
                "format": _CITED_SOURCE_FORMAT,
                "files": entries,
                "citations": citations,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _capture_raw_evidence(
    root: Path,
    corpus: RepositoryCorpus,
    files: list[RepositoryFile],
    history: list[dict],
    cited_sources: list[dict[str, str]],
) -> None:
    _write_raw_manifest(root, corpus, files)
    _write_history(root, history)
    _capture_cited_sources(root, corpus, history, cited_sources)


def build_repository_kb(  # noqa: PLR0913 - options map directly to the CLI.
    repository: Path | str,
    output: Path | str,
    *,
    commit: str = "HEAD",
    project: str | None = None,
    name: str | None = None,
    history_limit: int = 100,
    session_paths: typing.Iterable[Path | str] = (),
) -> dict:
    """Generate a deterministic, reviewable filesystem KB from one Git commit."""
    corpus = RepositoryCorpus(repository, commit)
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        _fail("Output directory must be empty or absent")
    project = project or corpus.repository.name
    name = name or f"{project} repository knowledge base"
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=parent))
    try:
        files = corpus.files()
        pages, cited_commits, cited_sources = _copy_committed_wiki_pages(
            staging, corpus, files
        )
        history = corpus.history(history_limit, include=cited_commits)
        components, entrypoints, parse_failures = _analyze_components(corpus, files)
        documented_decisions = [
            item.path
            for item in files
            if item.language == "Markdown"
            and re.search(
                r"(^|/)(adr|architecture|decision|design|plan|spec)", item.path, re.I
            )
        ]
        _capture_raw_evidence(staging, corpus, files, history, cited_sources)
        session_records, session_candidates = _capture_sessions(staging, session_paths)

        default_pages = [
            ("project/system-overview.md", "System overview", "project"),
            ("architecture/components.md", "Component architecture", "architecture"),
            (
                "decisions/repository-history.md",
                "Repository decision history",
                "decision",
            ),
        ]
        committed_paths = {path for path, _title, _page_type in pages}
        if default_pages[0][0] not in committed_paths:
            pages.append(default_pages[0])
            _write_page(
                staging,
                default_pages[0][0],
                _frontmatter(
                    project,
                    default_pages[0][0],
                    default_pages[0][1],
                    default_pages[0][2],
                    ["raw/repository/manifest.json"],
                ),
                _render_system_overview(
                    project, corpus, files, components, entrypoints, parse_failures
                ),
            )
        if default_pages[1][0] not in committed_paths:
            pages.append(default_pages[1])
            _write_page(
                staging,
                default_pages[1][0],
                _frontmatter(
                    project,
                    default_pages[1][0],
                    default_pages[1][1],
                    default_pages[1][2],
                    ["raw/repository/manifest.json"],
                ),
                _render_architecture(components),
            )
        if default_pages[2][0] not in committed_paths:
            pages.append(default_pages[2])
            _write_page(
                staging,
                default_pages[2][0],
                _frontmatter(
                    project,
                    default_pages[2][0],
                    default_pages[2][1],
                    default_pages[2][2],
                    ["raw/repository/manifest.json", "raw/git/history.jsonl"],
                ),
                _render_history(history, documented_decisions),
            )
        if session_records:
            session_path = "decisions/session-history.md"
            if session_path not in committed_paths:
                pages.append((session_path, "Agent session history", "decision"))
                session_sources = ["raw/sessions/manifest.json"] + [
                    record["captured_path"] for record in session_records
                ]
                _write_page(
                    staging,
                    session_path,
                    _frontmatter(
                        project,
                        session_path,
                        "Agent session history",
                        "decision",
                        session_sources,
                    ),
                    _render_sessions(session_records, session_candidates),
                )

        pages.sort()
        index_body = [
            f"# {name}",
            "",
            f"Compiled from repository commit `{corpus.commit}`.",
            "",
            "## Pages",
            "",
        ]
        index_body.extend(f"- [[{path}|{title}]]" for path, title, _page_type in pages)
        index_sources = [
            "raw/repository/manifest.json",
            "raw/git/history.jsonl",
            *(["raw/repository/citations/manifest.json"] if cited_sources else []),
        ]
        _write_page(
            staging,
            "index.md",
            _frontmatter(
                project,
                "index.md",
                name,
                "index",
                index_sources,
            ),
            "\n".join(index_body),
        )

        manifest = {
            "format": FORMAT_VERSION,
            "name": name,
            "project": project,
            "source": {
                "kind": "git",
                "repository": str(corpus.repository),
                "commit": corpus.commit,
            },
            "layout": {"raw": "raw", "wiki": "wiki", "index": "wiki/index.md"},
            "raw_files": _raw_hashes(staging),
        }
        (staging / "kb.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        validate_filesystem_kb(staging)
        if output.exists():
            output.rmdir()
        staging.rename(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "path": str(output),
        "commit": corpus.commit,
        "files": len(files),
        "components": len(components),
        "commits": len(history),
        "sessions": len(session_records),
        "pages": len(pages) + 1,
    }


def _parse_page(path: Path) -> tuple[dict, str]:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        _fail(f"{path}: frontmatter must start with ---")
    try:
        marker = content.index("\n---\n", 4)
        metadata = yaml.safe_load(content[4:marker]) or {}
    except (ValueError, yaml.YAMLError) as exc:
        error = FilesystemKBError(f"{path}: invalid frontmatter")
        raise error from exc
    if not isinstance(metadata, dict):
        _fail(f"{path}: frontmatter must be a mapping")
    return metadata, content[marker + 5 :]


def _safe_relative_reference(reference: str, *, field: str) -> str:
    path = reference.split("#", 1)[0]
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        _fail(f"Unsafe {field} reference: {reference}")
    return path


def _validate_raw_layer(root: Path) -> dict[str, str]:
    manifest_path = root / "kb.yaml"
    if not manifest_path.is_file():
        _fail("kb.yaml is missing")
    if any(path.is_symlink() for path in root.rglob("*")):
        _fail("Filesystem KB cannot contain symlinks")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        error = FilesystemKBError("kb.yaml is invalid YAML")
        raise error from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        _fail(f"kb.yaml format must be {FORMAT_VERSION}")
    missing_directories = [
        directory.name
        for directory in (root / "raw", root / "wiki")
        if not directory.is_dir()
    ]
    if missing_directories:
        _fail(f"Required directories are missing: {missing_directories}")
    raw_hashes = manifest.get("raw_files")
    if not isinstance(raw_hashes, dict) or not raw_hashes:
        _fail("kb.yaml raw_files must be a non-empty mapping")
    actual_raw = {
        path.relative_to(root).as_posix(): _sha256_bytes(path.read_bytes())
        for path in sorted((root / "raw").rglob("*"))
        if path.is_file()
    }
    if set(actual_raw) != set(raw_hashes):
        missing = sorted(set(raw_hashes) - set(actual_raw))
        extra = sorted(set(actual_raw) - set(raw_hashes))
        _fail(f"Raw file inventory mismatch: missing={missing} extra={extra}")
    for path, digest in raw_hashes.items():
        _safe_relative_reference(str(path), field="raw file")
        if not str(path).startswith("raw/") or actual_raw[str(path)] != digest:
            _fail(f"Raw file checksum mismatch: {path}")
    return actual_raw


def _validate_citation_source_entry(
    root: Path,
    entry,
    paths: set[str],
    commits: set[str],
) -> tuple[tuple[str, str], str]:
    if not isinstance(entry, dict):
        _fail("Cited source snapshot entry must be a mapping")
    required = {"commit", "path", "captured_path", "blob_oid", "sha256", "bytes"}
    missing = sorted(required - set(entry))
    if missing:
        _fail(f"Cited source snapshot entry missing fields {missing}")
    commit = str(entry["commit"])
    source_path = _safe_relative_reference(
        str(entry["path"]), field="cited source path"
    )
    captured_path = _safe_relative_reference(
        str(entry["captured_path"]), field="captured source path"
    )
    expected_path = (
        PurePosixPath("raw") / "repository" / "citations" / commit / source_path
    ).as_posix()
    if captured_path != expected_path:
        _fail(f"Cited source snapshot path mismatch: {captured_path}")
    if commit not in commits:
        _fail(f"Cited source snapshot is outside repository evidence: {commit}")
    if (
        not isinstance(entry["bytes"], int)
        or isinstance(entry["bytes"], bool)
        or not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"]))
    ):
        _fail(f"Cited source snapshot has invalid size or hash: {captured_path}")
    content = (root / captured_path).read_bytes()
    if len(content) != entry["bytes"] or _sha256_bytes(content) != entry["sha256"]:
        _fail(f"Cited source snapshot checksum mismatch: {captured_path}")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(entry["blob_oid"])):
        _fail(f"Cited source snapshot has invalid blob_oid: {captured_path}")
    algorithm = "sha1" if len(entry["blob_oid"]) == 40 else "sha256"
    blob = f"blob {len(content)}\0".encode() + content
    if (
        hashlib.new(algorithm, blob, usedforsecurity=False).hexdigest()
        != entry["blob_oid"]
    ):
        _fail(f"Cited source snapshot blob mismatch: {captured_path}")
    return (commit, source_path), captured_path


def _validate_unit_citation_entry(
    citation,
    source_files: dict[tuple[str, str], str],
) -> tuple[str, ...]:
    if not isinstance(citation, dict):
        _fail("Source-unit citation snapshot entry must be a mapping")
    required = {"page", "captured_path", *_SOURCE_CITATION_FIELDS}
    missing = sorted(required - set(citation))
    if missing:
        _fail(f"Source-unit citation snapshot entry missing fields {missing}")
    page = _safe_relative_reference(str(citation["page"]), field="citing page")
    if not page.startswith("wiki/") or not page.endswith(".md"):
        _fail(f"Source-unit citation has invalid page: {page}")
    unit_id = str(citation["unit_id"])
    try:
        uuid.UUID(unit_id)
    except ValueError as exc:
        error = FilesystemKBError("Source-unit citation has invalid unit_id")
        raise error from exc
    commit = str(citation["commit"])
    source_path = _safe_relative_reference(
        str(citation["path"]), field="cited source path"
    )
    symbol = str(citation["symbol"])
    role = str(citation["role"])
    content_hash = str(citation["content_hash"])
    if (
        not symbol.strip()
        or not role.strip()
        or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
    ):
        _fail(f"Source-unit citation has invalid provenance: {unit_id}")
    captured_path = _safe_relative_reference(
        str(citation["captured_path"]), field="captured source path"
    )
    if source_files.get((commit, source_path)) != captured_path:
        _fail(f"Source-unit citation has no captured file: {unit_id}")
    return (page, unit_id, commit, source_path, symbol, role, content_hash)


def _citation_source_evidence(
    root: Path,
    paths: set[str],
    commits: set[str],
) -> tuple[dict[tuple[str, str], str], Counter]:
    manifest_path = root / "raw" / "repository" / "citations" / "manifest.json"
    if not manifest_path.is_file():
        return {}, Counter()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["files"]
        citations = manifest["citations"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        error = FilesystemKBError("Cited source snapshot has an invalid schema")
        raise error from exc
    if (
        manifest.get("format") != _CITED_SOURCE_FORMAT
        or not isinstance(entries, list)
        or not isinstance(citations, list)
    ):
        _fail("Cited source snapshot has an invalid schema")
    captured_paths = set()
    source_files = {}
    for entry in entries:
        key, captured_path = _validate_citation_source_entry(
            root, entry, paths, commits
        )
        if key in source_files or captured_path in captured_paths:
            commit, source_path = key
            _fail(f"Cited source snapshot has duplicate entry: {commit}:{source_path}")
        source_files[key] = captured_path
        captured_paths.add(captured_path)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in manifest_path.parent.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_paths != captured_paths:
        _fail("Cited source snapshot file inventory mismatch")
    source_citations = Counter(
        _validate_unit_citation_entry(citation, source_files) for citation in citations
    )
    return source_files, source_citations


def _repository_evidence(root: Path) -> dict[str, typing.Any]:
    try:
        manifest = json.loads(
            (root / "raw" / "repository" / "manifest.json").read_text(encoding="utf-8")
        )
        history = [
            json.loads(line)
            for line in (root / "raw" / "git" / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        paths = {str(item["path"]) for item in manifest["files"]}
        commits = {str(manifest["commit"])}
        commits.update(str(item["oid"]) for item in history)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        error = FilesystemKBError("Raw repository evidence has an invalid schema")
        raise error from exc
    source_files, source_citations = _citation_source_evidence(root, paths, commits)
    return {
        "paths": paths,
        "commits": commits,
        "source_files": source_files,
        "source_citations": source_citations,
    }


def _consume_source_citation(
    relative: str,
    commit: str,
    source_path: str,
    source: dict,
    evidence: dict[str, typing.Any],
) -> None:
    unit_id = str(source["unit_id"])
    symbol = str(source["symbol"])
    role = str(source["role"])
    content_hash = str(source["content_hash"])
    if (
        not symbol.strip()
        or not role.strip()
        or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
    ):
        _fail(f"wiki/{relative}: structured source has invalid provenance")
    citation_key = (
        f"wiki/{relative}",
        unit_id,
        commit,
        source_path,
        symbol,
        role,
        content_hash,
    )
    if evidence["source_citations"][citation_key] < 1:
        _fail(
            f"wiki/{relative}: source-unit citation is not captured: "
            f"{unit_id}:{content_hash}"
        )
    evidence["source_citations"][citation_key] -= 1


def _validate_structured_source(
    relative: str,
    source: dict,
    evidence: dict[str, typing.Any],
) -> None:
    kind = source.get("kind")
    commit = str(source.get("commit", ""))
    if kind == "git-commit":
        if commit not in evidence["commits"]:
            _fail(f"wiki/{relative}: cited Git commit is not captured: {commit}")
        return
    if kind != "source-unit":
        _fail(f"wiki/{relative}: unsupported structured source kind: {kind}")
    required = set(_SOURCE_CITATION_FIELDS)
    missing = sorted(required - set(source))
    if missing:
        _fail(f"wiki/{relative}: structured source missing fields {missing}")
    try:
        uuid.UUID(str(source["unit_id"]))
    except ValueError as exc:
        error = FilesystemKBError(
            f"wiki/{relative}: structured source has invalid unit_id"
        )
        raise error from exc
    source_path = _safe_relative_reference(str(source["path"]), field="source path")
    if commit not in evidence["commits"]:
        _fail(f"wiki/{relative}: cited source commit is not captured: {commit}")
    if (commit, source_path) not in evidence["source_files"]:
        _fail(
            f"wiki/{relative}: cited source file is not captured: "
            f"{commit}:{source_path}"
        )
    _consume_source_citation(relative, commit, source_path, source, evidence)


def _validate_page_sources(
    root: Path,
    relative: str,
    sources,
    evidence: dict[str, typing.Any],
) -> int:
    if not isinstance(sources, list) or not sources:
        _fail(f"wiki/{relative}: sources must be a non-empty list")
    for source in sources:
        if isinstance(source, dict):
            _validate_structured_source(relative, source, evidence)
            continue
        source_path = _safe_relative_reference(str(source), field="source")
        if not source_path.startswith("raw/"):
            _fail(f"wiki/{relative}: source must reference raw/: {source}")
        if not (root / source_path).is_file():
            _fail(f"wiki/{relative}: missing source {source_path}")
    return len(sources)


def _validate_page_links(
    relative: str,
    body: str,
    page_relatives: set[str],
) -> set[str]:
    indexed_links = set()
    for raw_link in _WIKILINK_RE.findall(body):
        target = raw_link.split("|", 1)[0].strip()
        target_path = _safe_relative_reference(target, field="wiki link")
        if not target_path.endswith(".md"):
            target_path += ".md"
        if target_path not in page_relatives:
            _fail(f"wiki/{relative}: broken wiki link {target}")
        if relative == "index.md":
            indexed_links.add(target_path)
    return indexed_links


def _validate_page(
    root: Path,
    page_path: Path,
    page_relatives: set[str],
    ids: set[uuid.UUID],
    evidence: dict[str, typing.Any],
) -> tuple[int, set[str]]:
    relative = page_path.relative_to(root / "wiki").as_posix()
    metadata, body = _parse_page(page_path)
    required = ("id", "title", "type", "status", "project", "sources")
    missing_fields = [field for field in required if field not in metadata]
    if missing_fields:
        _fail(f"wiki/{relative}: missing fields {missing_fields}")
    try:
        document_id = uuid.UUID(str(metadata["id"]))
    except ValueError as exc:
        error = FilesystemKBError(f"wiki/{relative}: invalid id")
        raise error from exc
    if document_id in ids:
        _fail(f"Duplicate page id: {document_id}")
    ids.add(document_id)
    if not all(
        str(metadata[field]).strip() for field in ("title", "type", "status", "project")
    ):
        _fail(f"wiki/{relative}: blank required field")
    citations = _validate_page_sources(
        root,
        relative,
        metadata["sources"],
        evidence,
    )
    indexed_links = _validate_page_links(relative, body, page_relatives)
    return citations, indexed_links


def _validate_pages(root: Path) -> tuple[int, int]:
    page_paths = sorted((root / "wiki").rglob("*.md"))
    if not page_paths:
        _fail("wiki/ contains no Markdown pages")
    page_relatives = {path.relative_to(root / "wiki").as_posix() for path in page_paths}
    evidence = _repository_evidence(root)
    ids = set()
    indexed_links = set()
    cited_sources = 0
    for page_path in page_paths:
        citations, page_links = _validate_page(
            root,
            page_path,
            page_relatives,
            ids,
            evidence,
        )
        cited_sources += citations
        indexed_links.update(page_links)
    if sum(evidence["source_citations"].values()):
        _fail("Cited source snapshot contains unmatched source-unit citations")
    if "index.md" not in page_relatives:
        _fail("wiki/index.md is missing")
    unindexed = page_relatives - {"index.md"} - indexed_links
    if unindexed:
        _fail(f"wiki/index.md does not list pages: {sorted(unindexed)}")
    return len(page_paths), cited_sources


def read_wiki_records(root: Path | str):
    """Yield validated legacy records for explicit common-API reviewed intake."""
    root = Path(root)
    validate_filesystem_kb(root)
    for path in sorted((root / "wiki").rglob("*.md")):
        if path == root / "wiki" / "index.md":
            continue
        metadata, content = _parse_page(path)
        metadata["path"] = path.relative_to(root / "wiki").as_posix()
        yield {"metadata": metadata, "content": content}


def validate_filesystem_kb(root: Path | str) -> dict:
    """Validate schema, provenance hashes, page identity, citations, and links."""
    root = Path(root).resolve()
    if any(path.is_symlink() for path in root.rglob("*")):
        _fail("Portable knowledge bundles cannot contain symlinks")
    actual_raw = _validate_raw_layer(root)
    page_count, cited_sources = _validate_pages(root)
    return {
        "format": FORMAT_VERSION,
        "raw_files": len(actual_raw),
        "pages": page_count,
        "citations": cited_sources,
        "valid": True,
    }
