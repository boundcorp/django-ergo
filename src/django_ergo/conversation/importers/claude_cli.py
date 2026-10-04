"""Import conversations from Claude CLI session files (JSONL)."""

from __future__ import annotations

from typing import Any

from django_ergo.conversation.history import db_timestamp
from django_ergo.conversation.history import parse_timestamp
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import SessionMessage


async def _keep_original_timestamp(message: SessionMessage, value: Any) -> None:
    """Set created_at to the transcript's timestamp (auto_now_add ignores it on create)."""
    timestamp = parse_timestamp(value)
    if timestamp is not None:
        await SessionMessage.objects.filter(pk=message.pk).aupdate(
            created_at=db_timestamp(timestamp)
        )
        message.created_at = timestamp


class ClaudeCLIImporter:
    def detect_format(self, data: Any) -> bool:
        if not isinstance(data, list) or len(data) == 0:
            return False
        return any(
            isinstance(msg, dict) and msg.get("type") in ("user", "assistant")
            for msg in data
        )

    def _extract_metadata(self, data: list[dict]) -> tuple[str, dict]:
        """Extract session ID and metadata from JSONL data."""
        session_id = ""
        slug = None
        last_prompt = None
        total_duration_ms = 0

        for msg in data:
            if not session_id and (sid := msg.get("sessionId")):
                session_id = sid
            if not slug and (s := msg.get("slug")):
                slug = s
            if msg.get("type") == "last-prompt":
                last_prompt = msg.get("lastPrompt")
            if msg.get("type") == "system" and msg.get("subtype") == "turn_duration":
                total_duration_ms += msg.get("durationMs", 0)

        metadata = {"imported_from": "cli_session"}
        if slug:
            metadata["slug"] = slug
        if last_prompt:
            metadata["last_prompt"] = last_prompt
        if total_duration_ms:
            metadata["total_duration_ms"] = total_duration_ms

        return session_id, metadata

    async def import_conversation(
        self, data: list[dict], user, workflow=None
    ) -> ConversationSession:
        session_id, metadata = self._extract_metadata(data)

        session = await ConversationSession.objects.acreate(
            user=user,
            workflow=workflow,
            engine_type="claude",
            transport_type="api",
            status="paused",
            session_id=session_id,
            metadata=metadata,
        )

        for seq, msg_data in enumerate(data):
            msg_type = msg_data.get("type")
            if msg_type not in ("user", "assistant"):
                continue

            message_obj = msg_data.get("message", {})
            role = "user" if msg_type == "user" else "assistant"
            usage = message_obj.get("usage", {})

            message_row = await SessionMessage.objects.acreate(
                session=session,
                role=role,
                sequence=seq,
                stop_reason=message_obj.get("stop_reason"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                model_name=message_obj.get("model"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
            )

            content = message_obj.get("content", "")
            await self._import_content_blocks(message_row, content)
            await _keep_original_timestamp(message_row, msg_data.get("timestamp"))
        return session

    async def _import_content_blocks(
        self, message_row: SessionMessage, content: Any
    ) -> None:
        if isinstance(content, str):
            await MessageBlock.objects.acreate(
                message=message_row,
                block_type="text",
                sequence=0,
                text=content,
            )
        elif isinstance(content, list):
            for block_seq, block in enumerate(content):
                await self._import_block(message_row, block, block_seq)

    async def _import_block(
        self, message_row: SessionMessage, block: dict, block_seq: int
    ) -> None:
        block_type = block.get("type", "text")
        if block_type == "text":
            await MessageBlock.objects.acreate(
                message=message_row,
                block_type="text",
                sequence=block_seq,
                text=block.get("text", ""),
            )
        elif block_type == "thinking":
            await MessageBlock.objects.acreate(
                message=message_row,
                block_type="thinking",
                sequence=block_seq,
                thinking=block.get("thinking", ""),
            )
        elif block_type == "tool_use":
            await MessageBlock.objects.acreate(
                message=message_row,
                block_type="tool_use",
                sequence=block_seq,
                tool_use_id=block.get("id", ""),
                tool_name=block.get("name", ""),
                tool_input=block.get("input"),
            )
        elif block_type == "tool_result":
            await MessageBlock.objects.acreate(
                message=message_row,
                block_type="tool_result",
                sequence=block_seq,
                tool_result_for=block.get("tool_use_id", ""),
                tool_result_content=block.get("content", ""),
                is_error=block.get("is_error", False),
            )
