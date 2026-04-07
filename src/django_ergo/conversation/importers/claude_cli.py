"""Import conversations from Claude CLI session files (JSONL)."""

from __future__ import annotations

from typing import Any

from django_ergo.conversation.models import ClaudeContentBlock
from django_ergo.conversation.models import ClaudeMessage
from django_ergo.conversation.models import ConversationSession


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

            claude_msg = await ClaudeMessage.objects.acreate(
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
            await self._import_content_blocks(claude_msg, content)
        return session

    async def _import_content_blocks(
        self, claude_msg: ClaudeMessage, content: Any
    ) -> None:
        if isinstance(content, str):
            await ClaudeContentBlock.objects.acreate(
                message=claude_msg,
                block_type="text",
                sequence=0,
                text=content,
            )
        elif isinstance(content, list):
            for block_seq, block in enumerate(content):
                await self._import_block(claude_msg, block, block_seq)

    async def _import_block(
        self, claude_msg: ClaudeMessage, block: dict, block_seq: int
    ) -> None:
        block_type = block.get("type", "text")
        if block_type == "text":
            await ClaudeContentBlock.objects.acreate(
                message=claude_msg,
                block_type="text",
                sequence=block_seq,
                text=block.get("text", ""),
            )
        elif block_type == "thinking":
            await ClaudeContentBlock.objects.acreate(
                message=claude_msg,
                block_type="thinking",
                sequence=block_seq,
                thinking=block.get("thinking", ""),
            )
        elif block_type == "tool_use":
            await ClaudeContentBlock.objects.acreate(
                message=claude_msg,
                block_type="tool_use",
                sequence=block_seq,
                tool_use_id=block.get("id", ""),
                tool_name=block.get("name", ""),
                tool_input=block.get("input"),
            )
        elif block_type == "tool_result":
            await ClaudeContentBlock.objects.acreate(
                message=claude_msg,
                block_type="tool_result",
                sequence=block_seq,
                tool_result_for=block.get("tool_use_id", ""),
                tool_result_content=block.get("content", ""),
                is_error=block.get("is_error", False),
            )
