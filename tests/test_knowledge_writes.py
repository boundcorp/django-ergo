import builtins
import io
import json
from dataclasses import asdict
from dataclasses import replace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings

from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.changes import Proposal
from django_ergo.knowledge.database import DatabaseCorpus
from django_ergo.knowledge.ingestion import prepare_absorption
from django_ergo.knowledge.models import CorpusOperation
from django_ergo.knowledge.models import CorpusRevision
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Review
from django_ergo.knowledge.schema import import_snapshot
from django_ergo.knowledge.service import AccessDeniedError
from django_ergo.knowledge.service import CorpusService
from django_ergo.knowledge.toolkit import CorpusToolkit
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import host
from tests.test_knowledge_corpus import policy_for
from tests.test_knowledge_corpus import write_git_corpus

__all__ = ["corpus", "host"]


@pytest.fixture(params=["memory", "database", "git-staging"])
def writer(request, corpus, host, tmp_path):
    if request.param == "database":
        request.getfixturevalue("db")
        backend = DatabaseCorpus.workspace(corpus)
    elif request.param == "git-staging":
        backend = MemoryCorpus(write_git_corpus(tmp_path, corpus).load())
    else:
        backend = MemoryCorpus(corpus)
    return CorpusService(backend, policy_for(host, corpus), host[3])


def reviewed(service, proposal, host, *, decision="approved"):
    reviews = [
        Review(
            host[1],
            decision,
            "Checked evidence and change",
            host[2],
            document.content_digest,
        )
        for document in proposal.changes
    ]
    service.policy.approved_digests |= frozenset(
        review.reviewed_digest for review in reviews
    )
    service.policy.reviewed_proposals.add(
        replace(
            proposal,
            changes=tuple(
                replace(document, review=review)
                for document, review in zip(proposal.changes, reviews, strict=True)
            ),
        ).revision
    )
    return service.review(proposal, reviews)


def test_absorb_review_apply_correct_archive_and_cite(writer, corpus, host):
    toolkit = prepare_absorption(
        writer,
        content="Prefer morning deployments.",
        title="Host conversation",
        provenance=corpus.documents[0].provenance,
        reason="Explicit remember request",
    )
    toolkit.execute_tool(
        "corpus_suggest_create",
        {
            "document_id": "preferences",
            "title": "Deploy preferences",
            "content": "Prefer morning deployments.",
        },
    )
    proposal = Proposal.from_dict(json.loads(json.dumps(toolkit.get_suggestions())))
    assert writer.search("deployments") == []
    with pytest.raises(CorpusError, match="approval"):
        writer.apply(proposal)
    event = writer.apply(reviewed(writer, proposal, host))
    citation = proposal.changes[1].reference
    assert event["actor"] == host[3].username
    assert event["reason"] == "Explicit remember request"
    assert writer.search("deployments")[0]["citation"]["digest"] == citation.digest
    assert (
        writer.resolve(proposal.changes[0].reference)["content"]
        == "Prefer morning deployments."
    )
    correction = writer.revise(
        "preferences", content="Prefer afternoon deployments.", reason="User correction"
    )
    writer.apply(reviewed(writer, correction, host))
    assert writer.resolve(citation)["content"] == "Prefer morning deployments."
    assert (
        writer.get_document("preferences")["content"] == "Prefer afternoon deployments."
    )
    archive = writer.revise(
        "preferences", status="archived", reason="Stop using this memory"
    )
    writer.apply(reviewed(writer, archive, host))
    assert writer.search("deployments") == []
    assert "preferences" not in {
        row["document_id"] for row in writer.table_of_contents()
    }
    with pytest.raises(CorpusError, match="unavailable"):
        writer.get_document("preferences")
    assert writer.resolve(citation)["content"] == "Prefer morning deployments."
    assert len(writer.operations()) == 3
    restored = MemoryCorpus(import_snapshot(writer.export()))
    assert restored.load().revision == writer.validate()["revision"]


def test_draft_publication_rejection_and_tamper(writer, corpus, host):
    draft = replace(
        corpus.documents[2],
        document_id="draft",
        revision="one",
        status="draft",
        review=None,
    )
    proposal = writer.propose((draft,), reason="Capture draft")
    rejected = reviewed(writer, proposal, host, decision="rejected")
    with pytest.raises(CorpusError, match="approval"):
        writer.apply(rejected)
    approved = reviewed(writer, proposal, host)
    tampered = replace(
        approved,
        changes=(replace(approved.changes[0], content="Changed after review"),),
    )
    with pytest.raises(CorpusError, match="Review does not match"):
        writer.apply(tampered)
    writer.apply(approved)
    assert not any(
        row["citation"]["document_id"] == "draft" for row in writer.search("pump")
    )
    publish = writer.revise("draft", status="active", reason="Publish reviewed draft")
    writer.apply(reviewed(writer, publish, host))
    assert writer.get_document("draft")["status"] == "active"


def test_conflicts_scope_and_history_rewrite_fail_closed(writer, corpus, host):
    first = reviewed(
        writer,
        writer.revise("reset", content="First change", reason="Correction"),
        host,
    )
    second = reviewed(
        writer,
        writer.revise("reset", content="Second change", reason="Correction"),
        host,
    )
    writer.apply(first)
    with pytest.raises(CorpusError, match="rebase"):
        writer.apply(second)
    with pytest.raises(CorpusError, match="scope mismatch"):
        writer.propose(
            (replace(corpus.documents[0], revision="v3", scope="other"),), reason="No"
        )
    with pytest.raises(CorpusError, match="Duplicate document revision"):
        writer.propose(
            (replace(corpus.documents[0], content="Overwrite"),), reason="No"
        )
    payload = first.to_dict()
    payload["candidate"]["documents"][0]["content"] = "Rewrite retained evidence"
    with pytest.raises(CorpusError):
        Proposal.from_dict(payload)
    assert len(writer.operations()) == 1


def test_denied_write_actions_do_not_read_or_mutate(writer, corpus, host):
    proposal = writer.revise("reset", content="Changed", reason="Correction")
    writer.principal = host[4]
    with patch.object(
        writer.backend, "load", side_effect=AssertionError("Unauthorized read")
    ):
        for operation in (
            lambda: writer.propose(proposal.changes, reason="No"),
            lambda: writer.intake(
                content="No",
                title="No",
                provenance=corpus.documents[0].provenance,
                reason="No",
            ),
            lambda: writer.revise("reset", status="archived", reason="No"),
            lambda: writer.review(proposal, []),
            lambda: writer.apply(proposal),
            writer.operations,
        ):
            with pytest.raises(AccessDeniedError):
                operation()


def test_writer_cannot_approve_or_apply_without_host_grants(writer, host):
    proposal = reviewed(
        writer, writer.revise("reset", content="Changed", reason="Correction"), host
    )
    original = writer.policy.allows
    writer.policy.allows = lambda principal, collection_id, scope, action: (
        action not in {"review", "apply"}
        and original(principal, collection_id, scope, action)
    )
    with pytest.raises(AccessDeniedError):
        writer.review(proposal, [proposal.changes[0].review])
    with pytest.raises(AccessDeniedError):
        writer.apply(proposal)
    assert writer.backend.load().revision == proposal.base.revision


def test_memory_writes_never_use_files_or_processes(corpus, host):
    service = CorpusService(MemoryCorpus(corpus), policy_for(host, corpus), host[3])
    with (
        patch.object(builtins, "open", side_effect=AssertionError("File access")),
        patch("pathlib.Path.open", side_effect=AssertionError("Path access")),
        patch("subprocess.run", side_effect=AssertionError("Process access")),
    ):
        toolkit = prepare_absorption(
            service,
            content="Virtual input",
            title="Upload",
            provenance=corpus.documents[0].provenance,
            reason="Remember",
        )
        toolkit.execute_tool(
            "corpus_suggest_create",
            {"document_id": "virtual", "title": "Virtual", "content": "Virtual memory"},
        )
        service.apply(reviewed(service, toolkit.get_proposal(), host))
        assert service.search("virtual")
        assert service.operations()


@pytest.mark.django_db
def test_database_cas_rolls_back_failed_log_and_reopens(corpus, host):
    backend = DatabaseCorpus.workspace(corpus)
    service = CorpusService(backend, policy_for(host, corpus), host[3])
    proposal = reviewed(
        service, service.revise("reset", content="Durable", reason="Correct"), host
    )
    with (
        patch.object(CorpusOperation, "save", side_effect=RuntimeError("log failure")),
        pytest.raises(RuntimeError, match="log failure"),
    ):
        service.apply(proposal)
    assert backend.load().revision == corpus.revision
    assert CorpusRevision.objects.count() == 1
    service.apply(proposal)
    assert (
        DatabaseCorpus.workspace(corpus).load().revision == proposal.candidate.revision
    )
    with pytest.raises(CorpusError, match="rebase"):
        DatabaseCorpus.workspace(corpus).commit(
            corpus, expected_revision=corpus.revision, event={}
        )
    assert DatabaseCorpus.store(corpus).load().revision == corpus.revision


def test_virtual_management_intake_review_apply(writer, corpus, host):
    intake = {
        "content": "Command input",
        "title": "Upload",
        "provenance": asdict(corpus.documents[0].provenance),
        "reason": "Explicit intake",
    }
    with override_settings(ERGO_CORPORA={"host": lambda: writer}):
        with patch("sys.stdin", io.StringIO(json.dumps(intake))):
            proposed = json.loads(
                call_command("ergo_corpus", "host", "intake", stdout=io.StringIO())
            )
        proposal = Proposal.from_dict(proposed)
        review = reviewed(writer, proposal, host).changes[0].review
        with patch(
            "sys.stdin",
            io.StringIO(
                json.dumps({"proposal": proposed, "reviews": [asdict(review)]})
            ),
        ):
            approved = call_command(
                "ergo_corpus", "host", "review", stdout=io.StringIO()
            )
        with patch("sys.stdin", io.StringIO(approved)):
            result = json.loads(
                call_command("ergo_corpus", "host", "apply", stdout=io.StringIO())
            )
        assert result["base_revision"] == corpus.revision


def test_approval_receipt_binds_reason_and_base(writer, host):
    proposal = reviewed(
        writer, writer.revise("reset", content="Corrected", reason="Correction"), host
    )
    with pytest.raises(CorpusError, match="proposal receipt"):
        writer.apply(replace(proposal, reason="Forged justification"))
    unrelated = reviewed(
        writer,
        writer.revise("withdrawn", title="Renamed", reason="Metadata correction"),
        host,
    )
    writer.apply(unrelated)
    rebased = replace(proposal, base=writer.backend.load())
    with pytest.raises(CorpusError, match="proposal receipt"):
        writer.apply(rebased)


def test_toolkit_schema_and_update_withdraw(writer, corpus, host):
    class Adapter:
        def to_engine_schema(self, config):
            return asdict(config)

    toolkit = CorpusToolkit(writer)
    assert len(toolkit.get_tools_schema(Adapter())) == 21
    assert not toolkit.has_tool("corpus_apply")
    assert toolkit.get_bound_knowledgebases() == []
    assert (
        json.loads(
            toolkit.execute_tool(
                "corpus_resolve", asdict(corpus.documents[2].reference)
            )
        )["title"]
        == "Pump reset"
    )
    toolkit.execute_tool(
        "corpus_suggest_update",
        {
            "document_id": "reset",
            "title": "Updated pump",
            "content": "Updated instructions",
        },
    )
    writer.apply(reviewed(writer, toolkit.get_proposal(), host))
    toolkit.clear_suggestions()
    toolkit.execute_tool("corpus_suggest_archive", {"document_id": "reset"})
    writer.apply(reviewed(writer, toolkit.get_proposal(), host))
    assert json.loads(toolkit.execute_tool("corpus_search", {"query": "pump"})) == []
