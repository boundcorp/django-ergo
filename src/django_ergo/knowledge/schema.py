"""Logical corpus schema shared by virtual, database and filesystem sources."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import fields
from datetime import datetime

FORMAT = "ergo-corpus/v1"
EXTENDED_FORMAT = "ergo-corpus/v2"
PATH_FORMAT = "ergo-corpus/v3"
EXPORT_FORMAT = "ergo-corpus-export/v1"
MAX_DOCUMENTS = 10000
MAX_CONTENT_BYTES = 1024 * 1024
MAX_CORPUS_BYTES = 16 * 1024 * 1024
MAX_ID_LENGTH = 255
STATUSES = frozenset({"draft", "active", "stale", "archived", "superseded"})


class CorpusError(ValueError):
    """A corpus violates its identity, provenance or publication contract."""


def require(condition, message):
    if not condition:
        raise CorpusError(message)


def digest(value):
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _identity(value):
    return _text(value) and len(value) <= MAX_ID_LENGTH


def _timestamp(value):
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
        )
    except ValueError:
        return False


def _record(record_type, value):
    require(isinstance(value, dict), "Record must be an object")
    if record_type is Document:
        value = {"summary": "", "hierarchy_code": "", "path": "", **value}
    require(
        set(value) == {field.name for field in fields(record_type)},
        "Invalid record fields",
    )
    if record_type in (Reference, Provenance, Review):
        require(
            all(_text(item) for item in value.values()),
            "Record fields must be non-empty strings",
        )
    return record_type(**value)


@dataclass(frozen=True)
class Reference:
    document_id: str
    revision: str
    digest: str


@dataclass(frozen=True)
class Provenance:
    origin: str
    source_revision: str
    actor: str
    captured_at: str


@dataclass(frozen=True)
class Review:
    reviewer: str
    decision: str
    reason: str
    policy_version: str
    reviewed_digest: str


@dataclass(frozen=True)
class Document:
    document_id: str
    revision: str
    scope: str
    kind: str
    title: str
    content: str
    status: str
    provenance: Provenance
    sources: tuple[Reference, ...] = ()
    review: Review | None = None
    summary: str = ""
    hierarchy_code: str = ""
    path: str = ""

    @property
    def content_digest(self):
        payload = asdict(self)
        payload.pop("review")
        for name in ("summary", "hierarchy_code", "path"):
            if not payload[name]:
                payload.pop(name)
        return digest(payload)

    @property
    def reference(self):
        return Reference(self.document_id, self.revision, self.content_digest)


@dataclass(frozen=True)
class Snapshot:
    collection_id: str
    scope: str
    documents: tuple[Document, ...]
    heads: tuple[Reference, ...]

    @property
    def revision(self):
        return digest(self.to_dict())

    def to_dict(self):
        payload = asdict(self)
        extended = False
        path_format = any(document.path for document in self.documents)
        for record in payload["documents"]:
            if not record["path"]:
                record.pop("path")
            for name in ("summary", "hierarchy_code"):
                if not record[name]:
                    record.pop(name)
                else:
                    extended = True
            extended = extended or record["kind"] == "strategy"
        head_keys = {(head.document_id, head.revision) for head in self.heads}
        codes = [
            document.hierarchy_code
            for document in self.documents
            if (document.document_id, document.revision) in head_keys
            and document.hierarchy_code
        ]
        path_format = path_format or len(codes) != len(set(codes))
        payload["format"] = (
            PATH_FORMAT if path_format else EXTENDED_FORMAT if extended else FORMAT
        )
        payload["documents"] = sorted(
            payload["documents"],
            key=lambda item: (item["document_id"], item["revision"]),
        )
        payload["heads"] = sorted(
            payload["heads"], key=lambda item: item["document_id"]
        )
        return json.loads(json.dumps(payload))

    @classmethod
    def from_dict(cls, payload, *, require_publication=True):
        require(isinstance(payload, dict), "Corpus must be an object")
        require(
            set(payload) == {"format", "collection_id", "scope", "documents", "heads"},
            "Invalid corpus fields",
        )
        require(
            payload["format"] in {FORMAT, EXTENDED_FORMAT, PATH_FORMAT},
            "Unsupported corpus format",
        )
        require(
            isinstance(payload["documents"], list)
            and isinstance(payload["heads"], list),
            "Documents and heads must be lists",
        )
        require(
            len(payload["documents"]) <= MAX_DOCUMENTS, "Too many document revisions"
        )
        documents = []
        for value in payload["documents"]:
            require(isinstance(value, dict), "Document must be an object")
            record = dict(value)
            if payload["format"] != PATH_FORMAT:
                require("path" not in record, "Path records require ergo-corpus/v3")
            if payload["format"] == FORMAT:
                require(
                    not ({"summary", "hierarchy_code"} & set(record))
                    and record.get("kind") != "strategy",
                    "Extended records require ergo-corpus/v2",
                )
            require(isinstance(record.get("sources"), list), "Sources must be a list")
            record["sources"] = tuple(
                _record(Reference, source) for source in record["sources"]
            )
            record["provenance"] = _record(Provenance, record.get("provenance"))
            record["review"] = (
                _record(Review, record["review"])
                if record.get("review") is not None
                else None
            )
            documents.append(_record(Document, record))
        snapshot = cls(
            payload["collection_id"],
            payload["scope"],
            tuple(documents),
            tuple(_record(Reference, head) for head in payload["heads"]),
        )
        validate_snapshot(snapshot, require_publication=require_publication)
        return snapshot


def validate_snapshot(snapshot, *, require_publication=True):
    """Check structure and references, not host authorization or factual truth."""
    from .paths import validate_path

    require(isinstance(snapshot, Snapshot), "Snapshot must use the logical schema")
    require(
        isinstance(snapshot.documents, tuple) and isinstance(snapshot.heads, tuple),
        "Snapshot records must be immutable tuples",
    )
    require(
        _identity(snapshot.collection_id) and _identity(snapshot.scope),
        "Collection identity and scope are required",
    )
    require(len(snapshot.documents) <= MAX_DOCUMENTS, "Too many document revisions")
    records = {}
    for document in snapshot.documents:
        require(isinstance(document, Document), "Invalid document record")
        require(
            isinstance(document.provenance, Provenance), "Invalid provenance record"
        )
        require(
            isinstance(document.sources, tuple), "Sources must be immutable references"
        )
        require(
            _identity(document.document_id) and _identity(document.revision),
            "Invalid document identity",
        )
        require(
            all(
                _text(value)
                for value in (document.document_id, document.revision, document.title)
            ),
            "Document identity, revision and title are required",
        )
        require(document.scope == snapshot.scope, "Document scope mismatch")
        validate_path(document.path, allow_empty=True)
        require(
            not document.path or document.kind == "page",
            "Only pages have navigation paths",
        )
        require(
            _text(document.kind) and document.kind in {"page", "evidence", "strategy"},
            "Invalid document kind",
        )
        require(
            _text(document.status) and document.status in STATUSES,
            "Invalid lifecycle status",
        )
        require(
            isinstance(document.content, str)
            and len(document.content.encode("utf-8")) <= MAX_CONTENT_BYTES,
            "Invalid or oversized content",
        )
        require(
            isinstance(document.summary, str)
            and len(document.summary.encode("utf-8")) <= MAX_CONTENT_BYTES,
            "Invalid or oversized summary",
        )
        require(
            isinstance(document.hierarchy_code, str)
            and len(document.hierarchy_code) <= MAX_ID_LENGTH,
            "Invalid hierarchy code",
        )
        require(
            not document.hierarchy_code
            or (
                document.kind == "page"
                and document.hierarchy_code.isprintable()
                and bool(document.hierarchy_code.strip())
            ),
            "Hierarchy codes must be printable nonblank page codes",
        )
        require(
            all(_text(value) for value in asdict(document.provenance).values()),
            "Capture provenance is required",
        )
        require(
            _timestamp(document.provenance.captured_at),
            "Capture timestamp must include a timezone",
        )
        key = (document.document_id, document.revision)
        require(key not in records, "Duplicate document revision")
        records[key] = document
        if document.review is not None:
            require(isinstance(document.review, Review), "Invalid review record")
            require(
                all(_text(value) for value in asdict(document.review).values()),
                "Review fields are required",
            )
            require(
                document.review.decision in {"approved", "rejected"},
                "Invalid review decision",
            )
            require(
                document.review.reviewed_digest == document.content_digest,
                "Review does not match document revision",
            )
        if document.kind == "page" and document.status == "active":
            require(bool(document.sources), "Active pages require evidence references")
            if require_publication:
                require(
                    document.review is not None
                    and document.review.decision == "approved",
                    "Active pages require approval",
                )
        if (
            document.kind == "strategy"
            and document.status == "active"
            and require_publication
        ):
            require(
                document.review is not None and document.review.decision == "approved",
                "Active strategy requires approval",
            )
    heads = {}
    for head in snapshot.heads:
        require(isinstance(head, Reference), "Invalid head record")
        require(
            _text(head.document_id) and _text(head.revision), "Invalid head identity"
        )
        require(head.document_id not in heads, "Duplicate document head")
        target = records.get((head.document_id, head.revision))
        require(
            target is not None and target.content_digest == head.digest,
            "Head does not resolve",
        )
        heads[head.document_id] = target
    require(
        set(heads) == {document.document_id for document in snapshot.documents},
        "Every document needs exactly one head",
    )
    _validate_layout(heads)
    for document in snapshot.documents:
        for reference in document.sources:
            require(isinstance(reference, Reference), "Invalid citation record")
            require(
                _text(reference.document_id) and _text(reference.revision),
                "Invalid citation identity",
            )
            target = records.get((reference.document_id, reference.revision))
            require(
                target is not None and target.content_digest == reference.digest,
                "Citation does not resolve",
            )
            require(target.kind == "evidence", "Page sources must cite evidence")
    require(
        len(json.dumps(snapshot.to_dict()).encode()) <= MAX_CORPUS_BYTES,
        "Corpus exceeds size limit",
    )


def _validate_layout(heads):
    from .paths import tree_status

    paths = [document.path for document in heads.values() if document.path]
    require(len(paths) == len(set(paths)), "Duplicate document path")
    require(
        sum(document.kind == "strategy" for document in heads.values()) <= 1,
        "Only one strategy document is allowed",
    )
    for document in heads.values():
        if document.kind == "strategy":
            tree_status(document.content, ())


def export_snapshot(snapshot):
    validate_snapshot(snapshot)
    return {
        "format": EXPORT_FORMAT,
        "revision": snapshot.revision,
        "corpus": snapshot.to_dict(),
    }


def import_snapshot(envelope):
    require(
        isinstance(envelope, dict)
        and set(envelope) == {"format", "revision", "corpus"},
        "Invalid export envelope",
    )
    require(envelope["format"] == EXPORT_FORMAT, "Unsupported export format")
    snapshot = Snapshot.from_dict(envelope["corpus"])
    require(snapshot.revision == envelope["revision"], "Export revision mismatch")
    return snapshot
