from dataclasses import replace
from uuid import uuid4

import pytest
from django.db import IntegrityError
from django.db import connection
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor

from django_ergo.knowledge.articles import ArticleCompatibility
from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.service import CorpusService
from django_ergo.models import Article
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import host
from tests.test_knowledge_corpus import policy_for
from tests.test_knowledge_legacy import legacy_kbs
from tests.test_knowledge_writes import reviewed

__all__ = ["corpus", "host", "legacy_kbs"]
pytestmark = pytest.mark.django_db


def test_article_mapping_moves_and_duplicate_codes_are_not_path_collisions(
    legacy_kbs, corpus, host
):
    _owner, kb, article, archived, foreign = legacy_kbs
    service = CorpusService(
        MemoryCorpus(Snapshot("paths", corpus.scope, (), ())),
        policy_for(host, Snapshot("paths", corpus.scope, (), ())),
        host[3],
    )
    bridge = ArticleCompatibility(service, kb, lambda *args: True)
    captured = bridge.propose_import(
        provenance=corpus.documents[0].provenance, reason="Capture unmapped"
    )
    assert all(not document.path for document in captured.changes)
    assert not article.relative_path
    proposal = bridge.propose_import(
        provenance=corpus.documents[0].provenance,
        reason="Explicit mapping",
        path_mapping={
            str(article.pk): "manuals/pump.md",
            str(archived.pk): "history/withdrawn.md",
        },
    )
    service.apply(reviewed(service, proposal, host))
    reference = next(
        document.reference
        for document in proposal.changes
        if document.document_id == str(article.pk)
    )
    bridge.publish()
    article.refresh_from_db()
    assert article.relative_path == "manuals/pump.md"
    move = service.move(str(article.pk), "guides/pump.md", reason="Rename")
    service.apply(reviewed(service, move, host))
    bridge.publish()
    article.refresh_from_db()
    assert article.relative_path == "guides/pump.md"
    assert service.resolve(reference)["path"] == "manuals/pump.md"
    sources = next(
        document.sources
        for document in proposal.changes
        if document.document_id == str(article.pk)
    )
    created = service.create_page(
        path="guides/other.md",
        hierarchy_code=article.hierarchy_code,
        title="Other",
        content="pump",
        sources=sources,
        provenance=corpus.documents[0].provenance,
        reason="Another identity",
    )
    service.apply(reviewed(service, created, host))
    bridge.publish()
    assert kb.articles.filter(hierarchy_code=article.hierarchy_code).count() == 2
    assert kb.articles.by_relative_path_prefix("guides").count() == 2
    assert "guides/pump.md" in kb.get_table_of_contents()
    snapshot = service.backend.load()
    heads = {
        document.document_id: document
        for document in snapshot.documents
        if document.reference in snapshot.heads
    }
    swap = service.propose(
        (
            replace(
                heads[str(article.pk)],
                path="guides/other.md",
                revision=str(uuid4()),
                review=None,
            ),
            replace(
                heads[created.changes[0].document_id],
                path="guides/pump.md",
                revision=str(uuid4()),
                review=None,
            ),
        ),
        reason="Swap explicit paths",
    )
    service.apply(reviewed(service, swap, host))
    bridge.publish()
    article.refresh_from_db()
    assert article.relative_path == "guides/other.md"
    foreign.relative_path = "guides/pump.md"
    foreign.save()
    with pytest.raises(CorpusError, match="Duplicate document path"):
        service.move(
            created.changes[0].document_id, "guides/other.md", reason="Collision"
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Article.objects.create(
            knowledgebase=kb,
            relative_path="guides/pump.md",
            title="Collision",
            content="No",
        )


@pytest.mark.django_db(transaction=True)
def test_path_migration_keeps_unmapped_data_and_drops_only_code_uniqueness():
    previous = [("django_ergo", "0013_reconcile_article_compatibility")]
    current = [("django_ergo", "0014_path_primary_articles")]
    executor = MigrationExecutor(connection)
    executor.migrate(previous)
    historical = executor.loader.project_state(previous).apps
    kb = historical.get_model("django_ergo", "Knowledgebase").objects.create(
        name="Old codes"
    )
    old = historical.get_model("django_ergo", "Article").objects.create(
        knowledgebase=kb, hierarchy_code="A1", title="Original", content="Keep"
    )
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        article = Article.objects.get(pk=old.pk)
        assert article.relative_path == ""
        assert article.hierarchy_code == "A1"
        assert article.content == "Keep"
        Article.objects.create(
            knowledgebase_id=kb.pk,
            hierarchy_code="A1",
            relative_path="first.md",
            title="One",
            content="Keep",
        )
        Article.objects.create(
            knowledgebase_id=kb.pk,
            hierarchy_code="A1",
            relative_path="second.md",
            title="Two",
            content="Keep",
        )
        assert (
            Article.objects.filter(knowledgebase_id=kb.pk, hierarchy_code="A1").count()
            == 3
        )
    finally:
        MigrationExecutor(connection).migrate(current)
