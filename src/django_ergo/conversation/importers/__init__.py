"""Conversation importers for external formats."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.importers.claude_cli import ClaudeCLIImporter

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationSession


class ImportService:
    def __init__(self):
        self.importers = [ClaudeCLIImporter()]

    async def import_auto(self, data: Any, user, **kwargs) -> ConversationSession:
        for importer in self.importers:
            if importer.detect_format(data):
                return await importer.import_conversation(data, user, **kwargs)
        msg = "Unrecognized conversation format"
        raise ValueError(msg)
