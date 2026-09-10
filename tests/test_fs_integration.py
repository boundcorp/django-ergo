import uuid
from unittest.mock import patch

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from django_ergo.embedding_providers import DeterministicEmbeddingProvider
from django_ergo.filesystem_kb import build_repository_kb
from django_ergo.filesystem_kb import read_wiki_records
from django_ergo.filesystem_kb import validate_filesystem_kb
from django_ergo.indexing import index_article
from django_ergo.knowledge.articles import ArticleCompatibility
from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.ingestion import propose_wiki_import
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.service import CorpusService
from django_ergo.models import Article
from django_ergo.models import Knowledgebase
from django_ergo.models import KnowledgeSource
from django_ergo.repository_index import index_repository_commit
from django_ergo.repository_search import RepositorySearchError
from django_ergo.repository_search import search_repository
from django_ergo.sync import KnowledgeSyncError
from django_ergo.sync import project_commit
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import git
from tests.test_knowledge_corpus import host
from tests.test_knowledge_corpus import policy_for
from tests.test_knowledge_writes import reviewed

__all__ = ["corpus", "host"]
pytestmark = pytest.mark.django_db


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".ergo").mkdir()
    (tmp_path / ".ergo/index.yaml").write_text(
        "max_unit_characters: 6000\nroles:\n  - role: runtime\n    include: ['*.py']\n"
    )
    (tmp_path / "old.py").write_text("def target():\n    return 'target evidence'\n")
    page_id = str(uuid.uuid4())
    (tmp_path / "wiki/page.md").write_text(
        f"---\nid: {page_id}\ntitle: Target\nstatus: current\n---\nTarget explanation"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "Fixture")
    return tmp_path


@pytest.fixture
def source(repository):
    with override_settings(
        DJANGO_ERGO={"KNOWLEDGE_REPOSITORIES": {"fixture": str(repository)}}
    ):
        yield KnowledgeSource.objects.create(
            knowledgebase=Knowledgebase.objects.create(name="Fixture"),
            repository_alias="fixture",
            repository_subdirectory="",
        )


def test_projection_preserves_legacy_embedding_and_managed_lifecycle(
    source, repository
):
    with patch(
        "django_ergo.embedding_providers.get_embedding_provider",
        side_effect=AssertionError("No implicit provider"),
    ):
        project_commit(source)
    article = source.documents.get().article
    assert article.content_embedding is None
    index_article(article, provider=DeterministicEmbeddingProvider())
    article.refresh_from_db()
    assert article.content_embedding is not None
    with pytest.raises(PermissionError):
        article.save()
    with pytest.raises(PermissionError):
        article.delete()
    page = repository / "wiki/page.md"
    page.write_text(page.read_text().replace("current", "archived"))
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Withdraw")
    source.refresh_from_db()
    project_commit(source)
    article.refresh_from_db()
    assert article.status == "archived"
    assert not Article.objects.visible_to_retrieval().exists()
    assert article.content_embedding is None
    page.rename(repository / "wiki/renamed.md")
    page = repository / "wiki/renamed.md"
    page.write_text(page.read_text().replace("archived", "current"))
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Restore at new locator")
    source.refresh_from_db()
    project_commit(source)
    tracked = source.documents.get()
    assert tracked.article_id == article.pk
    assert tracked.prior_paths == ["page.md"]
    assert Article.objects.visible_to_retrieval().get().pk == article.pk
    page.write_text(page.read_text().replace("current", "typo"))
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Invalid lifecycle")
    source.refresh_from_db()
    with pytest.raises(KnowledgeSyncError, match="lifecycle"):
        project_commit(source)


def test_lexical_repository_index_needs_no_embedding_provider(source):
    with patch(
        "django_ergo.repository_index.get_embedding_provider",
        side_effect=AssertionError("Provider forbidden"),
    ):
        index_repository_commit(source, semantic=False)
    source.refresh_from_db()
    with patch(
        "django_ergo.repository_search.get_embedding_provider",
        side_effect=AssertionError("Provider forbidden"),
    ):
        assert search_repository(source, "target", mode="lexical")
    with pytest.raises(RepositorySearchError, match="fingerprint"):
        search_repository(source, "target", provider=DeterministicEmbeddingProvider())
    assert source.index_embedding_id == ""


@pytest.mark.parametrize("change", ["rename", "delete"])
def test_portable_citation_resolves_absent_ancestor_path(repository, tmp_path, change):
    commit = git(repository, "rev-parse", "HEAD").strip()
    page = repository / "wiki/page.md"
    page.write_text(
        f"---\nid: {uuid.uuid4()}\ntitle: Historical target\ntype: note\nstatus: current\nproject: fixture\nsources:\n  - kind: source-unit\n    unit_id: {uuid.uuid4()}\n    commit: {commit}\n    path: old.py\n    symbol: target\n    role: runtime\n    content_hash: {'a' * 64}\n---\nHistorical explanation"
    )
    if change == "rename":
        (repository / "old.py").rename(repository / "new.py")
    else:
        (repository / "old.py").unlink()
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Preserve historical citation")
    output = tmp_path / "bundle"
    build_repository_kb(repository, output, history_limit=1)
    assert validate_filesystem_kb(output)["valid"]
    captured = output / "raw/repository/citations" / commit / "old.py"
    assert captured.read_text().startswith("def target")


def test_missing_managed_article_import_cannot_resurrect_content(
    source, repository, corpus, host
):
    project_commit(source)
    (repository / "wiki/page.md").unlink()
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Missing")
    source.refresh_from_db()
    project_commit(source)
    empty = Snapshot("import", corpus.scope, (), ())
    service = CorpusService(MemoryCorpus(empty), policy_for(host, empty), host[3])
    bridge = ArticleCompatibility(service, source.knowledgebase, lambda *args: True)
    proposal = bridge.propose_import(
        provenance=corpus.documents[0].provenance, reason="Capture only"
    )
    service.apply(reviewed(service, proposal, host))
    assert service.search("target") == []


def test_portable_bundle_is_reviewed_common_input(repository, tmp_path, corpus, host):
    (repository / "wiki/page.md").unlink()
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Build from source evidence")
    output = tmp_path / "bundle"
    build_repository_kb(repository, output)
    empty = Snapshot("portable-import", corpus.scope, (), ())
    service = CorpusService(MemoryCorpus(empty), policy_for(host, empty), host[3])
    proposal = propose_wiki_import(
        service,
        read_wiki_records(output),
        provenance=corpus.documents[0].provenance,
        reason="Review generated bundle",
    )
    assert service.search("repository") == []
    service.apply(reviewed(service, proposal, host))
    assert service.search("repository")


@pytest.mark.django_db(transaction=True)
def test_conflicting_fs_hierarchy_upgrade_fails_without_reassigning():
    initial = [("django_ergo", "0009_knowledgebase_organization_strategy")]
    branch = [("django_ergo", "0011_knowledgesource_index_config_hash_and_more")]
    current = [("django_ergo", "0014_path_primary_articles")]
    MigrationExecutor(connection).migrate(initial)
    executor = MigrationExecutor(connection)
    executor.migrate(branch)
    historical = executor.loader.project_state(branch).apps
    articles = historical.get_model("django_ergo", "Article")
    kb = historical.get_model("django_ergo", "Knowledgebase").objects.create(
        name="Conflicts"
    )
    first = articles.objects.create(
        knowledgebase=kb, hierarchy_code="A", title="First", content="Preserved"
    )
    second = articles.objects.create(
        knowledgebase=kb, hierarchy_code="A", title="Second", content="Preserved"
    )
    try:
        with pytest.raises(RuntimeError, match="host reconciliation"):
            MigrationExecutor(connection).migrate(current)
        assert (
            articles.objects.filter(knowledgebase=kb, hierarchy_code="A").count() == 2
        )
    finally:
        articles.objects.filter(pk=second.pk).update(hierarchy_code=None)
        MigrationExecutor(connection).migrate(current)
    assert Article.objects.get(pk=first.pk).hierarchy_code == "A"
    assert Article.objects.get(pk=second.pk).hierarchy_code is None
