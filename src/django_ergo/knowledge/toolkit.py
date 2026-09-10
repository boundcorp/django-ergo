"""Structural Toolkit implementation: cited reads and proposals, never self-approval."""

import json
from dataclasses import replace
from uuid import uuid4

from django_ergo.tool_config import ToolConfig

from .paths import tree_block
from .schema import Document
from .schema import Reference
from .schema import require

TOOLS = {
    "corpus_paths": ("List active pages under a logical path", ("prefix",)),
    "corpus_get_path": ("Read an active page by logical path", ("path",)),
    "corpus_navigation": ("Browse logical directories and pages", ("prefix",)),
    "corpus_suggest_move": (
        "Propose a path move retaining identity and history",
        ("document_id", "path"),
    ),
    "corpus_suggest_move_tree": (
        "Propose a logical subtree move",
        ("prefix", "destination"),
    ),
    "corpus_suggest_path_page": (
        "Propose a page at an explicit logical path",
        ("document_id", "title", "content", "summary", "path"),
    ),
    "corpus_semantic_search": (
        "Cited semantic search with configured provider/index",
        ("query", "weights_json"),
    ),
    "corpus_hybrid_search": (
        "Cited lexical and semantic search",
        ("query", "weights_json"),
    ),
    "corpus_hierarchy": ("Legacy compatibility: list pages by code", ("prefix",)),
    "corpus_get_strategy": ("Read the reviewed organization strategy", ()),
    "corpus_tree_status": ("Report active article counts for planned trees", ()),
    "corpus_suggest_strategy": ("Propose a reviewed strategy revision", ("strategy",)),
    "corpus_suggest_tree": (
        "Propose a logical path tree in the strategy",
        ("prefix", "title", "description"),
    ),
    "corpus_suggest_placed_page": (
        "Legacy compatibility: propose an unmapped page with a code",
        ("document_id", "title", "content", "summary", "hierarchy_code"),
    ),
    "corpus_search": ("Search active reviewed pages", ("query",)),
    "corpus_get": ("Read an active document", ("document_id",)),
    "corpus_resolve": (
        "Resolve an exact authorized historical citation",
        ("document_id", "revision", "digest"),
    ),
    "corpus_toc": ("List active reviewed pages", ()),
    "corpus_suggest_create": (
        "Legacy unmapped-page intake; prefer corpus_suggest_path_page",
        ("document_id", "title", "content"),
    ),
    "corpus_suggest_update": (
        "Propose a page correction using admitted evidence",
        ("document_id", "title", "content"),
    ),
    "corpus_suggest_archive": (
        "Propose withdrawal from retrieval, retaining history",
        ("document_id",),
    ),
}


class CorpusToolkit:
    """Compatible with extra_tools; host binds policy, capture provenance and sources."""

    def __init__(
        self,
        service,
        *,
        provenance=None,
        sources=(),
        proposal=None,
        reason="Agent suggestion",
    ):
        self.service = service
        self.provenance = provenance
        self.sources = tuple(sources)
        self.reason = reason
        self._proposal = proposal

    def has_tool(self, tool_name):
        return tool_name in TOOLS

    def get_tools_schema(self, adapter):
        return [
            adapter.to_engine_schema(
                ToolConfig(
                    name=name,
                    description=description,
                    parameters={
                        parameter: {"type": "string", "required": True}
                        for parameter in parameters
                    },
                    readonly="suggest" not in name,
                )
            )
            for name, (description, parameters) in TOOLS.items()
        ]

    def get_bound_knowledgebases(self):
        return []

    def record_usage(self, context_id):
        self.service = self.service.with_usage_context(context_id)
        self.service.record_usage(context_id, mode="read")

    def render_overview(self):
        return json.dumps(
            {
                "collection_id": self.service.backend.collection_id,
                "pages": self.service.table_of_contents(),
            }
        )

    def get_suggestions(self):
        return self.get_proposal().to_dict() if self._proposal else None

    def get_proposal(self):
        require(self._proposal is not None, "No suggestions")
        current = self.service.propose(self._proposal.changes, reason=self.reason)
        require(
            current.base.revision == self._proposal.base.revision,
            "Corpus changed; restart suggestions",
        )
        return self._proposal

    def clear_suggestions(self):
        self._proposal = None

    def _stage(self, document):
        changes = (
            tuple(
                existing
                for existing in self._proposal.changes
                if existing.document_id != document.document_id
            )
            if self._proposal
            else ()
        )
        proposal = self.service.propose(changes + (document,), reason=self.reason)
        if self._proposal:
            require(
                proposal.base.revision == self._proposal.base.revision,
                "Corpus changed; restart suggestions",
            )
        self._proposal = proposal
        return json.dumps(
            {
                "proposal_revision": proposal.revision,
                "document_id": document.document_id,
            }
        )

    def _suggest_metadata(self, tool_name, arguments):
        if tool_name in {"corpus_suggest_placed_page", "corpus_suggest_path_page"}:
            require(
                bool(arguments.get("path") or arguments.get("hierarchy_code")),
                "Placement requires a path or explicit legacy code",
            )
            document = Document(
                arguments["document_id"],
                str(uuid4()),
                self.service.backend.scope,
                "page",
                arguments["title"],
                arguments["content"],
                "active",
                self.provenance,
                self.sources,
                summary=arguments["summary"],
                hierarchy_code=arguments.get("hierarchy_code", ""),
                path=arguments.get("path", ""),
            )
            proposal = self.service.propose(
                (self._proposal.changes if self._proposal else ()) + (document,),
                reason=self.reason,
            )
            require(
                not any(
                    head.document_id == document.document_id
                    for head in proposal.base.heads
                ),
                "Document already exists; suggest an update",
            )
            return self._stage(document)
        if tool_name == "corpus_suggest_strategy":
            strategy = arguments["strategy"]
        else:
            pending = (
                next(
                    (
                        document
                        for document in self._proposal.changes
                        if document.kind == "strategy"
                    ),
                    None,
                )
                if self._proposal
                else None
            )
            strategy = (
                pending.content if pending else self.service.get_strategy()["content"]
            ) + tree_block(**arguments)
        proposal = self.service.propose_strategy(
            strategy, provenance=self.provenance, reason=self.reason
        )
        return self._stage(proposal.changes[0])

    def execute_tool(self, tool_name, arguments):
        require(tool_name in TOOLS, "Unknown corpus tool")
        require(set(arguments) == set(TOOLS[tool_name][1]), "Invalid tool arguments")
        readers = {
            "corpus_paths": lambda: self.service.by_path_prefix(arguments["prefix"]),
            "corpus_get_path": lambda: self.service.get_by_path(arguments["path"]),
            "corpus_navigation": lambda: self.service.navigation(arguments["prefix"]),
            "corpus_search": lambda: self.service.search(arguments["query"]),
            "corpus_get": lambda: self.service.get_document(arguments["document_id"]),
            "corpus_resolve": lambda: self.service.resolve(Reference(**arguments)),
            "corpus_toc": lambda: json.loads(self.render_overview()),
            "corpus_hierarchy": lambda: self.service.by_hierarchy_prefix(
                arguments["prefix"]
            ),
            "corpus_get_strategy": self.service.get_strategy,
            "corpus_tree_status": self.service.get_tree_status,
            "corpus_semantic_search": lambda: self.service.multi_field_semantic_search(
                arguments["query"], weights=json.loads(arguments["weights_json"])
            ),
            "corpus_hybrid_search": lambda: self.service.hybrid_search(
                arguments["query"], weights=json.loads(arguments["weights_json"])
            ),
        }
        if tool_name in readers:
            return json.dumps(readers[tool_name]())
        if tool_name == "corpus_suggest_move":
            return self._stage(
                self.service.move(
                    arguments["document_id"], arguments["path"], reason=self.reason
                ).changes[0]
            )
        if tool_name == "corpus_suggest_move_tree":
            proposal = self.service.move_tree(
                arguments["prefix"], arguments["destination"], reason=self.reason
            )
            for document in proposal.changes:
                self._stage(document)
            return json.dumps(self.get_proposal().to_dict())
        if tool_name in {
            "corpus_suggest_strategy",
            "corpus_suggest_tree",
            "corpus_suggest_placed_page",
            "corpus_suggest_path_page",
        }:
            return self._suggest_metadata(tool_name, arguments)
        if tool_name == "corpus_suggest_archive":
            proposal = self.service.revise(
                arguments["document_id"], status="archived", reason=self.reason
            )
            return self._stage(proposal.changes[0])
        if tool_name == "corpus_suggest_update":
            proposal = self.service.revise(
                arguments["document_id"],
                title=arguments["title"],
                content=arguments["content"],
                reason=self.reason,
            )
            document = proposal.changes[0]
            if self.sources:
                document = replace(document, sources=self.sources)
        else:
            require(self.provenance is not None, "Host capture provenance is required")
            document = Document(
                arguments["document_id"],
                str(uuid4()),
                self.service.backend.scope,
                "page",
                arguments["title"],
                arguments["content"],
                "active",
                self.provenance,
                self.sources,
            )
            proposal = self.service.propose(
                (self._proposal.changes if self._proposal else ()) + (document,),
                reason=self.reason,
            )
            require(
                not any(
                    head.document_id == document.document_id
                    for head in proposal.base.heads
                ),
                "Document already exists; suggest an update",
            )
        return self._stage(document)
