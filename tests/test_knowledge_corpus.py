import builtins
import io
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.database import DatabaseCorpus
from django_ergo.knowledge.git import GitCorpus
from django_ergo.knowledge.models import CorpusRevision
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Document
from django_ergo.knowledge.schema import Provenance
from django_ergo.knowledge.schema import Review
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.schema import export_snapshot
from django_ergo.knowledge.schema import import_snapshot
from django_ergo.knowledge.service import AccessDeniedError
from django_ergo.knowledge.service import CorpusService
from examples.knowledge_hosts import MaintenancePolicy
from examples.knowledge_hosts import ProjectMember
from examples.knowledge_hosts import ProjectPolicy
from examples.knowledge_hosts import Technician


def approve(document, reviewer, version):
    return replace(
        document,
        review=Review(
            reviewer,
            "approved",
            "Checked source capture",
            version,
            document.content_digest,
        ),
    )


@pytest.fixture(params=["engineering", "maintenance"])
def host(request):
    if request.param == "engineering":
        return (
            "project:alpha",
            "engineering-review",
            "project/v1",
            ProjectMember("alice", ("project:alpha",), reviewer=True),
            ProjectMember("outsider", ("project:beta",), reviewer=True),
        )
    return (
        "acme:west",
        "maintenance-review",
        "site/v1",
        Technician("tech", "acme", ("west",), supervisor=True),
        Technician("outsider", "other-company", ("west",), supervisor=True),
    )


@pytest.fixture
def corpus(host):
    scope, reviewer, version, _principal, _outsider = host
    evidence = Document(
        "bulletin",
        "v1",
        scope,
        "evidence",
        "Pump bulletin",
        "Original pump reset instructions.",
        "active",
        Provenance("host:bulletin:42", "edition-1", "uploader", "2026-09-09T12:00:00Z"),
    )
    latest = replace(evidence, revision="v2", content="Revised pump instructions.")
    page = approve(
        Document(
            "reset",
            "v1",
            scope,
            "page",
            "Pump reset",
            "Reset the pump using the original bulletin.",
            "active",
            Provenance("host:review:9", "proposal-1", "editor", "2026-09-09T12:00:00Z"),
            (evidence.reference,),
        ),
        reviewer,
        version,
    )
    archived = replace(
        page,
        document_id="withdrawn",
        revision="v2",
        title="Archived pump secret",
        content="Do not retrieve this obsolete text.",
        status="archived",
        review=None,
    )
    return Snapshot(
        "handbook",
        scope,
        (evidence, latest, page, archived),
        (latest.reference, page.reference, archived.reference),
    )


def policy_for(host, corpus):
    scope, _reviewer, _version, _principal, _outsider = host
    approved = [
        document.content_digest for document in corpus.documents if document.review
    ]
    if scope.startswith("project:"):
        return ProjectPolicy(corpus.collection_id, scope, approved)
    return MaintenancePolicy(corpus.collection_id, "acme", "west", approved)


def git(repository, *arguments):
    return (
        subprocess.run(
            ["git", "-C", str(repository), *arguments], capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )


def write_git_corpus(repository, corpus):
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Corpus test")
    git(repository, "config", "user.email", "corpus@example.test")
    payload = corpus.to_dict()
    for index, record in enumerate(payload["documents"]):
        path = f"document-{index}.md"
        (repository / path).write_text(record.pop("content"), encoding="utf-8")
        record["content_path"] = path
    (repository / "ergo-source.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8"
    )
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Test-only corpus fixture")
    return GitCorpus(
        repository, ref="HEAD", collection_id=corpus.collection_id, scope=corpus.scope
    )


@pytest.fixture(params=["memory", "database", "git"])
def backend(request, corpus, tmp_path):
    if request.param == "memory":
        return MemoryCorpus(corpus)
    if request.param == "database":
        request.getfixturevalue("db")
        return DatabaseCorpus.store(corpus)
    return write_git_corpus(tmp_path, corpus)


@pytest.fixture
def service(backend, host, corpus):
    return CorpusService(backend, policy_for(host, corpus), host[3])


def test_shared_validation_and_cited_retrieval(service, corpus):
    assert service.validate()["revision"] == corpus.revision
    results = service.search("pump")
    assert len(results) == 1
    assert results[0]["citation"]["document_id"] == "reset"
    assert results[0]["corpus_revision"] == corpus.revision
    assert service.get_document("reset")["content"] == corpus.documents[2].content
    historical = service.resolve(corpus.documents[0].reference)
    assert historical["content"] == "Original pump reset instructions."
    assert historical["citation"]["revision"] == "v1"
    assert service.get_document("bulletin")["citation"]["revision"] == "v2"


def test_shared_denial_happens_before_backend_read(service, host):
    service.principal = host[4]
    with patch.object(
        service.backend,
        "load",
        side_effect=AssertionError("Source read before authorization"),
    ):
        for operation in (
            service.validate,
            lambda: service.search("pump"),
            lambda: service.get_document("reset"),
            service.export,
        ):
            with pytest.raises(AccessDeniedError):
                operation()
    assert all(not event.allowed for _actor, event in service.policy.events)


def test_shared_lifecycle_excludes_archived_get_search_and_supports_history(service):
    assert service.search("Archived") == []
    with pytest.raises(CorpusError, match="unavailable"):
        service.get_document("withdrawn")
    assert service.get_document("withdrawn", revision="v2")["status"] == "archived"


def test_history_and_evidence_require_separate_host_permissions(service, host):
    service.principal = replace(
        host[3],
        **(
            {"reviewer": False}
            if isinstance(host[3], ProjectMember)
            else {"supervisor": False}
        ),
    )
    assert service.search("pump")
    with pytest.raises(AccessDeniedError):
        service.get_document("withdrawn", revision="v2")
    with (
        patch.object(
            service.policy,
            "allows",
            side_effect=lambda _principal, _collection, _scope, action: (
                action != "evidence"
            ),
        ),
        pytest.raises(AccessDeniedError),
    ):
        service.get_document("bulletin")


def test_shared_exports_have_identical_schema_and_revision(service, corpus):
    assert import_snapshot(service.export()) == corpus
    assert service.export() == export_snapshot(corpus)
    assert service.backend.load().revision == corpus.revision


def test_shared_command_factory_carries_host_identity(service):
    with override_settings(ERGO_CORPORA={"handbook": lambda: service}):
        output = io.StringIO()
        call_command("ergo_corpus", "handbook", "search", query="pump", stdout=output)
        assert json.loads(output.getvalue())[0]["citation"]["document_id"] == "reset"
        call_command("ergo_corpus", "handbook", "validate", stdout=io.StringIO())
        call_command(
            "ergo_corpus", "handbook", "get", document="reset", stdout=io.StringIO()
        )
        call_command("ergo_corpus", "handbook", "export", stdout=io.StringIO())
        with pytest.raises(CommandError):
            call_command(
                "ergo_corpus",
                "handbook",
                "get",
                document="withdrawn",
                stdout=io.StringIO(),
            )


def test_virtual_backend_operations_never_open_files_or_run_git(corpus, host):
    with (
        patch.object(
            builtins, "open", side_effect=AssertionError("Filesystem dependency")
        ),
        patch.object(Path, "open", side_effect=AssertionError("Path dependency")),
        patch.object(
            subprocess, "run", side_effect=AssertionError("Process dependency")
        ),
    ):
        service = CorpusService(MemoryCorpus(corpus), policy_for(host, corpus), host[3])
        assert service.validate()
        assert service.search("pump")
        assert service.resolve(corpus.documents[0].reference)
        assert import_snapshot(service.export()) == corpus


@pytest.mark.parametrize(
    "mutation",
    [
        "scope",
        "citation",
        "approval",
        "identity",
        "format",
        "duplicate",
        "head",
        "provenance",
    ],
)
def test_common_validation_rejects_invalid_corpora(corpus, mutation):
    payload = corpus.to_dict()
    page = next(
        record for record in payload["documents"] if record["document_id"] == "reset"
    )
    if mutation == "scope":
        page["scope"] = "another-scope"
    elif mutation == "citation":
        page["sources"][0]["revision"] = "unknown"
    elif mutation == "approval":
        page["content"] = "Changed after approval"
    elif mutation == "identity":
        page["document_id"] = ""
    elif mutation == "format":
        payload["format"] = "unknown/v99"
    elif mutation == "duplicate":
        payload["documents"].append(page)
    elif mutation == "head":
        payload["heads"].pop()
    elif mutation == "provenance":
        page["provenance"]["source_revision"] = ""
    with pytest.raises(CorpusError):
        Snapshot.from_dict(payload)


def test_untrusted_review_is_not_an_authorization_grant(corpus, host):
    policy = policy_for(host, corpus)
    policy.approved_digests = frozenset()
    service = CorpusService(MemoryCorpus(corpus), policy, host[3])
    with pytest.raises(CorpusError, match="Host does not accept"):
        service.search("pump")


def test_backend_cannot_claim_another_scope(corpus, host):
    backend = MemoryCorpus(replace(corpus, collection_id="other"))
    backend.collection_id = corpus.collection_id
    service = CorpusService(backend, policy_for(host, corpus), host[3])
    with pytest.raises(CorpusError, match="Backend identity mismatch"):
        service.validate()


def test_bad_citation_digest_is_rejected(service, corpus):
    with pytest.raises(CorpusError, match="digest mismatch"):
        service.resolve(replace(corpus.documents[0].reference, digest="incorrect"))


@pytest.mark.django_db
def test_database_persistence_rebuild_and_integrity(corpus, host):
    backend = DatabaseCorpus.store(corpus)
    DatabaseCorpus.store(corpus)
    assert CorpusRevision.objects.count() == 1
    reloaded = DatabaseCorpus(
        collection_id=corpus.collection_id, scope=corpus.scope, revision=corpus.revision
    )
    assert reloaded.load() == backend.load()
    CorpusRevision.objects.update(
        payload=replace(corpus, collection_id="tampered").to_dict()
    )
    with pytest.raises(CorpusError, match="integrity mismatch"):
        reloaded.load()


def test_git_historical_deleted_path_and_uncommitted_edits(tmp_path, corpus):
    backend = write_git_corpus(tmp_path, corpus)
    original_commit = git(tmp_path, "rev-parse", "HEAD")
    manifest = tmp_path / "ergo-source.yaml"
    payload = yaml.safe_load(manifest.read_text())
    historical = payload["documents"][0]
    old_path = tmp_path / historical["content_path"]
    old_path.unlink()
    historical["content_commit"] = original_commit
    manifest.write_text(yaml.safe_dump(payload))
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Test-only historical path deletion")
    assert backend.load() == corpus
    manifest.write_text("uncommitted invalid content")
    assert backend.load() == corpus


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "wiki/../escape", "wiki\\escape"]
)
def test_git_rejects_unsafe_paths(tmp_path, corpus, path):
    with pytest.raises(CorpusError):
        GitCorpus(
            tmp_path,
            ref="HEAD",
            collection_id=corpus.collection_id,
            scope=corpus.scope,
            manifest=path,
        )


def test_import_export_detects_changed_revision(corpus):
    envelope = export_snapshot(corpus)
    envelope["revision"] = "altered"
    with pytest.raises(CorpusError, match="revision mismatch"):
        import_snapshot(envelope)


@pytest.mark.parametrize("bad_status", [None, [], {}, "unknown"])
def test_malformed_status_is_a_validation_error(corpus, bad_status):
    payload = corpus.to_dict()
    payload["documents"][0]["status"] = bad_status
    with pytest.raises(CorpusError):
        Snapshot.from_dict(payload)


def test_capture_timestamp_requires_timezone(corpus):
    payload = corpus.to_dict()
    payload["documents"][0]["provenance"]["captured_at"] = "yesterday"
    with pytest.raises(CorpusError, match="timestamp"):
        Snapshot.from_dict(payload)


def test_snapshot_revision_is_independent_of_adapter_record_order(corpus):
    shuffled = replace(
        corpus,
        documents=tuple(reversed(corpus.documents)),
        heads=tuple(reversed(corpus.heads)),
    )
    assert shuffled.revision == corpus.revision


def test_git_rejects_symlink_body(tmp_path, corpus):
    backend = write_git_corpus(tmp_path, corpus)
    payload = yaml.safe_load((tmp_path / "ergo-source.yaml").read_text())
    path = tmp_path / payload["documents"][0]["content_path"]
    path.unlink()
    path.symlink_to("ergo-source.yaml")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Test-only symlink fixture")
    with pytest.raises(CorpusError):
        backend.load()
