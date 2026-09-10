import builtins
import io
import json
import math
from dataclasses import replace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.database import DatabaseCorpus
from django_ergo.knowledge.ingestion import prepare_absorption
from django_ergo.knowledge.retrieval import CapabilityUnavailableError
from django_ergo.knowledge.retrieval import MemoryVectorIndex
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Document
from django_ergo.knowledge.schema import Provenance
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.service import AccessDeniedError
from django_ergo.knowledge.service import CorpusService
from django_ergo.knowledge.toolkit import CorpusToolkit
from django_ergo.knowledge.usage import DatabaseUsageStore
from django_ergo.knowledge.usage import MemoryUsageStore
from django_ergo.knowledge.usage import UsageRecordingError
from django_ergo.knowledge.usage_files import FileUsageStore
from tests.test_knowledge_corpus import approve
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import git
from tests.test_knowledge_corpus import host
from tests.test_knowledge_corpus import policy_for
from tests.test_knowledge_corpus import write_git_corpus
from tests.test_knowledge_writes import reviewed
from tests.test_knowledge_writes import writer

__all__ = ["corpus", "host", "writer"]


def test_original_v1_hashes_survive_extended_schema():
    document = Document(
        "doc",
        "1",
        "scope",
        "evidence",
        "Title",
        "Body",
        "active",
        Provenance("host:1", "1", "actor", "2026-09-09T12:00:00Z"),
    )
    assert (
        document.content_digest
        == "38b9418ae26f915188315154268f967b0df1d307b6e7d8ec409819fe8e43ea21"
    )
    assert (
        Snapshot("collection", "scope", (document,), (document.reference,)).revision
        == "21e1f7e725296af879dd23553c38a6053175cc27069728a0d284fef683269d7f"
    )


def test_convenience_writers_require_history_before_loading(advanced_service):
    service = advanced_service
    original = service.policy.allows
    service.policy.allows = lambda principal, collection_id, scope, action: (
        action != "history" and original(principal, collection_id, scope, action)
    )
    with patch.object(
        service.backend,
        "load",
        side_effect=AssertionError("History read before authorization"),
    ):
        with pytest.raises(AccessDeniedError):
            service.create_page(
                title="No",
                content="No",
                provenance=None,
                sources=(),
                reason="No",
                section="A",
            )
        with pytest.raises(AccessDeniedError):
            service.propose_strategy("No", provenance=None, reason="No")


@pytest.mark.parametrize("sink_kind", ["memory", "database", "file"])
def test_usage_sinks_work_with_every_corpus_adapter(
    advanced_service, sink_kind, request, tmp_path
):
    if sink_kind == "database":
        request.getfixturevalue("db")
        sink = DatabaseUsageStore()
    elif sink_kind == "file":
        sink = FileUsageStore(tmp_path / "usage.jsonl")
    else:
        sink = MemoryUsageStore()
    advanced_service.usage_store = sink
    event = advanced_service.record_usage("session:durable")
    sink.record(event)
    assert len(advanced_service.usage()) == 1
    with pytest.raises(CorpusError, match="identity conflict"):
        sink.record({**event, "actor": "changed"})
    assert sink.read(advanced_service.backend.collection_id, "foreign") == []
    if sink_kind == "file":
        assert FileUsageStore(tmp_path / "usage.jsonl").read(
            advanced_service.backend.collection_id, advanced_service.backend.scope
        ) == [event]


def test_git_publication_is_explicit_and_v2_roundtrips(corpus, host, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = write_git_corpus(repository, corpus)
    service = CorpusService(
        MemoryCorpus(source.load()),
        policy_for(host, corpus),
        host[3],
        usage_store=FileUsageStore(tmp_path / "usage.jsonl"),
    )
    proposal = service.propose_tree(
        "A",
        "Pumps",
        "Instructions",
        provenance=corpus.documents[0].provenance,
        reason="Organize",
    )
    service.apply(reviewed(service, proposal, host))
    assert source.load().revision == corpus.revision
    (repository / "ergo-source.yaml").write_text(json.dumps(service.export()["corpus"]))
    git(repository, "add", "ergo-source.yaml")
    git(repository, "commit", "-m", "Publish synthetic v2 fixture")
    published = CorpusService(
        source,
        service.policy,
        host[3],
        embedding_provider=TinyProvider(),
        vector_index=MemoryVectorIndex(),
        provider_id="tiny/v1",
        usage_store=FileUsageStore(tmp_path / "usage.jsonl"),
    )
    assert published.get_tree_status() == [{"path": "A", "article_count": 0}]
    assert published.get_strategy()["citation"] == service.get_strategy()["citation"]
    published.rebuild_index()
    assert published.semantic_search_content("pump")
    readonly = published.propose_strategy(
        "New strategy",
        provenance=corpus.documents[0].provenance,
        reason="No implicit commit",
    )
    with pytest.raises(CorpusError, match="read-only"):
        published.apply(reviewed(published, readonly, host))


def test_advanced_toolkit_intake_and_strategy_share_one_review_batch(
    writer, corpus, host
):
    toolkit = prepare_absorption(
        writer,
        content="pump water",
        title="Capture",
        provenance=corpus.documents[0].provenance,
        reason="Remember",
    )
    toolkit.execute_tool(
        "corpus_suggest_path_page",
        {
            "document_id": "placed",
            "title": "Pump",
            "content": "pump water",
            "summary": "Pump summary",
            "path": "A/pump.md",
        },
    )
    toolkit.execute_tool(
        "corpus_suggest_tree",
        {"prefix": "A", "title": "Pumps", "description": "Instructions"},
    )
    toolkit.execute_tool(
        "corpus_suggest_tree",
        {"prefix": "B", "title": "Wiring", "description": "Planned"},
    )
    writer.apply(reviewed(writer, toolkit.get_proposal(), host))
    assert writer.get_tree_status() == [
        {"path": "A", "article_count": 1},
        {"path": "B", "article_count": 0},
    ]
    writer.embedding_provider = TinyProvider()
    writer.provider_id = "tiny/v1"
    writer.vector_index = MemoryVectorIndex()
    writer.rebuild_index()
    toolkit.clear_suggestions()
    assert (
        json.loads(
            toolkit.execute_tool(
                "corpus_semantic_search",
                {"query": "pump", "weights_json": '{"summary": 1}'},
            )
        )[0]["citation"]["document_id"]
        == "placed"
    )


def test_advanced_commands_use_the_same_host_bound_service(advanced_service):
    with override_settings(ERGO_CORPORA={"host": lambda: advanced_service}):
        result = json.loads(
            call_command(
                "ergo_corpus",
                "host",
                "search",
                query="pump",
                mode="semantic",
                weights='{"summary": 1}',
                rebuild_index=True,
                stdout=io.StringIO(),
            )
        )
        assert result[0]["citation"]["document_id"] == "circuit"
        assert (
            json.loads(
                call_command(
                    "ergo_corpus", "host", "get_tree_status", stdout=io.StringIO()
                )
            )[0]["path"]
            == "planned"
        )
        assert json.loads(
            call_command(
                "ergo_corpus",
                "host",
                "usage",
                context="conversation:one",
                stdout=io.StringIO(),
            )
        )


def test_usage_failure_reports_whether_publication_already_committed(writer, host):
    proposal = reviewed(
        writer, writer.revise("reset", content="Correction", reason="Correct"), host
    )
    with (
        patch.object(
            writer.usage_store, "record", side_effect=RuntimeError("sink offline")
        ),
        pytest.raises(UsageRecordingError) as failure,
    ):
        writer.apply(proposal)
    assert failure.value.committed is True
    assert writer.operations()[-1]["result_revision"] == proposal.candidate.revision


def test_usage_context_binding_does_not_leak_between_toolkits(advanced_service):
    first = CorpusToolkit(advanced_service)
    second = CorpusToolkit(advanced_service)
    first.record_usage("one")
    second.record_usage("two")
    first.execute_tool("corpus_get_strategy", {})
    assert [event["mode"] for event in advanced_service.usage(context_id="one")] == [
        "read",
        "strategy",
    ]
    assert [event["mode"] for event in advanced_service.usage(context_id="two")] == [
        "read"
    ]


def test_extended_fields_cannot_be_smuggled_into_v1(advanced_corpus):
    payload = advanced_corpus.to_dict()
    payload["format"] = "ergo-corpus/v1"
    with pytest.raises(CorpusError, match="require ergo-corpus/v3"):
        Snapshot.from_dict(payload)


class TinyProvider:
    def __init__(self):
        self.calls = []

    def get_dimensions(self):
        return 2

    def generate_embedding(self, text):
        self.calls.append(text)
        return (
            [1.0, 0.0]
            if "pump" in text.lower() or "water" in text.lower()
            else [0.0, 1.0]
        )


@pytest.fixture
def advanced_corpus(corpus, host):
    first = approve(
        replace(
            corpus.documents[2],
            content="pump water",
            summary="electrical wiring",
            hierarchy_code="A0",
            path="pumps/reset.md",
            review=None,
        ),
        host[1],
        host[2],
    )
    second = approve(
        replace(
            first,
            document_id="circuit",
            title="Circuit",
            content="electrical wiring",
            summary="pump water",
            hierarchy_code="B0",
            path="wiring/circuit.md",
            review=None,
        ),
        host[1],
        host[2],
    )
    archived = replace(
        corpus.documents[3],
        hierarchy_code="A1",
        path="pumps/archived.md",
        summary="Archived secret",
    )
    strategy = approve(
        Document(
            "strategy",
            "v1",
            corpus.scope,
            "strategy",
            "Strategy",
            "### Path tree `pumps`: Pumps\n### Path tree `wiring`: Wiring\n### Path tree `planned`: Planned",
            "active",
            first.provenance,
        ),
        host[1],
        host[2],
    )
    return Snapshot(
        corpus.collection_id,
        corpus.scope,
        (corpus.documents[0], corpus.documents[1], first, second, archived, strategy),
        (
            corpus.documents[1].reference,
            first.reference,
            second.reference,
            archived.reference,
            strategy.reference,
        ),
    )


@pytest.fixture(params=["memory", "database", "git"])
def advanced_service(request, advanced_corpus, host, tmp_path):
    if request.param == "memory":
        backend = MemoryCorpus(advanced_corpus)
    elif request.param == "database":
        request.getfixturevalue("db")
        backend = DatabaseCorpus.workspace(advanced_corpus)
    else:
        backend = write_git_corpus(tmp_path, advanced_corpus)
    return CorpusService(
        backend,
        policy_for(host, advanced_corpus),
        host[3],
        embedding_provider=TinyProvider(),
        provider_id="tiny/v1",
        vector_index=MemoryVectorIndex(),
        usage_context="conversation:one",
    )


def test_common_semantic_weighted_hybrid_and_vector_apis(advanced_service):
    service = advanced_service
    assert (
        service.search("pump", weights={"summary": 1})[0]["citation"]["document_id"]
        == "circuit"
    )
    assert service.rebuild_index()["documents"] == 2
    assert all(
        "Archived" not in text and "Planned" not in text
        for text in service.embedding_provider.calls
    )
    methods = {
        "semantic_search_content": "reset",
        "semantic_search_summary": "circuit",
        "multi_field_semantic_search": "reset",
        "hybrid_search": "reset",
    }
    for method, expected in methods.items():
        results = getattr(service, method)("pump")
        assert results[0]["citation"]["document_id"] == expected
        assert (
            service.resolve(
                type(service.backend.load().heads[0])(**results[0]["citation"])
            )["status"]
            == "active"
        )
    assert (
        service.vector_search_content([1.0, 0.0])[0]["citation"]["document_id"]
        == "reset"
    )
    assert (
        service.vector_search_summary([1.0, 0.0])[0]["citation"]["document_id"]
        == "circuit"
    )
    assert (
        service.multi_field_vector_search(
            [1.0, 0.0], weights={"summary": 9, "content": 1}
        )[0]["citation"]["document_id"]
        == "circuit"
    )
    assert (
        service.search("pump", mode="semantic", weights={"summary": 1})[0]["citation"][
            "document_id"
        ]
        == "circuit"
    )
    assert (
        service.hybrid_search("pump", weights={"summary": 1}, lexical_weight=1)[0][
            "citation"
        ]["document_id"]
        == "reset"
    )
    assert all(
        result["citation"]["document_id"] != "withdrawn"
        for result in service.search("pump", mode="hybrid")
    )
    assert service.usage(context_id="conversation:one")
    assert all(
        event["corpus_revision"] == service.backend.load().revision
        for event in service.usage()
    )


def test_common_hierarchy_strategy_and_usage_read_apis(advanced_service):
    service = advanced_service
    assert [
        row["hierarchy_code"] for row in service.table_of_contents(prefix="pumps")
    ] == ["A0"]
    assert service.get_by_hierarchy("B0")["summary"] == "pump water"
    assert [
        row["citation"]["document_id"] for row in service.by_hierarchy_prefix("A")
    ] == ["reset"]
    assert service.get_tree_status() == [
        {"path": "planned", "article_count": 0},
        {"path": "pumps", "article_count": 1},
        {"path": "wiring", "article_count": 1},
    ]
    assert "Planned" in service.get_strategy()["content"]
    service.record_usage("session:two", mode="strategy")
    assert len(service.usage(context_id="session:two")) == 1
    assert service.usage(context_id="unrelated") == []


def test_denial_precedes_optional_provider_and_backend_access(advanced_service, host):
    service = advanced_service
    service.principal = host[4]
    with patch.object(
        service.backend, "load", side_effect=AssertionError("Unauthorized content read")
    ):
        for operation in (
            service.rebuild_index,
            lambda: service.semantic_search_content("pump"),
            service.get_strategy,
            service.get_tree_status,
            lambda: service.by_hierarchy_prefix("A"),
            lambda: service.record_usage("session"),
            service.usage,
        ):
            with pytest.raises(AccessDeniedError):
                operation()
    assert service.embedding_provider.calls == []
    assert (
        service.usage_store.read(service.backend.collection_id, service.backend.scope)
        == []
    )


def test_explicit_optional_capability_and_stale_index_errors(advanced_service):
    service = advanced_service
    with pytest.raises(CapabilityUnavailableError, match="not built"):
        service.semantic_search_content("pump")
    service.rebuild_index()
    service.provider_id = "changed/v2"
    with pytest.raises(CapabilityUnavailableError, match="fingerprint"):
        service.semantic_search_content("pump")
    service.provider_id = "tiny/v1"
    service.vector_index.projection = replace(
        service.vector_index.projection, scope="other"
    )
    with pytest.raises(CorpusError, match="scope"):
        service.semantic_search_content("pump")
    service.vector_index = None
    assert service.search("pump")
    with pytest.raises(CapabilityUnavailableError, match="Configure"):
        service.semantic_search_content("pump")


@pytest.mark.parametrize(
    "weights",
    [{}, {"bogus": 1}, {"content": -1}, {"content": math.nan}, {"content": 0}],
)
def test_invalid_search_weights_fail(advanced_service, weights):
    advanced_service.rebuild_index()
    with pytest.raises(CorpusError):
        advanced_service.multi_field_semantic_search("pump", weights=weights)


def test_invalid_vectors_and_missing_provider_fail_clearly(advanced_service):
    service = advanced_service
    service.rebuild_index()
    for vector in ([1], [0, 0], [math.inf, 1]):
        with pytest.raises(CorpusError):
            service.vector_search_content(vector)
    service.embedding_provider = None
    assert service.vector_search_content([1, 0])
    with pytest.raises(CapabilityUnavailableError, match="embedding_provider"):
        service.semantic_search_content("pump")


def test_governed_hierarchy_and_strategy_writes_retain_history(writer, corpus, host):
    service = writer
    proposal = service.create_page(
        title="Parent",
        content="pump parent",
        summary="summary",
        hierarchy_code="A",
        provenance=corpus.documents[0].provenance,
        sources=(corpus.documents[0].reference,),
        reason="Place parent",
    )
    service.apply(reviewed(service, proposal, host))
    citation = proposal.changes[0].reference
    child = service.create_page(
        title="Child",
        content="pump child",
        parent_code="A",
        provenance=corpus.documents[0].provenance,
        sources=(corpus.documents[0].reference,),
        reason="Place child",
    )
    assert child.changes[0].hierarchy_code == "A0"
    service.apply(reviewed(service, child, host))
    strategy = service.propose_legacy_tree(
        "A",
        "Pumps",
        "Instructions",
        entries=["Parent"],
        provenance=corpus.documents[0].provenance,
        reason="Organize",
    )
    service.apply(reviewed(service, strategy, host))
    assert service.get_tree_status(legacy=True) == [{"prefix": "A", "article_count": 2}]
    assert service.backend.load().to_dict()["format"] == "ergo-corpus/v2"
    assert service.resolve(citation)["summary"] == "summary"
    moved = service.revise(
        citation.document_id, hierarchy_code="B", summary="new summary", reason="Move"
    )
    service.apply(reviewed(service, moved, host))
    assert service.get_tree_status(legacy=True) == [{"prefix": "A", "article_count": 1}]
    assert service.resolve(citation)["hierarchy_code"] == "A"
    with pytest.raises(CorpusError, match="already exists"):
        service.create_page(
            title="Duplicate",
            content="pump",
            hierarchy_code="B",
            provenance=corpus.documents[0].provenance,
            sources=(corpus.documents[0].reference,),
            reason="Duplicate",
        )


def test_mutation_invalidates_derived_index_and_archive_never_leaks(writer, host):
    service = writer
    service.embedding_provider = TinyProvider()
    service.provider_id = "tiny/v1"
    service.vector_index = MemoryVectorIndex()
    service.rebuild_index()
    proposal = service.revise("reset", status="archived", reason="Withdraw")
    service.apply(reviewed(service, proposal, host))
    with pytest.raises(CapabilityUnavailableError, match="stale"):
        service.semantic_search_content("pump")
    assert service.search("pump") == []
    service.rebuild_index()
    assert service.semantic_search_content("pump") == []


@pytest.mark.django_db
def test_durable_usage_is_storage_independent_and_scope_isolated(advanced_service):
    service = advanced_service
    service.usage_store = DatabaseUsageStore()
    service.get_by_hierarchy("A0")
    service.record_usage("conversation:durable", mode="strategy")
    reopened = DatabaseUsageStore()
    assert len(reopened.read(service.backend.collection_id, service.backend.scope)) == 2
    assert reopened.read(service.backend.collection_id, "unrelated") == []
    assert service.usage(context_id="conversation:durable")[0]["mode"] == "strategy"


def test_all_common_read_features_can_run_without_file_io(advanced_corpus, host):
    service = CorpusService(
        MemoryCorpus(advanced_corpus),
        policy_for(host, advanced_corpus),
        host[3],
        embedding_provider=TinyProvider(),
        provider_id="tiny/v1",
        vector_index=MemoryVectorIndex(),
        usage_store=MemoryUsageStore(),
    )
    with (
        patch.object(builtins, "open", side_effect=AssertionError("Filesystem")),
        patch("pathlib.Path.open", side_effect=AssertionError("Filesystem")),
        patch("subprocess.run", side_effect=AssertionError("Git/process")),
    ):
        service.rebuild_index()
        assert service.hybrid_search("pump")
        assert service.get_tree_status()
        assert service.get_by_hierarchy("A0")
        assert service.usage()


def test_v1_roundtrip_remains_canonical(corpus):
    payload = corpus.to_dict()
    assert payload["format"] == "ergo-corpus/v1"
    assert all(
        "summary" not in document and "hierarchy_code" not in document
        for document in payload["documents"]
    )
    assert (
        Snapshot.from_dict(json.loads(json.dumps(payload))).revision == corpus.revision
    )
