"""Claude CLI engine — drives a claude subprocess with stream-json."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engine import TransportFailover

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.models import ConversationSession
    from django_ergo.models import Workflow


class ClaudeCLIEngine(Engine):
    engine_type = "claude"

    def __init__(self, config: dict):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._adapter = ClaudeToolAdapter()

    def _health_check(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start_session(self, session: ConversationSession) -> str:
        # Stub — real implementation spawns claude subprocess
        return str(session.id)

    async def resume_session(self, session: ConversationSession) -> None:
        # Would spawn: claude -p --output-format stream-json --resume {session.session_id}
        # For now, raise failover if no process is alive
        if not self._health_check():
            reason = "CLI process not running, cannot resume"
            raise TransportFailover(original="cli", fallback="api", reason=reason)

    async def send(
        self, session: ConversationSession, message: str
    ) -> AsyncIterator[EngineResponse]:
        msg = "CLI streaming not yet implemented"
        raise NotImplementedError(msg)
        yield  # make it a generator

    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        *,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        msg = "CLI tool result not yet implemented"
        raise NotImplementedError(msg)
        yield

    def get_tools_schema(self, workflow: Workflow) -> list[dict]:
        from django_ergo.tools import tool_registry

        tools_config = workflow.get_tools_config()
        if not tools_config:
            return []
        schemas = []
        for tool_name in tools_config.get("enabled_tools", []):
            tool_config = tool_registry.get_tool(tool_name)
            if tool_config:
                schemas.append(self._adapter.to_engine_schema(tool_config))
        return schemas

    def reconstruct_messages(self, session: ConversationSession) -> list[dict]:
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
        return messages

    async def close_session(self, session: ConversationSession) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except TimeoutError:
                self.process.kill()
        self.process = None

    def get_tool_adapter(self):
        return self._adapter
