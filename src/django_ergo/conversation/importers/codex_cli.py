"""Import conversations from Codex CLI rollout files (JSONL)."""

from __future__ import annotations

from typing import Any

from django_ergo.conversation.history import codex_blocks
from django_ergo.conversation.history import codex_item
from django_ergo.conversation.history import db_timestamp
from django_ergo.conversation.history import parse_timestamp
from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession

_CODEX_LINE_TYPES = {"session_meta", "response_item", "turn_context", "event_msg"}


class CodexCLIImporter:
    """Stores a Codex rollout using the Claude message tables.

    Those tables hold every block kind Codex produces (text, reasoning
    summaries, tool calls and outputs). Message sequence is the JSONL line
    number, and created_at is the line's timestamp.
    """

    def detect_format(self, data: Any) -> bool:
        if not isinstance(data, list) or not data:
            return False
        return any(
            isinstance(record, dict)
            and (record.get("type") in _CODEX_LINE_TYPES or codex_item(record))
            for record in data[:20]
        )

    def _metadata(self, data: list[dict]) -> tuple[str, dict]:
        session_id = ""
        metadata: dict[str, Any] = {"imported_from": "codex_cli"}
        for record in data:
            if record.get("type") == "session_meta":
                payload = record.get("payload") or {}
                session_id = payload.get("id", "")
                for key in ("cwd", "cli_version", "originator"):
                    if payload.get(key):
                        metadata[key] = payload[key]
                break
        return session_id, metadata

    async def import_conversation(
        self, data: list[dict], user, workflow=None
    ) -> ConversationSession:
        session_id, metadata = self._metadata(data)
        session = await ConversationSession.objects.acreate(
            user=user,
            workflow=workflow,
            engine_type="claude",
            transport_type="api",
            status="paused",
            session_id=session_id,
            metadata=metadata,
        )
        for line_no, record in enumerate(data):
            item = codex_item(record)
            parsed = codex_blocks(item) if item else None
            if not parsed or not parsed[1]:
                continue
            role, blocks = parsed
            message = await ClaudeMessage.objects.acreate(
                session=session,
                role="assistant" if role == "assistant" else "user",
                sequence=line_no,
            )
            for block_seq, block in enumerate(blocks):
                await ClaudeContentBlock.objects.acreate(
                    message=message, sequence=block_seq, **_block_fields(block)
                )
            timestamp = parse_timestamp(record.get("timestamp"))
            if timestamp is not None:
                await ClaudeMessage.objects.filter(pk=message.pk).aupdate(
                    created_at=db_timestamp(timestamp)
                )
        return session


def _block_fields(block: dict) -> dict:
    kind = block["type"]
    if kind == "thinking":
        return {"block_type": "thinking", "thinking": block["text"]}
    if kind == "tool_use":
        tool_input = block.get("input")
        return {
            "block_type": "tool_use",
            "tool_use_id": block.get("id", ""),
            "tool_name": block.get("name", ""),
            "tool_input": tool_input
            if isinstance(tool_input, dict)
            else {"input": tool_input},
        }
    if kind == "tool_result":
        return {
            "block_type": "tool_result",
            "tool_result_for": block.get("tool_use_id", ""),
            "tool_result_content": block.get("content", ""),
            "is_error": bool(block.get("is_error")),
        }
    return {"block_type": "text", "text": block.get("text", "")}
