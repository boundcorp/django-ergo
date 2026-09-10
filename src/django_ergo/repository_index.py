"""Committed Python and Markdown repository indexing."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import re
import uuid
from dataclasses import dataclass

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import Value
from django.utils import timezone

from django_ergo.embedding_providers import get_embedding_provider
from django_ergo.filesystem_support import yaml
from django_ergo.git_snapshot import GitSnapshot
from django_ergo.git_snapshot import GitSnapshotError
from django_ergo.knowledge.retrieval import vector_values
from django_ergo.models import KnowledgeSource
from django_ergo.models import SourceFile
from django_ergo.models import SourceRelation
from django_ergo.models import SourceUnit

_EMBEDDING_DIMENSIONS = 1536


class RepositoryIndexError(RuntimeError):
    """Raised when a committed repository cannot be indexed safely."""


def _fail(message):
    raise RepositoryIndexError(message)


@dataclass(frozen=True)
class ExtractedUnit:
    key: str
    kind: str
    symbol: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str
    lexical_text: str
    evidence_text: str
    metadata: dict


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _glob_match(path, pattern):
    return any(
        fnmatch.fnmatch(path, candidate)
        for candidate in (
            pattern,
            pattern.replace("/**/", "/"),
            pattern.replace("**/", ""),
        )
    )


def _role(config, path):
    for item in config.get("roles", []):
        if any(_glob_match(path, pattern) for pattern in item["include"]):
            return item["role"]
    return None


def _included(config, path):
    return (
        path.endswith((".py", ".md"))
        and not any(_glob_match(path, pattern) for pattern in config.get("exclude", []))
        and _role(config, path) is not None
    )


def _bounded_parts(text, maximum):
    """Split only at line boundaries; callers retain the parent structural card."""
    if len(text) <= maximum:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        remainder = line
        while remainder:
            remaining = maximum - len(current)
            current += remainder[:remaining]
            remainder = remainder[remaining:]
            if len(current) == maximum:
                parts.append(current)
                current = ""
    if current:
        parts.append(current)
    return parts


def _statement_parts(text, node, maximum):
    """Keep Python continuations on statement boundaries when possible."""
    statements = getattr(node, "body", [])
    if not statements:
        return _bounded_parts(text, maximum)
    parts, current = [], ""
    for statement in statements:
        segment = ast.get_source_segment(text, statement) or ""
        if current and len(current) + len(segment) > maximum:
            parts.append(current)
            current = ""
        if len(segment) > maximum:
            parts.extend(_bounded_parts(segment, maximum))
        else:
            current += segment
    if current:
        parts.append(current)
    return parts or _bounded_parts(text, maximum)


def _markdown_parts(text, maximum):
    """Split Markdown at block boundaries while retaining fenced blocks intact."""
    blocks, current, fenced = [], "", False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        boundary = not fenced and not line.strip()
        current += line
        if boundary:
            blocks.append(current)
            current = ""
    if current:
        blocks.append(current)
    parts, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) > maximum:
            parts.append(current)
            current = ""
        if len(block) > maximum:
            parts.extend(_bounded_parts(block, maximum))
        else:
            current += block
    if current:
        parts.append(current)
    return parts or _bounded_parts(text, maximum)


def _source_segment(lines, node):
    return "".join(lines[node.lineno - 1 : node.end_lineno])


def _python_units(path, text, maximum):
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        error = RepositoryIndexError(f"{path}: invalid Python: {exc}")
        raise error from exc
    lines = text.splitlines(keepends=True)
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    units = [
        ExtractedUnit(
            "module",
            "module",
            path,
            path,
            1,
            len(lines),
            "",
            path,
            text[:maximum],
            {"imports": imports},
        )
    ]

    def visit(body, parent=""):
        for node in body:
            if not isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            name = node.name
            qualified = f"{parent}.{name}" if parent else name
            kind = (
                "class"
                if isinstance(node, ast.ClassDef)
                else (
                    "async-function"
                    if isinstance(node, ast.AsyncFunctionDef)
                    else "function"
                )
            )
            signature = ast.get_source_segment(text, node).split("\n", 1)[0]
            metadata = {
                "decorators": [
                    ast.get_source_segment(text, item) or ""
                    for item in node.decorator_list
                ]
            }
            if isinstance(node, ast.ClassDef):
                metadata["inherits"] = [
                    base.id for base in node.bases if isinstance(base, ast.Name)
                ]
            metadata["calls"] = sorted(
                {
                    call.func.id
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                }
            )
            parts = _statement_parts(text, node, maximum)
            units.append(
                ExtractedUnit(
                    qualified,
                    kind,
                    name,
                    qualified,
                    node.lineno,
                    node.end_lineno,
                    signature,
                    f"{path} {qualified} {signature}",
                    parts[0],
                    metadata,
                )
            )
            for part_number, part in enumerate(parts[1:], 2):
                units.append(
                    ExtractedUnit(
                        f"{qualified}:part:{part_number}",
                        f"{kind}-part",
                        name,
                        qualified,
                        node.lineno,
                        node.end_lineno,
                        signature,
                        f"{path} {qualified} part {part_number}",
                        part,
                        {"parent_key": qualified},
                    )
                )
            visit(node.body, qualified)

    visit(tree.body)
    return units


def _markdown_units(path, text, maximum):
    lines = text.splitlines(keepends=True)
    headings = [
        (index, line.lstrip("#").strip(), len(line) - len(line.lstrip("#")))
        for index, line in enumerate(lines)
        if line.startswith("#")
    ]
    units = [
        ExtractedUnit(
            "document",
            "document",
            path,
            path,
            1,
            len(lines),
            "",
            path,
            text[:maximum],
            {},
        )
    ]
    for number, (start, heading, level) in enumerate(headings):
        end = headings[number + 1][0] if number + 1 < len(headings) else len(lines)
        key = f"heading:{start + 1}:{heading}"
        section = "".join(lines[start:end])
        links = re.findall(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)", section)
        parts = _markdown_parts(section, maximum)
        units.append(
            ExtractedUnit(
                key,
                "heading",
                heading,
                heading,
                start + 1,
                end,
                "",
                f"{path} {heading}",
                parts[0],
                {"level": level, "links": links},
            )
        )
        for part_number, part in enumerate(parts[1:], 2):
            units.append(
                ExtractedUnit(
                    f"{key}:part:{part_number}",
                    "heading-part",
                    heading,
                    heading,
                    start + 1,
                    end,
                    "",
                    f"{path} {heading} part {part_number}",
                    part,
                    {"parent_key": key, "level": level},
                )
            )
    return units


def prepare_index(source, commit=None, *, semantic=True):
    try:
        snapshot = GitSnapshot(source, commit)
        config_text = snapshot.read_text(".ergo/index.yaml")
    except (GitSnapshotError, yaml.YAMLError) as exc:
        raise RepositoryIndexError(str(exc)) from exc
    try:
        config = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as exc:
        raise RepositoryIndexError("Invalid index YAML") from exc
    if not isinstance(config, dict):
        _fail("Index configuration must be a mapping.")
    maximum = config.get("max_unit_characters")
    if type(maximum) is not int or not 1 <= maximum <= 100000:
        _fail("max_unit_characters must be between 1 and 100000.")
    if semantic and config.get("embedding_dimensions") != _EMBEDDING_DIMENSIONS:
        _fail("Index configuration must require 1536 dimensions.")
    files = []
    for path in snapshot.paths():
        if not _included(config, path):
            continue
        text = snapshot.read_text(path)
        maximum = config["max_unit_characters"]
        units = (
            _python_units(path, text, maximum)
            if path.endswith(".py")
            else _markdown_units(path, text, maximum)
        )
        files.append((path, snapshot.blob_oid(path), _role(config, path), text, units))
    return snapshot, _hash(config_text), files


def index_repository_commit(  # noqa: C901, PLR0912, PLR0915 - atomic reconciliation.
    source,
    *,
    commit=None,
    provider=None,
    dry_run=False,
    semantic=True,
):
    snapshot, config_hash, extracted = prepare_index(source, commit, semantic=semantic)
    unit_count = sum(len(item[4]) for item in extracted)
    if dry_run:
        return {
            "commit": snapshot.commit,
            "files": len(extracted),
            "units": unit_count,
            "relations": 0,
            "writes": 0,
        }
    provider = (provider or get_embedding_provider()) if semantic else None
    if semantic and provider.get_dimensions() != _EMBEDDING_DIMENSIONS:
        _fail("Repository index provider must produce 1536 dimensions.")
    provider_id = provider.get_index_id() if semantic else ""
    if (
        source.last_indexed_commit == snapshot.commit
        and source.index_config_hash == config_hash
        and source.index_embedding_id == provider_id
    ):
        return {
            "commit": snapshot.commit,
            "files": len(extracted),
            "units": unit_count,
            "relations": SourceRelation.objects.filter(source=source).count(),
            "writes": 0,
        }
    observed_checkpoint = source.last_indexed_commit
    existing_files = list(SourceFile.objects.filter(source=source))
    files_by_path = {item.relative_path: item for item in existing_files}
    files_by_hash = {}
    for item in existing_files:
        files_by_hash.setdefault(item.content_hash, []).append(item)
    renames = snapshot.rename_map(source.last_indexed_commit)
    existing_units = {
        unit.id: unit for unit in SourceUnit.objects.filter(source_file__source=source)
    }
    existing_units_by_hash = {}
    for unit in existing_units.values():
        existing_units_by_hash.setdefault(
            (unit.source_file_id, unit.content_hash), []
        ).append(unit)
    claimed_unit_ids = set()
    claimed_file_ids = set()
    prepared = []
    for path, oid, role, text, cards in extracted:
        content_hash = _hash(text)
        matched = files_by_path.get(path) or files_by_path.get(renames.get(path, ""))
        if matched is None:
            candidates = [
                item
                for item in files_by_hash.get(content_hash, [])
                if item.id not in claimed_file_ids
            ]
            if len(candidates) == 1:
                matched = candidates[0]
        file_id = matched.id if matched else uuid.uuid5(source.id, path)
        claimed_file_ids.add(file_id)
        prepared_cards = []
        for card in cards:
            input_text = (
                f"{role}\n{path}\n{card.kind}\n{card.qualified_name}\n"
                f"{card.signature}\n{card.evidence_text}"
            )
            input_hash = _hash(input_text)
            unit_id = uuid.uuid5(file_id, card.key)
            old = existing_units.get(unit_id)
            if old is None:
                candidates = [
                    candidate
                    for candidate in existing_units_by_hash.get(
                        (file_id, _hash(card.evidence_text)), []
                    )
                    if candidate.id not in claimed_unit_ids
                ]
                if len(candidates) == 1:
                    old = candidates[0]
                    unit_id = old.id
            if old:
                claimed_unit_ids.add(old.id)
            reuse_embedding = (
                old
                and old.embedding_input_hash == input_hash
                and source.index_embedding_id == provider_id
            )
            vector = (
                old.embedding
                if reuse_embedding
                else list(
                    vector_values(
                        provider.generate_embedding(input_text), _EMBEDDING_DIMENSIONS
                    )
                )
                if semantic
                else None
            )
            prepared_cards.append(
                (unit_id, card, input_hash, vector, not reuse_embedding)
            )
        prepared.append((file_id, path, oid, role, text, prepared_cards))
    writes = relation_count = 0
    with transaction.atomic():
        locked = KnowledgeSource.objects.select_for_update().get(pk=source.pk)
        if locked.last_indexed_commit != observed_checkpoint:
            _fail("Knowledge source changed while indexing; retry.")
        seen_files, seen_units = [], []
        SourceRelation.objects.filter(source=locked).delete()
        existing_files = {
            item.id: item for item in SourceFile.objects.filter(source=locked)
        }
        for file_id, path, oid, role, text, cards in prepared:
            values = {
                "source": locked,
                "relative_path": path,
                "git_blob_oid": oid,
                "language": "python" if path.endswith(".py") else "markdown",
                "source_role": role,
                "content_hash": _hash(text),
                "last_indexed_commit": snapshot.commit,
            }
            source_file = existing_files.get(file_id)
            if source_file is None:
                source_file = SourceFile.objects.create(id=file_id, **values)
                writes += 1
            elif any(
                getattr(source_file, key) != value
                for key, value in values.items()
                if key != "source"
            ):
                if source_file.relative_path != path:
                    source_file.prior_paths = list(source_file.prior_paths or []) + [
                        source_file.relative_path
                    ]
                for key, value in values.items():
                    setattr(source_file, key, value)
                source_file.save()
                writes += 1
            seen_files.append(file_id)
            for unit_id, card, input_hash, vector, vector_changed in cards:
                seen_units.append(unit_id)
                values = {
                    "source_file": source_file,
                    "unit_key": card.key,
                    "kind": card.kind,
                    "symbol": card.symbol,
                    "qualified_name": card.qualified_name,
                    "start_line": card.start_line,
                    "end_line": card.end_line,
                    "signature": card.signature,
                    "lexical_text": card.lexical_text,
                    "evidence_text": card.evidence_text,
                    "content_hash": _hash(card.evidence_text),
                    "embedding_input_hash": input_hash,
                    "embedding": vector,
                    "metadata": card.metadata,
                }
                old = existing_units.get(unit_id)
                if old is None:
                    SourceUnit.objects.create(
                        id=unit_id,
                        search_vector=SearchVector(
                            Value(card.lexical_text), config="english"
                        ),
                        **values,
                    )
                    writes += 1
                elif vector_changed or any(
                    getattr(old, key) != value
                    for key, value in values.items()
                    if key != "embedding"
                ):
                    if old.unit_key != card.key:
                        old.prior_keys = list(old.prior_keys or []) + [old.unit_key]
                    for key, value in values.items():
                        setattr(old, key, value)
                    old.search_vector = SearchVector(
                        Value(card.lexical_text), config="english"
                    )
                    old.save()
                    writes += 1
            parent_id = uuid.uuid5(
                file_id, "module" if path.endswith(".py") else "document"
            )
            for unit_id, *_ in cards:
                if unit_id != parent_id:
                    SourceRelation.objects.create(
                        source=locked,
                        from_unit_id=parent_id,
                        to_unit_id=unit_id,
                        relation_type="contains",
                    )
                    relation_count += 1
        indexed_units = list(
            SourceUnit.objects.filter(source_file__source=locked).select_related(
                "source_file"
            )
        )
        by_symbol, by_path, by_module, relation_keys = {}, {}, {}, set()
        for unit in indexed_units:
            if unit.symbol:
                by_symbol.setdefault(unit.symbol, []).append(unit)
            if unit.kind in {"module", "document"}:
                by_path[unit.source_file.relative_path] = unit
                module_path = unit.source_file.relative_path
                if module_path.startswith("src/"):
                    module_path = module_path[4:]
                if module_path.endswith(".py"):
                    module_path = module_path[:-3]
                by_module[module_path.replace("/", ".")] = unit
        for unit in indexed_units:
            for relation_type, names in (
                ("imports", unit.metadata.get("imports", [])),
                ("links", unit.metadata.get("links", [])),
            ):
                for name in names:
                    target = (
                        by_module.get(name)
                        if relation_type == "imports"
                        else by_path.get(name.lstrip("./"))
                    )
                    key = (unit.id, target.id, relation_type) if target else None
                    if key and key not in relation_keys:
                        SourceRelation.objects.create(
                            source=locked,
                            from_unit=unit,
                            to_unit=target,
                            relation_type=relation_type,
                        )
                        relation_keys.add(key)
                        relation_count += 1
        for unit in SourceUnit.objects.filter(source_file__source=locked):
            for relation_type, names in (
                ("calls", unit.metadata.get("calls", [])),
                ("inherits", unit.metadata.get("inherits", [])),
            ):
                for name in names:
                    targets = by_symbol.get(name, [])
                    if len(targets) == 1 and targets[0].id != unit.id:
                        key = (unit.id, targets[0].id, relation_type)
                        if key not in relation_keys:
                            SourceRelation.objects.create(
                                source=locked,
                                from_unit=unit,
                                to_unit=targets[0],
                                relation_type=relation_type,
                            )
                            relation_keys.add(key)
                            relation_count += 1
        stale_units = SourceUnit.objects.filter(source_file__source=locked).exclude(
            id__in=seen_units
        )
        deleted_units = stale_units.count()
        stale_units.delete()
        stale_files = SourceFile.objects.filter(source=locked).exclude(
            id__in=seen_files
        )
        deleted_files = stale_files.count()
        stale_files.delete()
        locked.last_indexed_commit = snapshot.commit
        locked.last_indexed_at = timezone.now()
        locked.index_config_hash = config_hash
        locked.index_embedding_id = provider_id
        locked.save(
            update_fields=[
                "last_indexed_commit",
                "last_indexed_at",
                "index_config_hash",
                "index_embedding_id",
                "updated_at",
            ]
        )
        writes += 1
    return {
        "commit": snapshot.commit,
        "files": len(extracted),
        "units": unit_count,
        "relations": relation_count,
        "writes": writes + deleted_units + deleted_files,
    }
