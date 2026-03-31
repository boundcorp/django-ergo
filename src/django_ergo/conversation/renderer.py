"""Conversation renderer with tiered detail levels.

Detail levels:
- headline: slug + last_prompt (if available) + first user message (1000 chars)
- skeleton: user messages + assistant text, tool calls summarized, thinking omitted
- full: everything verbatim
- custom: async callable for LLM-powered rendering
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationSession

HEADLINE_MAX_CHARS = 1000
HEADLINE_LINES_NO_MSG = 2
SUMMARIZE_ARGS_MAX_LEN = 50
SUMMARIZE_ARGS_TRUNCATE = 47


class ConversationRenderer:
    """Renders conversations at configurable detail levels for LLM consumption."""

    def __init__(self, detail: str = "skeleton", custom_fn=None):
        self.detail = detail
        self.custom_fn = custom_fn

    def render(self, session: ConversationSession) -> str:
        """Render a conversation from DB models."""
        messages = self._get_messages_from_session(session)
        metadata = session.metadata if hasattr(session, "metadata") else {}
        return self.render_messages(messages, metadata=metadata)

    def render_messages(
        self, messages: list[dict], metadata: dict | None = None
    ) -> str:
        """Render from reconstructed message dicts."""
        if self.detail == "headline":
            return self._render_headline(messages, metadata or {})
        if self.detail == "skeleton":
            return self._render_skeleton(messages)
        if self.detail == "full":
            return self._render_full(messages)
        if self.detail == "custom":
            msg = (
                "Custom rendering requires render_async(). Use render_async() instead."
            )
            raise ValueError(msg)
        msg = f"Unknown detail level: {self.detail}"
        raise ValueError(msg)

    async def render_async(self, session: ConversationSession, **kwargs) -> str:
        """Async render — required for custom strategies."""
        if self.detail == "custom":
            if not self.custom_fn:
                msg = "Custom detail level requires a custom_fn"
                raise ValueError(msg)
            # Check cache first
            cache = session.metadata.get("rendered_cache", {})
            cache_key = getattr(self.custom_fn, "__name__", "custom")
            if cache_key in cache:
                return cache[cache_key]
            return await self.custom_fn(session, **kwargs)
        return self.render(session)

    async def render_and_cache(self, session: ConversationSession, **kwargs) -> str:
        """Render with custom strategy and cache the result in session.metadata."""
        result = await self.render_async(session, **kwargs)
        if self.detail == "custom" and self.custom_fn:
            cache_key = getattr(self.custom_fn, "__name__", "custom")
            if "rendered_cache" not in session.metadata:
                session.metadata["rendered_cache"] = {}
            session.metadata["rendered_cache"][cache_key] = result
            await session.asave(update_fields=["metadata"])
        return result

    def _get_messages_from_session(self, session: ConversationSession) -> list[dict]:
        """Build message dicts from DB models."""
        messages = []
        for msg in session.claude_messages.prefetch_related("content_blocks").all():
            content = []
            for block in msg.content_blocks.all():
                if block.block_type == "text":
                    content.append({"type": "text", "text": block.text})
                elif block.block_type == "thinking":
                    content.append({"type": "thinking", "thinking": block.thinking})
                elif block.block_type == "tool_use":
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.tool_use_id,
                            "name": block.tool_name,
                            "input": block.tool_input or {},
                        }
                    )
                elif block.block_type == "tool_result":
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_result_for,
                            "content": block.tool_result_content or "",
                            "is_error": block.is_error,
                        }
                    )
            messages.append({"role": msg.role, "content": content})

        # Fallback: try OpenAI messages if no Claude messages
        if not messages:
            for msg in session.openai_messages.all():
                entry = {"role": msg.role, "content": msg.content}
                messages.append(entry)

        return messages

    def _render_headline(self, messages: list[dict], metadata: dict) -> str:
        lines = []
        slug = metadata.get("slug")
        last_prompt = metadata.get("last_prompt")
        if slug:
            lines.append(f"[slug: {slug}]")
        if last_prompt:
            lines.append(f'[last_prompt: "{last_prompt}"]')

        first_user_text = self._find_first_user_text(messages)
        if first_user_text:
            truncated = first_user_text[:HEADLINE_MAX_CHARS]
            lines.append(f"First message: {truncated}")

        return (
            " ".join(lines)
            if len(lines) <= HEADLINE_LINES_NO_MSG and not first_user_text
            else "\n".join(lines)
        )

    def _render_skeleton(self, messages: list[dict]) -> str:
        lines = []
        tool_call_counter = 0
        tool_id_to_number: dict[str, int] = {}

        for msg_num, msg in enumerate(messages):
            role = msg["role"].upper()
            content = msg.get("content")

            if isinstance(content, str):
                lines.append(f"[msg #{msg_num} {role}]: {content}")
            elif isinstance(content, list):
                msg_parts, tool_call_counter = self._skeleton_blocks(
                    content, tool_call_counter, tool_id_to_number
                )
                if msg_parts:
                    lines.append(f"[msg #{msg_num} {role}]: {' '.join(msg_parts)}")
            elif content is not None:
                lines.append(f"[msg #{msg_num} {role}]: {content}")

        return "\n".join(lines)

    def _skeleton_blocks(
        self,
        blocks: list[dict],
        tool_call_counter: int,
        tool_id_to_number: dict[str, int],
    ) -> tuple[list[str], int]:
        msg_parts: list[str] = []
        for block in blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                msg_parts.append(block["text"])
            elif block_type == "tool_use":
                tool_call_counter += 1
                tool_id_to_number[block.get("id", "")] = tool_call_counter
                key_args = self._summarize_args(block.get("input", {}))
                msg_parts.append(
                    f"[tool_call #{tool_call_counter}: {block['name']}({key_args})]"
                )
            elif block_type == "tool_result":
                tc_id = block.get("tool_use_id", "")
                tc_num = tool_id_to_number.get(tc_id, "?")
                result_content = block.get("content", "")
                line_count = (
                    len(result_content.strip().splitlines())
                    if isinstance(result_content, str)
                    else 1
                )
                error_tag = " ERROR" if block.get("is_error") else ""
                msg_parts.append(
                    f"[tool_result #{tc_num}{error_tag}: ({line_count} lines)]"
                )
            # thinking blocks are omitted in skeleton
        return msg_parts, tool_call_counter

    def _render_full(self, messages: list[dict]) -> str:
        lines = []
        tool_call_counter = 0
        tool_id_to_number: dict[str, int] = {}

        for msg_num, msg in enumerate(messages):
            role = msg["role"].upper()
            content = msg.get("content")

            if isinstance(content, str):
                lines.append(f"[msg #{msg_num} {role}]: {content}")
            elif isinstance(content, list):
                for block in content:
                    block_type = block.get("type", "")
                    if block_type == "text":
                        lines.append(f"[msg #{msg_num} {role}]: {block['text']}")
                    elif block_type == "thinking":
                        lines.append(
                            f"[msg #{msg_num} {role} thinking]: {block['thinking']}"
                        )
                    elif block_type == "tool_use":
                        tool_call_counter += 1
                        tool_id_to_number[block.get("id", "")] = tool_call_counter
                        lines.append(
                            f"[msg #{msg_num} {role} tool_call #{tool_call_counter}]: "
                            f"{block['name']}({block.get('input', {})})"
                        )
                    elif block_type == "tool_result":
                        tc_id = block.get("tool_use_id", "")
                        tc_num = tool_id_to_number.get(tc_id, "?")
                        error_tag = " ERROR" if block.get("is_error") else ""
                        lines.append(
                            f"[msg #{msg_num} TOOL_RESULT #{tc_num}{error_tag}]: "
                            f"{block.get('content', '')}"
                        )
            elif content is not None:
                lines.append(f"[msg #{msg_num} {role}]: {content}")

        return "\n".join(lines)

    def _find_first_user_text(self, messages: list[dict]) -> str | None:
        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        return block["text"]
        return None

    def _summarize_args(self, args: dict) -> str:
        if not args:
            return ""
        parts = []
        for key, val in args.items():
            val_str = str(val)
            if len(val_str) > SUMMARIZE_ARGS_MAX_LEN:
                val_str = val_str[:SUMMARIZE_ARGS_TRUNCATE] + "..."
            parts.append(f'{key}="{val_str}"')
        return ", ".join(parts)
