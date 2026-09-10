import json
from unittest.mock import patch

import pytest

from django_ergo.knowledge.ingestion import propose_wiki_import
from django_ergo.knowledge.service import AccessDeniedError
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import host
from tests.test_knowledge_writes import reviewed
from tests.test_knowledge_writes import writer

__all__ = ["corpus", "host", "writer"]


def test_legacy_records_enter_common_review_without_files(writer, corpus, host):
    record = {
        "metadata": {
            "id": "imported-wiki",
            "title": "Imported guide",
            "status": "current",
            "hierarchy_code": "E1",
            "sources": [{"commit": "claimed-not-verified", "path": "old.py"}],
        },
        "content": "Imported operational guide",
    }
    with (
        patch("builtins.open", side_effect=AssertionError("No files")),
        patch("subprocess.run", side_effect=AssertionError("No Git")),
    ):
        proposal = propose_wiki_import(
            writer,
            [record],
            provenance=corpus.documents[0].provenance,
            reason="Review legacy artifact",
        )
        assert writer.search("imported") == []
        writer.apply(reviewed(writer, proposal, host))
        page = writer.get_by_hierarchy("E1")
        assert page["content"] == record["content"]
        capture = writer.resolve(proposal.changes[0].reference)
        assert json.loads(capture["content"]) == record
        record["metadata"]["status"] = "archived"
        withdrawal = propose_wiki_import(
            writer,
            [record],
            provenance=corpus.documents[0].provenance,
            reason="Withdraw legacy guide",
        )
        writer.apply(reviewed(writer, withdrawal, host))
        assert writer.search("imported") == []


def test_import_denial_precedes_consuming_source_records(writer, corpus, host):
    writer.principal = host[4]

    def source():
        message = "Source must not be consumed"
        raise AssertionError(message)
        yield

    with pytest.raises(AccessDeniedError):
        propose_wiki_import(
            writer, source(), provenance=corpus.documents[0].provenance, reason="Denied"
        )
