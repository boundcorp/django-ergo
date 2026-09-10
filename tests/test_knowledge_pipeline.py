from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.adapters import OpenAIToolAdapter
from django_ergo.kb_pipelines import absorb_corpus_conversation
from django_ergo.knowledge.ingestion import prepare_absorption
from tests.test_kb_pipelines import source_session
from tests.test_kb_pipelines import user
from tests.test_knowledge_corpus import corpus
from tests.test_knowledge_corpus import host
from tests.test_knowledge_writes import reviewed
from tests.test_knowledge_writes import writer

__all__ = ["corpus", "host", "source_session", "user", "writer"]

pytestmark = pytest.mark.django_db


def test_existing_conversation_runner_accepts_corpus_toolkit(
    source_session, writer, corpus, host
):
    toolkit = prepare_absorption(
        writer,
        content="Authorized redacted preference: morning deploys",
        title="Conversation capture",
        provenance=corpus.documents[0].provenance,
        reason="Remember approved transcript",
    )
    for adapter in (ClaudeToolAdapter(), OpenAIToolAdapter()):
        assert len(toolkit.get_tools_schema(adapter)) == 15

    async def curator(**kwargs):
        assert kwargs["extra_tools"] == [toolkit]
        assert "Authorized redacted preference" in kwargs["message"]
        assert "I prefer morning deployments and use pytest" not in kwargs["message"]
        await sync_to_async(toolkit.execute_tool)(
            "corpus_suggest_create",
            {
                "document_id": "memory",
                "title": "Deploy preferences",
                "content": "Morning deploys",
            },
        )

    with patch("django_ergo.kb_pipelines.run_workflow_task", side_effect=curator):
        result = async_to_sync(absorb_corpus_conversation)(
            source_session, toolkit, MagicMock()
        )
    assert result is toolkit
    assert writer.search("morning") == []
    writer.apply(reviewed(writer, toolkit.get_proposal(), host))
    assert writer.search("morning")[0]["citation"]["document_id"] == "memory"
