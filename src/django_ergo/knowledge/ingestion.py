"""Host-extracted text intake; providers, source access and execution remain host-owned."""

import json
from uuid import uuid4

from .schema import Document
from .schema import import_snapshot
from .schema import require
from .toolkit import CorpusToolkit


def propose_wiki_import(service, records, *, provenance, reason):
    """Admit parsed legacy wiki records as reviewed logical revisions.

    Records contain metadata (legacy frontmatter) and content, not paths. The
    original record is retained as evidence; its old citation metadata is not
    misrepresented as independently resolved evidence or host approval.
    """
    base = import_snapshot(service.export())
    changes = []
    for record in records:
        require(
            isinstance(record, dict) and set(record) == {"metadata", "content"},
            "Expected metadata and content",
        )
        metadata = record["metadata"]
        require(isinstance(metadata, dict), "Wiki metadata must be an object")
        require(
            "scope" not in metadata or metadata["scope"] == base.scope,
            "Wiki scope mismatch",
        )
        require(
            isinstance(metadata.get("id"), str) and metadata["id"], "Wiki ID required"
        )
        require(
            isinstance(metadata.get("title"), str) and metadata["title"],
            "Wiki title required",
        )
        require(isinstance(record["content"], str), "Wiki content must be text")
        evidence = Document(
            f"wiki-capture:{uuid4()}",
            str(uuid4()),
            base.scope,
            "evidence",
            metadata["title"],
            json.dumps(record, sort_keys=True, ensure_ascii=False),
            "active",
            provenance,
        )
        status = metadata.get("status", "current")
        page = Document(
            metadata["id"],
            str(uuid4()),
            base.scope,
            "page",
            metadata["title"],
            record["content"],
            "active" if status == "current" else status,
            provenance,
            (evidence.reference,),
            summary=metadata.get("summary", ""),
            hierarchy_code=metadata.get("hierarchy_code", ""),
            path=metadata.get("path", ""),
        )
        changes.extend((evidence, page))
    proposal = service.propose(changes, reason=reason)
    require(
        proposal.base.revision == base.revision, "Corpus changed; repeat wiki capture"
    )
    return proposal


def prepare_absorption(service, *, content, title, provenance, reason):
    """Capture evidence in a pending proposal and bind a curator's cited tools.

    Pass the returned toolkit to any existing extra_tools runner or use it directly.
    Nothing is published until a host reviewer approves and applies its proposal.
    """
    proposal = service.intake(
        content=content, title=title, provenance=provenance, reason=reason
    )
    return CorpusToolkit(
        service,
        provenance=provenance,
        sources=(proposal.changes[0].reference,),
        proposal=proposal,
        reason=reason,
    )
