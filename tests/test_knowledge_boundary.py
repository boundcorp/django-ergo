import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_lightweight_app_and_virtual_command_do_not_import_legacy_dependencies():
    script = """
import importlib.abc
import json
import sys

class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in (
            'pgvector', 'psycopg', 'openai', 'yaml', 'django_ergo.models',
            'django_ergo.conversation',
        )):
            raise AssertionError('Unexpected optional dependency: ' + fullname)

sys.meta_path.insert(0, BlockLegacy())
from django.conf import settings
settings.configure(INSTALLED_APPS=['django_ergo.knowledge'],
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
    SECRET_KEY='test-only')
import django
django.setup()
from django_ergo.knowledge.backends import MemoryCorpus
from django_ergo.knowledge.schema import Snapshot
from django_ergo.knowledge.service import CorpusService
from django_ergo.knowledge.database import DatabaseCorpus
from django_ergo.knowledge.ingestion import prepare_absorption
from django_ergo.knowledge.schema import Provenance, Review
from django_ergo.knowledge.retrieval import MemoryVectorIndex
from dataclasses import replace
from django.core.management import call_command

class Policy:
    receipts = set()
    def allows(self, principal, collection_id, scope, action):
        return principal == 'host-command' and scope == 'project'
    def accepts_review(self, *args):
        return True
    def actor(self, principal):
        return principal
    def accepts_proposal(self, principal, proposal):
        return proposal.revision in self.receipts
    def audit(self, *args):
        pass

class Provider:
    def get_dimensions(self):
        return 2
    def generate_embedding(self, text):
        return [1.0, 0.0]

settings.ERGO_CORPORA = {'virtual': lambda: CorpusService(
    MemoryCorpus(Snapshot('virtual', 'project', (), ())), Policy(), 'host-command')}
result = json.loads(call_command('ergo_corpus', 'virtual', 'validate'))
assert result['documents'] == 0
call_command('migrate', verbosity=0)
for backend in (MemoryCorpus(Snapshot('virtual', 'project', (), ())),
                DatabaseCorpus.workspace(Snapshot('virtual', 'project', (), ()))):
    service = CorpusService(backend, Policy(), 'host-command')
    toolkit = prepare_absorption(service, content='Virtual intake', title='Upload',
        provenance=Provenance('host:upload', '1', 'host-command', '2026-09-09T12:00:00Z'),
        reason='Remember explicitly')
    toolkit.execute_tool('corpus_suggest_placed_page',
        {'document_id': 'memory', 'title': 'Memory', 'content': 'Virtual intake',
         'hierarchy_code': 'A0', 'summary': 'Virtual summary'})
    toolkit.execute_tool('corpus_suggest_tree',
        {'prefix': 'A', 'title': 'Memory', 'description': 'Instructions'})
    proposal = toolkit.get_proposal()
    reviews = [Review('reviewer', 'approved', 'Checked', 'v1', document.content_digest)
               for document in proposal.changes]
    approved = replace(proposal, changes=tuple(replace(document, review=review)
        for document, review in zip(proposal.changes, reviews)))
    service.policy.receipts.add(approved.revision)
    service.apply(service.review(proposal, reviews))
    assert service.search('virtual')
    assert service.resolve(proposal.changes[0].reference)['content'] == 'Virtual intake'
    assert len(service.operations()) == 1
    service.embedding_provider = Provider()
    service.provider_id = 'fixture/v1'
    service.vector_index = MemoryVectorIndex()
    service.rebuild_index()
    assert service.semantic_search_summary('virtual')
    assert service.multi_field_semantic_search('virtual', weights={'summary': 1})
    assert service.hybrid_search('virtual')
    assert service.get_by_hierarchy('A0')['summary'] == 'Virtual summary'
    assert service.get_tree_status() == [{'prefix': 'A', 'article_count': 1}]
    assert service.usage()
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_distribution_dependencies_offer_minimal_and_legacy_installs():
    configuration = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    project = configuration["project"]
    assert not {"pgvector", "openai", "psycopg[binary]"} & set(project["dependencies"])
    assert set(project["optional-dependencies"]["legacy"]) == {
        "pgvector",
        "openai",
        "psycopg[binary]",
    }
