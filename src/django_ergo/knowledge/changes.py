"""Serializable proposals and append-only, scope-preserving revision changes."""

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace

from .schema import Document
from .schema import Snapshot
from .schema import digest
from .schema import require
from .schema import validate_snapshot


@dataclass(frozen=True)
class Proposal:
    base: Snapshot
    changes: tuple
    actor: str
    reason: str

    @property
    def candidate(self):
        require(
            isinstance(self.changes, tuple) and self.changes, "Changes are required"
        )
        require(
            all(
                isinstance(value, str) and value.strip()
                for value in (self.actor, self.reason)
            ),
            "Actor and reason are required",
        )
        validate_snapshot(self.base)
        require(
            all(isinstance(document, Document) for document in self.changes),
            "Changes must be document records",
        )
        changed_ids = {document.document_id for document in self.changes}
        require(
            len(changed_ids) == len(self.changes), "One change per document is required"
        )
        records = {document.document_id: document for document in self.base.documents}
        for document in self.changes:
            previous = records.get(document.document_id)
            require(
                previous is None or previous.kind == document.kind,
                "Document kind is immutable",
            )
        snapshot = replace(
            self.base,
            documents=self.base.documents + self.changes,
            heads=tuple(
                head for head in self.base.heads if head.document_id not in changed_ids
            )
            + tuple(document.reference for document in self.changes),
        )
        validate_snapshot(snapshot, require_publication=False)
        return snapshot

    @property
    def revision(self):
        return digest(self.to_dict())

    def to_dict(self):
        candidate = self.candidate
        return {
            "format": "ergo-proposal/v1",
            "base": self.base.to_dict(),
            "candidate": candidate.to_dict(),
            "actor": self.actor,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload):
        require(
            isinstance(payload, dict)
            and set(payload) == {"format", "base", "candidate", "actor", "reason"}
            and payload["format"] == "ergo-proposal/v1",
            "Invalid proposal envelope",
        )
        base = Snapshot.from_dict(payload["base"])
        candidate = Snapshot.from_dict(payload["candidate"], require_publication=False)
        keys = {
            (document.document_id, document.revision) for document in base.documents
        }
        proposal = cls(
            base,
            tuple(
                document
                for document in candidate.documents
                if (document.document_id, document.revision) not in keys
            ),
            payload["actor"],
            payload["reason"],
        )
        require(
            proposal.candidate.to_dict() == candidate.to_dict(),
            "Proposal rewrites history or scope",
        )
        return proposal


def operation_event(proposal, *, actor, captured_at):
    return {
        "format": "ergo-operation/v1",
        "proposal_revision": proposal.revision,
        "base_revision": proposal.base.revision,
        "result_revision": proposal.candidate.revision,
        "actor": actor,
        "proposed_by": proposal.actor,
        "reason": proposal.reason,
        "captured_at": captured_at,
        "affected_documents": [document.document_id for document in proposal.changes],
        "sources": [
            asdict(source)
            for document in proposal.changes
            for source in document.sources
        ],
        "reviews": [asdict(document.review) for document in proposal.changes],
    }
