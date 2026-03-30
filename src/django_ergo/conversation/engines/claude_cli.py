"""Claude CLI engine — drives a claude subprocess with stream-json."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import Any

from django_ergo.conversation.adapters import ClaudeToolAdapter
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.engine import TransportFailover

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.models import ClaudeMessage
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
        cmd = ["claude", "-p", "--output-format", "stream-json"]

        if session.workflow:
            tools = self.get_tools_schema(session.workflow)
            if tools:
                tool_names = [t["name"] for t in tools]
                cmd.extend(["--allowedTools", ",".join(tool_names)])

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return str(session.id)

    async def resume_session(self, session: ConversationSession) -> None:
        # Would spawn: claude -p --output-format stream-json --resume {session.session_id}
        # For now, raise failover if no process is alive
        if not self._health_check():
            reason = "CLI process not running, cannot resume"
            raise TransportFailover(original="cli", fallback="api", reason=reason)

    async def _persist_block(
        self, assistant_msg: ClaudeMessage, block_data: dict, block_seq: int
    ) -> EngineResponse | None:
        """Persist a single content block and return the corresponding EngineResponse."""
        from django_ergo.conversation.models import ClaudeContentBlock

        block_type = block_data["type"]
        if block_type == "text":
            await ClaudeContentBlock.objects.acreate(
                message=assistant_msg,
                block_type="text",
                sequence=block_seq,
                text=block_data["text"],
            )
            return EngineResponse(
                event_type="text", raw=block_data, text=block_data["text"]
            )
        if block_type == "tool_use":
            await ClaudeContentBlock.objects.acreate(
                message=assistant_msg,
                block_type="tool_use",
                sequence=block_seq,
                tool_use_id=block_data["id"],
                tool_name=block_data["name"],
                tool_input=block_data.get("input", {}),
            )
            return EngineResponse(
                event_type="tool_use",
                raw=block_data,
                tool_use={
                    "id": block_data["id"],
                    "name": block_data["name"],
                    "input": block_data.get("input", {}),
                },
            )
        if block_type == "thinking":
            await ClaudeContentBlock.objects.acreate(
                message=assistant_msg,
                block_type="thinking",
                sequence=block_seq,
                thinking=block_data.get("thinking", ""),
            )
            return EngineResponse(
                event_type="thinking",
                raw=block_data,
                thinking=block_data.get("thinking", ""),
            )
        return None

    async def _read_response(
        self, assistant_msg: ClaudeMessage, start_block_seq: int
    ) -> AsyncIterator[EngineResponse]:
        """Read NDJSON lines from subprocess stdout, persist blocks, yield events."""
        block_seq = start_block_seq

        while True:
            line = await self.process.stdout.readline()
            if not line:
                break

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            # Claude CLI stream-json outputs "assistant" type events with full message
            if event_type == "assistant":
                msg_data = event.get("message", {})
                for block_data in msg_data.get("content", []):
                    response = await self._persist_block(
                        assistant_msg, block_data, block_seq
                    )
                    if response is not None:
                        yield response
                    block_seq += 1

                # Update message metadata
                assistant_msg.stop_reason = msg_data.get("stop_reason")
                usage = msg_data.get("usage", {})
                assistant_msg.input_tokens = usage.get("input_tokens")
                assistant_msg.output_tokens = usage.get("output_tokens")
                await assistant_msg.asave()

                yield EngineResponse(
                    event_type="done", raw={"stop_reason": msg_data.get("stop_reason")}
                )
                break

            elif event_type == "result":
                yield EngineResponse(event_type="done", raw=event)
                break

    async def send(
        self, session: ConversationSession, message: str
    ) -> AsyncIterator[EngineResponse]:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        if not self._health_check():
            raise TransportFailover(
                original="cli", fallback="api", reason="CLI process not running"
            )

        # Persist user message
        seq = await session.claude_messages.acount()
        user_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=user_msg, block_type="text", sequence=0, text=message
        )

        # Write to subprocess stdin
        self.process.stdin.write((message + "\n").encode())
        await self.process.stdin.drain()

        # Read NDJSON response lines and collect content blocks
        assistant_msg = await ClaudeMessage.objects.acreate(
            session=session, role="assistant", sequence=seq + 1
        )
        block_seq = 0

        async for response in self._read_response(assistant_msg, block_seq):
            yield response

    async def submit_tool_result(
        self,
        session: ConversationSession,
        tool_use_id: str,
        result: Any,
        *,
        is_error: bool = False,
    ) -> AsyncIterator[EngineResponse]:
        from django_ergo.conversation.models import ClaudeContentBlock
        from django_ergo.conversation.models import ClaudeMessage

        # Persist tool result
        seq = await session.claude_messages.acount()
        result_msg = await ClaudeMessage.objects.acreate(
            session=session, role="user", sequence=seq
        )
        await ClaudeContentBlock.objects.acreate(
            message=result_msg,
            block_type="tool_result",
            sequence=0,
            tool_result_for=tool_use_id,
            tool_result_content=str(result),
            is_error=is_error,
        )

        # Send tool result to subprocess
        tool_result_json = json.dumps(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": str(result),
                "is_error": is_error,
            }
        )
        self.process.stdin.write((tool_result_json + "\n").encode())
        await self.process.stdin.drain()

        # Read continuation
        assistant_msg = await ClaudeMessage.objects.acreate(
            session=session, role="assistant", sequence=seq + 1
        )
        async for response in self._read_response(assistant_msg, 0):
            yield response

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
