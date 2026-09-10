import pytest
from asgiref.sync import async_to_sync

from django_ergo.conversation.runner import _record_kb_usage
from django_ergo.knowledge.articles import ArticleCompatibility
from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.retrieval import MemoryVectorIndex
from django_ergo.knowledge.schema import CorpusError
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.service import AccessDeniedError
from django_ergo.knowledge.service import CorpusService
from django_ergo.knowledge.toolkit import CorpusToolkit
from django_ergo.knowledge.usage import DatabaseUsageStore
from tests.test_kb_pipelines import source_session
from tests.test_kb_pipelines import user
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import host
from tests.test_knowledge_corpus import policy_for
from tests.test_knowledge_features import TinyProvider
from tests.test_knowledge_legacy import legacy_kbs
from tests.test_knowledge_writes import reviewed

__all__ = ["corpus", "host", "legacy_kbs", "source_session", "user"]
pytestmark = pytest.mark.django_db


def test_article_compatibility_roundtrip_preserves_existing_callers(
    legacy_kbs, corpus, host
):
    _owner, knowledgebase, article, archived, foreign = legacy_kbs
    knowledgebase.organization_strategy = "### Tree #A: Pumps"
    knowledgebase.save(update_fields=["organization_strategy"])
    empty = Snapshot("bridge", corpus.scope, (), ())
    service = CorpusService(
        MemoryCorpus(empty),
        policy_for(host, empty),
        host[3],
        embedding_provider=TinyProvider(),
        provider_id="tiny/v1",
        vector_index=MemoryVectorIndex(),
    )
    bridge = ArticleCompatibility(
        service,
        knowledgebase,
        lambda principal, kb, action: (
            kb.pk == knowledgebase.pk and principal == host[3]
        ),
    )
    proposal = bridge.propose_import(
        provenance=corpus.documents[0].provenance,
        reason="Explicitly admit existing Article content",
    )
    assert service.search("garden") == []
    service.apply(reviewed(service, proposal, host))
    assert service.get_by_hierarchy("A")["citation"]["document_id"] == str(article.pk)
    assert service.get_by_hierarchy("A")["summary"] == article.summary
    assert service.get_strategy()["content"] == knowledgebase.organization_strategy
    service.rebuild_index()
    assert service.semantic_search_content("pump")[0]["citation"]["document_id"] == str(
        article.pk
    )
    correction = service.revise(
        str(article.pk),
        content="Updated pump guidance",
        summary="Updated summary",
        reason="Correction",
    )
    service.apply(reviewed(service, correction, host))
    result = bridge.publish()
    article.refresh_from_db()
    archived.refresh_from_db()
    foreign.refresh_from_db()
    assert result["article_ids"][str(article.pk)] == str(article.pk)
    assert article.content == "Updated pump guidance"
    assert article.summary == "Updated summary"
    assert archived.status == "archived"
    assert foreign.content == "garden pump"
    assert list(
        knowledgebase.articles.visible_to_retrieval().values_list("pk", flat=True)
    ) == [article.pk]
    assert article.content_embedding is not None
    knowledgebase.organization_strategy = ""
    knowledgebase.save(update_fields=["organization_strategy"])
    cleared = bridge.propose_import(
        provenance=corpus.documents[0].provenance,
        reason="Clear strategy through legacy caller",
    )
    service.apply(reviewed(service, cleared, host))
    assert service.get_strategy()["content"] == ""


def test_article_bridge_denial_does_not_touch_sources(legacy_kbs, corpus, host):
    _owner, knowledgebase, _article, _archived, _foreign = legacy_kbs
    service = CorpusService(MemoryCorpus(corpus), policy_for(host, corpus), host[3])
    bridge = ArticleCompatibility(service, knowledgebase, lambda *args: False)
    with pytest.raises(AccessDeniedError):
        bridge.propose_import(provenance=corpus.documents[0].provenance, reason="No")
    with pytest.raises(AccessDeniedError):
        bridge.publish()


def test_article_publish_does_not_overwrite_concurrent_legacy_edits(
    legacy_kbs, corpus, host
):
    _owner, knowledgebase, article, _archived, _foreign = legacy_kbs
    empty = Snapshot("bridge", corpus.scope, (), ())
    service = CorpusService(MemoryCorpus(empty), policy_for(host, empty), host[3])
    bridge = ArticleCompatibility(service, knowledgebase, lambda *args: True)
    pending = bridge.propose_import(
        provenance=corpus.documents[0].provenance, reason="Import"
    )
    service.apply(reviewed(service, pending, host))
    knowledgebase.articles.filter(pk=article.pk).update(
        content="Concurrent legacy correction"
    )
    with pytest.raises(CorpusError, match="Articles changed"):
        bridge.publish()
    article.refresh_from_db()
    assert article.content == "Concurrent legacy correction"


def test_runner_tracks_logical_corpus_context_without_legacy_fk(
    source_session, corpus, host
):
    service = CorpusService(
        MemoryCorpus(corpus),
        policy_for(host, corpus),
        host[3],
        usage_store=DatabaseUsageStore(),
    )
    toolkit = CorpusToolkit(service)
    async_to_sync(_record_kb_usage)(source_session, [toolkit])
    toolkit.execute_tool("corpus_search", {"query": "pump"})
    events = service.usage(context_id=str(source_session.pk))
    assert [event["mode"] for event in events] == ["read", "search"]
    assert events[1]["references"][0]["document_id"] == "reset"
    assert service.usage_context == ""
    assert source_session.kb_usages.count() == 0
