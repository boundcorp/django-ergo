import json
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.changes import Proposal
from django_ergo.knowledge.retrieval import MemoryVectorIndex
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Document
from django_ergo.knowledge.schema import Provenance
from django_ergo.knowledge.schema import Review
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.schema import import_snapshot
from django_ergo.knowledge.service import AccessDeniedError
from django_ergo.knowledge.service import CorpusService
from django_ergo.knowledge.toolkit import CorpusToolkit
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import host
from tests.test_knowledge_corpus import policy_for
from tests.test_knowledge_corpus import write_git_corpus
from tests.test_knowledge_features import TinyProvider
from tests.test_knowledge_writes import reviewed
from tests.test_knowledge_writes import writer

__all__ = ["corpus", "host", "writer"]
pytestmark = pytest.mark.django_db


def create(service, corpus, path, **kwargs):
    return service.create_page(
        path=path,
        title="Pump",
        content="pump water",
        summary="summary",
        sources=(corpus.documents[0].reference,),
        provenance=corpus.documents[0].provenance,
        reason="Place explicitly",
        **kwargs,
    )


def test_v2_receipts_and_export_hashes_match_pre_path_wheel():
    provenance = Provenance("host:fixture", "1", "actor", "2026-09-09T12:00:00Z")
    evidence = Document(
        "evidence", "1", "scope", "evidence", "Evidence", "Source", "active", provenance
    )
    page = Document(
        "page",
        "1",
        "scope",
        "page",
        "Page",
        "Body",
        "active",
        provenance,
        (evidence.reference,),
        summary="Summary",
        hierarchy_code="A0",
    )
    page = replace(
        page,
        review=Review("reviewer", "approved", "Checked", "v1", page.content_digest),
    )
    snapshot = Snapshot(
        "collection", "scope", (evidence, page), (evidence.reference, page.reference)
    )
    proposal = Proposal(
        snapshot,
        (replace(page, revision="2", content="Revised", review=None),),
        "actor",
        "Update",
    )
    assert snapshot.to_dict()["format"] == "ergo-corpus/v2"
    assert (
        page.content_digest
        == "b6c21fed8f4cb8f2617c9b7d8fb57a3482dfb41c5802f0992e7cd86314ac9065"
    )
    assert (
        snapshot.revision
        == "bd636aea180cc56fe4c808e8f9a11c78a86a5dd656c6a18edb67bd65a9bb15cf"
    )
    assert (
        proposal.revision
        == "207ff9ccc0a7160fbf0ac7c62bcdcb6b3f4de6a1661a44df0c704e13e75d4cc3"
    )


def test_paths_moves_navigation_citations_and_index_parity(
    writer, corpus, host, tmp_path
):
    proposal = create(writer, corpus, "guides/pump.md")
    assert Proposal.from_dict(proposal.to_dict()).revision == proposal.revision
    writer.apply(reviewed(writer, proposal, host))
    reference = proposal.changes[0].reference
    writer.apply(reviewed(writer, create(writer, corpus, "guides-other/pump.md"), host))
    assert len(writer.by_path_prefix("guides")) == 1
    assert writer.get_by_path("guides/pump.md")["hierarchy_code"] == ""
    assert "guides" in writer.navigation()["directories"]
    assert writer.table_of_contents(prefix="guides")[0]["path"] == "guides/pump.md"
    writer.embedding_provider = TinyProvider()
    writer.provider_id = "tiny/v1"
    writer.vector_index = MemoryVectorIndex()
    writer.rebuild_index()
    moved = writer.move(
        reference.document_id, "manuals/pump.md", reason="Reviewed rename"
    )
    writer.apply(reviewed(writer, moved, host))
    assert writer.resolve(reference)["path"] == "guides/pump.md"
    assert (
        writer.get_by_path("manuals/pump.md")["citation"]["document_id"]
        == reference.document_id
    )
    assert writer.by_path_prefix("guides") == []
    with pytest.raises(CorpusError, match="stale"):
        writer.hybrid_search("pump")
    writer.rebuild_index()
    assert any(
        item["path"] == "manuals/pump.md" for item in writer.hybrid_search("pump")
    )
    exported = writer.export()
    assert exported["corpus"]["format"] == "ergo-corpus/v3"
    assert import_snapshot(exported).revision == exported["revision"]
    repository = tmp_path / "published"
    repository.mkdir()
    backend = write_git_corpus(repository, import_snapshot(exported))
    published = CorpusService(backend, writer.policy, host[3])
    assert (
        published.get_by_path("manuals/pump.md")["citation"]
        == writer.get_by_path("manuals/pump.md")["citation"]
    )
    assert published.resolve(reference)["path"] == "guides/pump.md"
    assert writer.usage()


def test_explicit_placement_tree_move_and_archive_reservations(writer, corpus, host):
    proposal = writer.create_page(
        parent_path="manuals",
        name="pump.md",
        title="Pump",
        content="pump",
        provenance=corpus.documents[0].provenance,
        sources=(corpus.documents[0].reference,),
        reason="Explicit placement",
    )
    writer.apply(reviewed(writer, proposal, host))
    reference = proposal.changes[0].reference
    strategy = writer.propose_tree(
        "manuals",
        "Manuals",
        "Host layout",
        provenance=corpus.documents[0].provenance,
        reason="Plan layout",
    )
    writer.apply(reviewed(writer, strategy, host))
    assert writer.get_tree_status() == [{"path": "manuals", "article_count": 1}]
    moved = writer.move_tree("manuals", "reference", reason="Move subtree")
    assert {item.kind for item in moved.changes} == {"page", "strategy"}
    writer.apply(reviewed(writer, moved, host))
    assert writer.get_tree_status() == [{"path": "reference", "article_count": 1}]
    assert (
        writer.get_by_path("reference/pump.md")["citation"]["document_id"]
        == reference.document_id
    )
    writer.apply(
        reviewed(
            writer,
            writer.revise(reference.document_id, status="archived", reason="Withdraw"),
            host,
        )
    )
    assert writer.by_path_prefix("reference") == []
    with pytest.raises(CorpusError, match="Duplicate document path"):
        create(writer, corpus, "reference/pump.md")
    assert writer.resolve(reference)["path"] == "manuals/pump.md"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/file",
        "../file",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "a\x00b",
        "a\nb",
        "a/ b",
        "a/b ",
        "a/%2e%2e/b",
        "C:/x",
        "e\u0301",
        "x" * 1025,
    ],
)
def test_invalid_paths_fail_without_files(corpus, host, path):
    service = CorpusService(MemoryCorpus(corpus), policy_for(host, corpus), host[3])
    with (
        patch("builtins.open", side_effect=AssertionError("No files")),
        patch("subprocess.run", side_effect=AssertionError("No processes")),
        pytest.raises(CorpusError),
    ):
        create(service, corpus, path)


def test_code_metadata_is_not_path_identity_and_old_exports_keep_hashes(
    writer, corpus, host
):
    old = corpus.to_dict()
    assert Snapshot.from_dict(old).revision == corpus.revision
    assert all("path" not in record for record in old["documents"])
    first = create(writer, corpus, "one.md", hierarchy_code="A")
    writer.apply(reviewed(writer, first, host))
    second = create(writer, corpus, "two.md", hierarchy_code="A")
    writer.apply(reviewed(writer, second, host))
    assert len(writer.by_hierarchy_prefix("A")) == 2
    with pytest.raises(CorpusError, match="ambiguous"):
        writer.get_by_hierarchy("A")
    assert (
        writer.get_by_path("one.md")["citation"]
        != writer.get_by_path("two.md")["citation"]
    )
    with pytest.raises(CorpusError, match="Duplicate document path"):
        writer.move(second.changes[0].document_id, "one.md", reason="Collision")
    tampered = writer.export()["corpus"]
    tampered["format"] = "ergo-corpus/v2"
    with pytest.raises(CorpusError, match="v3"):
        Snapshot.from_dict(tampered)
    approved = reviewed(
        writer, writer.move(first.changes[0].document_id, "new.md", reason="Move"), host
    )
    with pytest.raises(CorpusError, match="Review does not match"):
        writer.apply(
            replace(
                approved, changes=(replace(approved.changes[0], path="tampered.md"),)
            )
        )


def test_path_denial_and_scope_checks(writer, corpus, host):
    writer.principal = host[4]
    with patch.object(
        writer.backend, "load", side_effect=AssertionError("Denied content read")
    ):
        for action in (
            lambda: writer.get_by_path("a.md"),
            writer.navigation,
            lambda: writer.move("id", "a.md", reason="No"),
            lambda: writer.move_tree("old", "new", reason="No"),
        ):
            with pytest.raises(AccessDeniedError):
                action()
    writer.principal = host[3]
    proposal = create(writer, corpus, "a.md")
    with pytest.raises(CorpusError, match="scope"):
        writer.propose(
            (replace(proposal.changes[0], scope="other"),), reason="No promotion"
        )


def test_path_toolkit_commands_and_no_fs_operations(writer, corpus, host):
    toolkit = CorpusToolkit(
        writer,
        provenance=corpus.documents[0].provenance,
        sources=(corpus.documents[0].reference,),
    )
    with (
        patch("builtins.open", side_effect=AssertionError("No files")),
        patch("subprocess.run", side_effect=AssertionError("No Git")),
    ):
        toolkit.execute_tool(
            "corpus_suggest_path_page",
            {
                "document_id": "tool-page",
                "title": "Pump",
                "content": "pump",
                "summary": "summary",
                "path": "guides/pump.md",
            },
        )
        writer.apply(reviewed(writer, toolkit.get_proposal(), host))
        toolkit.clear_suggestions()
        assert (
            json.loads(
                toolkit.execute_tool("corpus_get_path", {"path": "guides/pump.md"})
            )["path"]
            == "guides/pump.md"
        )
        toolkit.execute_tool(
            "corpus_suggest_move", {"document_id": "tool-page", "path": "help/pump.md"}
        )
        writer.apply(reviewed(writer, toolkit.get_proposal(), host))
        assert json.loads(
            toolkit.execute_tool("corpus_navigation", {"prefix": "help"})
        )["pages"]
    with override_settings(ERGO_CORPORA={"host": lambda: writer}):
        assert (
            json.loads(
                call_command(
                    "ergo_corpus",
                    "host",
                    "get_path",
                    path="help/pump.md",
                    stdout=StringIO(),
                )
            )["path"]
            == "help/pump.md"
        )
