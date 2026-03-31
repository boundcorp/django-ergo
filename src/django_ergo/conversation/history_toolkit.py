"""ChatWithHistoryToolkit — scoped tools for agent drill-down into past conversations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.renderer import ConversationRenderer

if TYPE_CHECKING:
    from django_ergo.conversation.adapters import ToolAdapter
    from django_ergo.conversation.models import ConversationSession

# Tool definitions with name, description, and parameters
TOOLKIT_TOOLS = [
    {
        "name": "history_view_conversation",
        "description": "View a past conversation at a given detail level (headline, skeleton, or full)",
        "parameters": {
            "session_id": {
                "type": "string",
                "required": True,
                "description": "UUID of the conversation session",
            },
            "detail": {
                "type": "string",
                "required": False,
                "description": "Detail level: headline, skeleton, or full. Default: skeleton",
            },
        },
    },
    {
        "name": "history_get_tool_call",
        "description": "Get the full input and output for a specific tool call by number",
        "parameters": {
            "session_id": {
                "type": "string",
                "required": True,
                "description": "UUID of the conversation session",
            },
            "tool_call_number": {
                "type": "integer",
                "required": True,
                "description": "Tool call number (e.g., 1, 2, 3)",
            },
        },
    },
    {
        "name": "history_get_message_range",
        "description": "Get full-detail rendering of a range of messages by number",
        "parameters": {
            "session_id": {
                "type": "string",
                "required": True,
                "description": "UUID of the conversation session",
            },
            "start": {
                "type": "integer",
                "required": True,
                "description": "Start message number (inclusive)",
            },
            "end": {
                "type": "integer",
                "required": True,
                "description": "End message number (inclusive)",
            },
        },
    },
    {
        "name": "history_get_thinking",
        "description": "Get the thinking block content for a specific assistant message",
        "parameters": {
            "session_id": {
                "type": "string",
                "required": True,
                "description": "UUID of the conversation session",
            },
            "message_number": {
                "type": "integer",
                "required": True,
                "description": "Message number",
            },
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLKIT_TOOLS}


class ChatWithHistoryToolkit:
    """Scoped toolkit for chatting about past conversations with drill-down tools."""

    def __init__(
        self,
        sessions: list[ConversationSession],
        renderer: ConversationRenderer | None = None,
    ):
        self.sessions = {str(s.id): s for s in sessions}
        self.renderer = renderer or ConversationRenderer(detail="skeleton")

    def render_overview(self) -> str:
        """Render skeleton views of all sessions for initial context."""
        parts = []
        for i, (sid, session) in enumerate(self.sessions.items(), 1):
            header = f"=== Conversation {i} (session_id: {sid}) ==="
            body = self.renderer.render(session)
            parts.append(f"{header}\n{body}")
        return "\n\n".join(parts)

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in TOOL_NAMES

    def get_tools_schema(self, adapter: ToolAdapter) -> list[dict]:
        """Return tool schemas in engine-native format."""
        from django_ergo.tools import ToolConfig

        schemas = []
        for tool_def in TOOLKIT_TOOLS:
            config = ToolConfig(
                name=tool_def["name"],
                description=tool_def["description"],
                parameters=tool_def["parameters"],
                readonly=True,
            )
            schemas.append(adapter.to_engine_schema(config))
        return schemas

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        if tool_name == "history_view_conversation":
            return self._view_conversation(arguments)
        if tool_name == "history_get_tool_call":
            return self._get_tool_call(arguments)
        if tool_name == "history_get_message_range":
            return self._get_message_range(arguments)
        if tool_name == "history_get_thinking":
            return self._get_thinking(arguments)
        msg = f"Unknown tool: {tool_name}"
        raise ValueError(msg)

    def _get_session(self, session_id: str) -> ConversationSession:
        session = self.sessions.get(session_id)
        if session is None:
            msg = f"Session {session_id} not found in this toolkit"
            raise ValueError(msg)
        return session

    def _view_conversation(self, args: dict) -> str:
        session = self._get_session(args["session_id"])
        detail = args.get("detail", "skeleton")
        renderer = ConversationRenderer(detail=detail)
        return renderer.render(session)

    def _get_tool_call(self, args: dict) -> str:
        session = self._get_session(args["session_id"])
        target_num = args["tool_call_number"]

        messages = self.renderer._get_messages_from_session(session)  # noqa: SLF001
        tool_call_counter = 0

        for msg in messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    tool_call_counter += 1
                    if tool_call_counter == target_num:
                        # Find the matching tool_result
                        tool_id = block.get("id", "")
                        result_text = self._find_tool_result(messages, tool_id)
                        return (
                            f"Tool call #{target_num}: {block['name']}\n"
                            f"Input: {block.get('input', {})}\n"
                            f"Result: {result_text}"
                        )

        msg = f"Tool call #{target_num} not found"
        raise ValueError(msg)

    def _find_tool_result(self, messages: list[dict], tool_use_id: str) -> str:
        for msg in messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    block.get("type") == "tool_result"
                    and block.get("tool_use_id") == tool_use_id
                ):
                    return str(block.get("content", ""))
        return "(no result found)"

    def _get_message_range(self, args: dict) -> str:
        session = self._get_session(args["session_id"])
        start = args["start"]
        end = args["end"]

        messages = self.renderer._get_messages_from_session(session)  # noqa: SLF001
        selected = messages[start : end + 1]

        lines: list[str] = []
        tool_call_counter = 0
        tool_id_to_number: dict[str, int] = {}

        # Count tool calls before start to get correct numbering
        for msg in messages[:start]:
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_call_counter += 1
                        tool_id_to_number[block.get("id", "")] = tool_call_counter

        # Render selected range at full detail
        for i, msg in enumerate(selected):
            msg_num = start + i
            role = msg["role"].upper()
            tool_call_counter = self._render_msg_blocks(
                msg, msg_num, role, lines, tool_call_counter, tool_id_to_number
            )

        return "\n".join(lines)

    def _render_msg_blocks(  # noqa: PLR0913
        self,
        msg: dict,
        msg_num: int,
        role: str,
        lines: list[str],
        tool_call_counter: int,
        tool_id_to_number: dict[str, int],
    ) -> int:
        """Render blocks for a single message, appending to lines. Returns updated tool_call_counter."""
        content = msg.get("content")
        if isinstance(content, str):
            lines.append(f"[msg #{msg_num} {role}]: {content}")
        elif isinstance(content, list):
            for block in content:
                bt = block.get("type", "")
                if bt == "text":
                    lines.append(f"[msg #{msg_num} {role}]: {block['text']}")
                elif bt == "thinking":
                    lines.append(
                        f"[msg #{msg_num} {role} thinking]: {block['thinking']}"
                    )
                elif bt == "tool_use":
                    tool_call_counter += 1
                    tool_id_to_number[block.get("id", "")] = tool_call_counter
                    lines.append(
                        f"[msg #{msg_num} {role} tool_call #{tool_call_counter}]: "
                        f"{block['name']}({block.get('input', {})})"
                    )
                elif bt == "tool_result":
                    tc_id = block.get("tool_use_id", "")
                    tc_num = tool_id_to_number.get(tc_id, "?")
                    error_tag = " ERROR" if block.get("is_error") else ""
                    lines.append(
                        f"[msg #{msg_num} TOOL_RESULT #{tc_num}{error_tag}]: "
                        f"{block.get('content', '')}"
                    )
        return tool_call_counter

    def _get_thinking(self, args: dict) -> str:
        session = self._get_session(args["session_id"])
        target_msg_num = args["message_number"]

        messages = self.renderer._get_messages_from_session(session)  # noqa: SLF001
        if target_msg_num >= len(messages):
            msg = f"Message #{target_msg_num} not found"
            raise ValueError(msg)

        msg_data = messages[target_msg_num]
        content = msg_data.get("content", [])
        if not isinstance(content, list):
            return "No thinking blocks in this message."

        thinking_parts = [
            block["thinking"] for block in content if block.get("type") == "thinking"
        ]

        if not thinking_parts:
            return "No thinking blocks in this message."

        return "\n\n".join(thinking_parts)
