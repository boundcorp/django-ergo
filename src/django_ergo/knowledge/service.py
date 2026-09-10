"""Authorized validation and cited retrieval shared by every corpus backend."""

from __future__ import annotations

import math
import re
from copy import copy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Protocol
from uuid import uuid4

from .changes import Proposal
from .changes import operation_event
from .hierarchy import allocate_code
from .hierarchy import tree_block
from .hierarchy import tree_status
from .retrieval import capability
from .retrieval import normalized_weights
from .schema import MAX_ID_LENGTH
from .schema import CorpusError
from .schema import Document
from .schema import Review
from .schema import export_snapshot
from .schema import require
from .schema import validate_snapshot
from .usage import MemoryUsageStore
from .usage import UsageRecordingError

if TYPE_CHECKING:
    from .backends import CorpusBackend

MAX_QUERY_LENGTH = 1000
MAX_RESULTS = 100


class AccessDeniedError(PermissionError):
    """The host denied this operation; no source content was returned."""


@dataclass(frozen=True)
class AccessEvent:
    collection_id: str
    scope: str
    action: str
    allowed: bool


@dataclass(frozen=True)
class ReviewEvent:
    collection_id: str
    scope: str
    proposal_revision: str
    actor: str
    decisions: tuple


class HostPolicy(Protocol):
    def allows(
        self, principal, collection_id: str, scope: str, action: str
    ) -> bool: ...

    def accepts_review(
        self, principal, collection_id: str, scope: str, review: Review
    ) -> bool: ...

    def audit(self, principal, event: AccessEvent | ReviewEvent) -> None: ...

    def actor(self, principal) -> str: ...

    def accepts_proposal(self, principal, proposal: Proposal) -> bool: ...


class CorpusService:
    """Host-bound operations; backend objects themselves are trusted infrastructure."""

    def __init__(
        self,
        backend: CorpusBackend,
        policy: HostPolicy,
        principal,
        *,
        embedding_provider=None,
        provider_id=None,
        vector_index=None,
        usage_store=None,
        usage_context="",
    ):
        self.backend = backend
        self.policy = policy
        self.principal = principal
        self.embedding_provider = embedding_provider
        self.provider_id = provider_id
        self.vector_index = vector_index
        self.usage_store = (
            usage_store if usage_store is not None else MemoryUsageStore()
        )
        require(
            isinstance(usage_context, str) and len(usage_context) <= MAX_ID_LENGTH,
            "Invalid usage context",
        )
        self.usage_context = usage_context

    def _authorize(self, action):
        allowed = (
            self.policy.allows(
                self.principal, self.backend.collection_id, self.backend.scope, action
            )
            is True
        )
        self.policy.audit(
            self.principal,
            AccessEvent(
                self.backend.collection_id, self.backend.scope, action, allowed
            ),
        )
        if not allowed:
            message = "Corpus access denied"
            raise AccessDeniedError(message)

    def with_usage_context(self, context_id):
        require(
            isinstance(context_id, str) and len(context_id) <= MAX_ID_LENGTH,
            "Invalid usage context",
        )
        service = copy(self)
        service.usage_context = context_id
        return service

    def _load(self, action):
        self._authorize(action)
        snapshot = self.backend.load()
        require(
            snapshot.collection_id == self.backend.collection_id
            and snapshot.scope == self.backend.scope,
            "Backend identity mismatch",
        )
        validate_snapshot(snapshot)
        for document in snapshot.documents:
            if document.kind in {"page", "strategy"} and document.status == "active":
                accepted = (
                    self.policy.accepts_review(
                        self.principal,
                        snapshot.collection_id,
                        snapshot.scope,
                        document.review,
                    )
                    is True
                )
                require(accepted, "Host does not accept the publication review")
        return snapshot

    @staticmethod
    def _records(snapshot):
        return {
            (document.document_id, document.revision): document
            for document in snapshot.documents
        }

    @staticmethod
    def _result(snapshot, document):
        return {
            "collection_id": snapshot.collection_id,
            "scope": snapshot.scope,
            "corpus_revision": snapshot.revision,
            "citation": asdict(document.reference),
            "title": document.title,
            "kind": document.kind,
            "status": document.status,
            "content": document.content,
            "sources": [asdict(reference) for reference in document.sources],
            "provenance": asdict(document.provenance),
            "summary": document.summary,
            "hierarchy_code": document.hierarchy_code,
        }

    def validate(self):
        snapshot = self._load("validate")
        return {
            "collection_id": snapshot.collection_id,
            "scope": snapshot.scope,
            "revision": snapshot.revision,
            "documents": len(snapshot.heads),
        }

    def search(self, query, *, limit=10, mode="lexical", weights=None):
        require(mode in {"lexical", "semantic", "hybrid"}, "Unknown search mode")
        if mode == "semantic":
            return self.multi_field_semantic_search(query, top_k=limit, weights=weights)
        if mode == "hybrid":
            return self.hybrid_search(query, top_k=limit, weights=weights)
        snapshot = self._load("search")
        require(
            isinstance(query, str) and 0 < len(query.strip()) <= MAX_QUERY_LENGTH,
            "Query must contain 1-1000 characters",
        )
        require(
            type(limit) is int and 1 <= limit <= MAX_RESULTS,
            "Limit must be between 1 and 100",
        )
        terms = set(re.findall(r"\w+", query.casefold()))
        field_weights = (
            normalized_weights(weights)
            if weights is not None
            else {"title": 3, "content": 1}
        )
        records = self._records(snapshot)
        matches = []
        for head in snapshot.heads:
            document = records[(head.document_id, head.revision)]
            if document.kind != "page" or document.status != "active":
                continue
            score = sum(
                weight * getattr(document, name).casefold().count(term)
                for term in terms
                for name, weight in field_weights.items()
            )
            if score:
                result = self._result(snapshot, document)
                result["excerpt"] = result.pop("content")[:400]
                result["score"] = score
                matches.append(result)
        results = sorted(
            matches, key=lambda item: (-item["score"], item["citation"]["document_id"])
        )[:limit]
        self._track(snapshot, "search", results)
        return results

    def get_document(self, document_id, *, revision=None):
        if revision is not None:
            self._authorize("history")
        snapshot = self._load("read")
        records = self._records(snapshot)
        if revision is not None:
            document = records.get((document_id, revision))
        else:
            head = next(
                (head for head in snapshot.heads if head.document_id == document_id),
                None,
            )
            document = records.get((head.document_id, head.revision)) if head else None
            if document and document.status != "active":
                document = None
        if document is None:
            message = "Document is unavailable"
            raise CorpusError(message)
        if document.kind == "evidence":
            self._authorize("evidence")
        result = self._result(snapshot, document)
        self._track(snapshot, "read", [result])
        return result

    def resolve(self, reference):
        result = self.get_document(reference.document_id, revision=reference.revision)
        require(
            result["citation"]["digest"] == reference.digest, "Citation digest mismatch"
        )
        return result

    def export(self):
        self._authorize("history")
        self._authorize("evidence")
        snapshot = self._load("export")
        self._track(snapshot, "export")
        return export_snapshot(snapshot)

    def table_of_contents(self, *, prefix=""):
        snapshot = self._load("read")
        require(isinstance(prefix, str), "Hierarchy prefix must be text")
        records = self._records(snapshot)
        results = [
            {
                "document_id": head.document_id,
                "title": records[(head.document_id, head.revision)].title,
                "hierarchy_code": records[
                    (head.document_id, head.revision)
                ].hierarchy_code,
            }
            for head in sorted(snapshot.heads, key=lambda item: item.document_id)
            if records[(head.document_id, head.revision)].kind == "page"
            and records[(head.document_id, head.revision)].status == "active"
            and records[(head.document_id, head.revision)].hierarchy_code.startswith(
                prefix
            )
        ]
        self._track(snapshot, "read")
        return sorted(
            results, key=lambda item: (item["hierarchy_code"], item["document_id"])
        )

    def propose(self, documents, *, reason):
        """Prepare an untrusted artifact; this does not publish or grant approval."""
        self._authorize("history")
        self._authorize("evidence")
        documents = tuple(documents)
        require(
            all(isinstance(document, Document) for document in documents),
            "Changes must be document records",
        )
        if any(document.kind == "evidence" for document in documents):
            self._authorize("intake")
        snapshot = self._load("propose")
        proposal = Proposal(
            snapshot, documents, self.policy.actor(self.principal), reason
        )
        _candidate = proposal.candidate
        self._track(snapshot, "suggest")
        return proposal

    def intake(self, *, content, title, provenance, reason, document_id=None):
        """Admit host-extracted text as proposed evidence, never fetch a locator."""
        self._authorize("intake")
        document = Document(
            document_id or str(uuid4()),
            str(uuid4()),
            self.backend.scope,
            "evidence",
            title,
            content,
            "active",
            provenance,
        )
        return self.propose((document,), reason=reason)

    def revise(self, document_id, *, reason, **changes):
        """Correct, withdraw or supersede a head by appending a new revision."""
        require(
            set(changes)
            <= {
                "title",
                "content",
                "status",
                "sources",
                "provenance",
                "summary",
                "hierarchy_code",
            },
            "Unsupported revision fields",
        )
        self._authorize("history")
        self._authorize("evidence")
        snapshot = self._load("propose")
        head = next(
            (head for head in snapshot.heads if head.document_id == document_id), None
        )
        require(head is not None, "Document is unavailable")
        document = self._records(snapshot)[(head.document_id, head.revision)]
        updated = replace(document, **changes, revision=str(uuid4()), review=None)
        proposal = Proposal(
            snapshot, (updated,), self.policy.actor(self.principal), reason
        )
        _candidate = proposal.candidate
        self._track(snapshot, "suggest")
        return proposal

    def _check_proposal(self, proposal, action):
        self._authorize("history")
        self._authorize("evidence")
        snapshot = self._load(action)
        require(isinstance(proposal, Proposal), "A proposal artifact is required")
        require(
            proposal.base.collection_id == snapshot.collection_id
            and proposal.base.scope == snapshot.scope,
            "Proposal scope mismatch",
        )
        require(
            snapshot.revision == proposal.base.revision,
            "Corpus changed; rebase and review again",
        )
        _candidate = proposal.candidate

    def review(self, proposal, reviews):
        """Bind host-issued review records to exact proposed document digests."""
        self._check_proposal(proposal, "review")
        require(len(reviews) == len(proposal.changes), "Every change needs a review")
        reviewed = replace(
            proposal,
            changes=tuple(
                replace(document, review=review)
                for document, review in zip(proposal.changes, reviews, strict=True)
            ),
        )
        _candidate = reviewed.candidate
        for document in reviewed.changes:
            require(
                self.policy.accepts_review(
                    self.principal,
                    self.backend.collection_id,
                    self.backend.scope,
                    document.review,
                )
                is True,
                "Host does not accept the change review",
            )
        require(
            self.policy.accepts_proposal(self.principal, reviewed) is True,
            "Host does not accept the proposal receipt",
        )
        self.policy.audit(
            self.principal,
            ReviewEvent(
                self.backend.collection_id,
                self.backend.scope,
                reviewed.revision,
                self.policy.actor(self.principal),
                tuple(document.review.decision for document in reviewed.changes),
            ),
        )
        return reviewed

    def apply(self, proposal):
        """Atomically publish a reviewed artifact using a backend compare-and-swap."""
        self._check_proposal(proposal, "apply")
        for document in proposal.changes:
            require(
                document.review is not None and document.review.decision == "approved",
                "Every applied change requires approval",
            )
            require(
                self.policy.accepts_review(
                    self.principal,
                    self.backend.collection_id,
                    self.backend.scope,
                    document.review,
                )
                is True,
                "Host does not accept the change review",
            )
        require(
            self.policy.accepts_proposal(self.principal, proposal) is True,
            "Host does not accept the proposal receipt",
        )
        snapshot = proposal.candidate
        validate_snapshot(snapshot)
        commit = getattr(self.backend, "commit", None)
        require(
            callable(commit),
            "Backend is read-only; stage changes in MemoryCorpus or DatabaseCorpus.workspace",
        )
        event = operation_event(
            proposal,
            actor=self.policy.actor(self.principal),
            captured_at=datetime.now(UTC).isoformat(),
        )
        commit(snapshot, expected_revision=proposal.base.revision, event=event)
        self._track(
            snapshot,
            "write",
            [self._result(snapshot, document) for document in proposal.changes],
        )
        return event

    @staticmethod
    def _active_pages(snapshot):
        records = CorpusService._records(snapshot)
        return [
            records[(head.document_id, head.revision)]
            for head in snapshot.heads
            if records[(head.document_id, head.revision)].kind == "page"
            and records[(head.document_id, head.revision)].status == "active"
        ]

    def _track(self, snapshot, mode, results=(), *, context_id=None):
        actor = getattr(self.policy, "actor", None)
        event = {
            "format": "ergo-usage/v1",
            "event_id": str(uuid4()),
            "collection_id": snapshot.collection_id,
            "scope": snapshot.scope,
            "corpus_revision": snapshot.revision,
            "mode": mode,
            "context_id": self.usage_context if context_id is None else context_id,
            "actor": actor(self.principal) if callable(actor) else "",
            "captured_at": datetime.now(UTC).isoformat(),
            "references": [result["citation"] for result in results],
        }
        try:
            self.usage_store.record(event)
        except Exception as exc:
            raise UsageRecordingError(committed=mode == "write") from exc
        return event

    def record_usage(self, context_id, *, mode="read"):
        actions = {
            "read": "read",
            "search": "search",
            "write": "apply",
            "suggest": "propose",
            "strategy": "read",
        }
        require(
            mode in actions
            and isinstance(context_id, str)
            and len(context_id) <= MAX_ID_LENGTH,
            "Invalid usage binding",
        )
        return self._track(self._load(actions[mode]), mode, context_id=context_id)

    def usage(self, *, context_id=None):
        self._authorize("usage")
        return self.usage_store.read(
            self.backend.collection_id, self.backend.scope, context_id
        )

    def rebuild_index(self):
        snapshot = self._load("index")
        capability(
            self.vector_index is not None and self.embedding_provider is not None,
            "Configure a vector_index and embedding_provider to rebuild semantic search",
        )
        self.vector_index.rebuild(
            snapshot,
            self._active_pages(snapshot),
            self.embedding_provider,
            self.provider_id,
        )
        return {
            "corpus_revision": snapshot.revision,
            "provider_id": self.provider_id,
            "documents": len(self._active_pages(snapshot)),
        }

    def _rank_vectors(
        self,
        snapshot,
        query_vector,
        *,
        top_k,
        weights,
        query_text=None,
        lexical_weight=0,
    ):
        documents = self._active_pages(snapshot)
        scores = self.vector_index.scores(
            snapshot, documents, query_vector, weights, self.provider_id
        )
        require(
            set(scores) <= {document.reference for document in documents},
            "Index returned an unauthorized document",
        )
        lexical = {}
        if lexical_weight:
            terms = set(re.findall(r"\w+", query_text.casefold()))
            lexical = {
                document.reference: sum(
                    document.content.casefold().count(term)
                    + 3 * document.title.casefold().count(term)
                    for term in terms
                )
                for document in documents
            }
        maximum = max(lexical.values(), default=0) or 1
        results = []
        for document in documents:
            if document.reference not in scores:
                continue
            score = scores[document.reference]
            require(
                type(score) in (int, float) and math.isfinite(score),
                "Index returned an invalid score",
            )
            if lexical_weight:
                score = (1 - lexical_weight) * (
                    score + 1
                ) / 2 + lexical_weight * lexical.get(document.reference, 0) / maximum
            result = self._result(snapshot, document)
            result["excerpt"] = result.pop("content")[:400]
            result["score"] = score
            results.append(result)
        results = sorted(
            results, key=lambda item: (-item["score"], item["citation"]["document_id"])
        )[:top_k]
        self._track(snapshot, "search", results)
        return results

    def _semantic(
        self, query, *, top_k=10, weights=None, is_vector=False, lexical_weight=0
    ):
        self._authorize("semantic")
        snapshot = self._load("search")
        require(
            type(top_k) is int and 1 <= top_k <= MAX_RESULTS,
            "Limit must be between 1 and 100",
        )
        weights = normalized_weights(weights)
        require(
            type(lexical_weight) in (int, float)
            and math.isfinite(lexical_weight)
            and 0 <= lexical_weight <= 1,
            "Hybrid lexical weight must be between zero and one",
        )
        capability(
            self.vector_index is not None,
            "Configure a vector_index and call rebuild_index() for semantic search",
        )
        self.vector_index.check(snapshot, self.provider_id)
        if is_vector:
            query_vector = query
        else:
            require(
                isinstance(query, str) and 0 < len(query.strip()) <= MAX_QUERY_LENGTH,
                "Query must contain 1-1000 characters",
            )
            capability(
                self.embedding_provider is not None,
                "Semantic text search requires a host embedding_provider; vector queries can use an existing index",
            )
            query_vector = self.embedding_provider.generate_embedding(query)
        return self._rank_vectors(
            snapshot,
            query_vector,
            top_k=top_k,
            weights=weights,
            query_text=None if is_vector else query,
            lexical_weight=lexical_weight,
        )

    def semantic_search_content(self, query_text, top_k=10):
        return self._semantic(query_text, top_k=top_k, weights={"content": 1})

    def semantic_search_summary(self, query_text, top_k=10):
        return self._semantic(query_text, top_k=top_k, weights={"summary": 1})

    def multi_field_semantic_search(self, query_text, top_k=10, weights=None):
        return self._semantic(query_text, top_k=top_k, weights=weights)

    def vector_search_content(self, query_vector, top_k=10):
        return self._semantic(
            query_vector, top_k=top_k, weights={"content": 1}, is_vector=True
        )

    def vector_search_summary(self, query_vector, top_k=10):
        return self._semantic(
            query_vector, top_k=top_k, weights={"summary": 1}, is_vector=True
        )

    def multi_field_vector_search(self, query_vector, top_k=10, weights=None):
        return self._semantic(
            query_vector, top_k=top_k, weights=weights, is_vector=True
        )

    def hybrid_search(self, query_text, top_k=10, weights=None, *, lexical_weight=0.5):
        return self._semantic(
            query_text, top_k=top_k, weights=weights, lexical_weight=lexical_weight
        )

    def by_hierarchy_prefix(self, prefix):
        require(isinstance(prefix, str), "Hierarchy prefix must be text")
        snapshot = self._load("read")
        results = [
            self._result(snapshot, document)
            for document in self._active_pages(snapshot)
            if document.hierarchy_code.startswith(prefix)
        ]
        self._track(snapshot, "read", results)
        return sorted(
            results,
            key=lambda item: (item["hierarchy_code"], item["citation"]["document_id"]),
        )

    def get_by_hierarchy(self, hierarchy_code):
        require(
            isinstance(hierarchy_code, str) and bool(hierarchy_code.strip()),
            "A nonempty hierarchy code is required",
        )
        results = self.by_hierarchy_prefix(hierarchy_code)
        result = next(
            (
                result
                for result in results
                if result["hierarchy_code"] == hierarchy_code
            ),
            None,
        )
        require(result is not None, "Document is unavailable")
        return result

    def create_page(
        self,
        *,
        title,
        content,
        provenance,
        sources,
        reason,
        hierarchy_code=None,
        parent_code=None,
        section=None,
        summary="",
        document_id=None,
    ):
        self._authorize("history")
        self._authorize("evidence")
        snapshot = self._load("propose")
        records = self._records(snapshot)
        codes = {
            records[(head.document_id, head.revision)].hierarchy_code
            for head in snapshot.heads
        }
        code = allocate_code(
            codes,
            hierarchy_code=hierarchy_code,
            parent_code=parent_code,
            section=section,
        )
        document = Document(
            document_id or str(uuid4()),
            str(uuid4()),
            snapshot.scope,
            "page",
            title,
            content,
            "active",
            provenance,
            tuple(sources),
            summary=summary,
            hierarchy_code=code,
        )
        require(
            not any(
                head.document_id == document.document_id for head in snapshot.heads
            ),
            "Document already exists; use revise()",
        )
        proposal = self.propose((document,), reason=reason)
        require(
            proposal.base.revision == snapshot.revision,
            "Corpus changed; rebase placement",
        )
        return proposal

    def get_strategy(self):
        snapshot = self._load("read")
        records = self._records(snapshot)
        document = next(
            (
                records[(head.document_id, head.revision)]
                for head in snapshot.heads
                if records[(head.document_id, head.revision)].kind == "strategy"
                and records[(head.document_id, head.revision)].status == "active"
            ),
            None,
        )
        result = (
            self._result(snapshot, document)
            if document
            else {"content": "", "citation": None, "corpus_revision": snapshot.revision}
        )
        self._track(snapshot, "strategy", [result] if document else [])
        return result

    def propose_strategy(self, strategy, *, provenance, reason):
        self._authorize("history")
        self._authorize("evidence")
        snapshot = self._load("propose")
        records = self._records(snapshot)
        previous = next(
            (
                records[(head.document_id, head.revision)]
                for head in snapshot.heads
                if records[(head.document_id, head.revision)].kind == "strategy"
            ),
            None,
        )
        document = Document(
            previous.document_id if previous else "ergo:strategy",
            str(uuid4()),
            snapshot.scope,
            "strategy",
            "Organization strategy",
            strategy,
            "active",
            provenance,
        )
        proposal = self.propose((document,), reason=reason)
        require(
            proposal.base.revision == snapshot.revision,
            "Corpus changed; rebase strategy",
        )
        return proposal

    def propose_tree(
        self, prefix, title, description, *, provenance, reason, entries=()
    ):
        strategy = self.get_strategy()
        proposal = self.propose_strategy(
            strategy["content"] + tree_block(prefix, title, description, entries),
            provenance=provenance,
            reason=reason,
        )
        require(
            proposal.base.revision == strategy["corpus_revision"],
            "Corpus changed; rebase strategy",
        )
        return proposal

    def get_tree_status(self):
        snapshot = self._load("read")
        records = self._records(snapshot)
        strategy = next(
            (
                records[(head.document_id, head.revision)].content
                for head in snapshot.heads
                if records[(head.document_id, head.revision)].kind == "strategy"
                and records[(head.document_id, head.revision)].status == "active"
            ),
            "",
        )
        self._track(snapshot, "strategy")
        return tree_status(
            strategy,
            [document.hierarchy_code for document in self._active_pages(snapshot)],
        )

    def operations(self):
        self._load("history")
        reader = getattr(self.backend, "operations", None)
        require(
            callable(reader),
            "Backend has no operation log; inspect its host publication history",
        )
        return reader()
