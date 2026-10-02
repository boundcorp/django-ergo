# Conversation Renderer and History Toolkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace naive full-transcript rendering with a tiered ConversationRenderer (headline/skeleton/full), add ChatWithHistoryToolkit for agent drill-down into past conversations, and integrate with the existing pipeline and runner.

**Architecture:** ConversationRenderer handles detail-level rendering of conversations. ChatWithHistoryToolkit wraps multiple sessions with scoped tools for drill-down. The runner gets an `extra_tools` parameter. Pipelines use the renderer instead of `_format_conversation_as_text`.

**Tech Stack:** Django 4.2+, existing conversation models, ToolAdapter for schema generation

**Spec:** `docs/superpowers/specs/2026-03-30-conversation-renderer-and-history-toolkit-design.md`

---

## File Structure

```
src/django_ergo/conversation/
├── renderer.py              # ConversationRenderer with headline/skeleton/full/custom strategies
├── history_toolkit.py       # ChatWithHistoryToolkit with scoped drill-down tools
├── pipelines.py             # Modified: use renderer instead of _format_conversation_as_text
├── runner.py                # Modified: add extra_tools parameter
├── importers/
│   └── claude_cli.py        # Modified: extract slug, last_prompt, turn_duration metadata

tests/
├── test_conversation_renderer.py    # Renderer detail levels, caching, custom strategies
├── test_conversation_toolkit.py     # ChatWithHistoryToolkit tools and execution
├── test_conversation_runner.py      # Modified: extra_tools integration tests
├── test_conversation_pipelines.py   # Modified: verify renderer integration
├── test_conversation_import.py      # Modified: verify metadata extraction
```

---

### Task 1: ConversationRenderer — Core Rendering

**Files:**
- Create: `src/django_ergo/conversation/renderer.py`
- Test: `tests/test_conversation_renderer.py`

- [ ] **Step 1: Write failing tests for all three detail levels**

```python
# tests/test_conversation_renderer.py
"""Tests for ConversationRenderer detail levels."""
import pytest

from django_ergo.conversation.renderer import ConversationRenderer


# Sample Claude-format messages (content block arrays)
SAMPLE_MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "What files are in /tmp?"}]},
    {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "Bash",
                "input": {"command": "ls /tmp"},
            }
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01",
                "content": "file1.txt\nfile2.txt",
                "is_error": False,
            }
        ],
    },
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "There are 2 files in /tmp: file1.txt and file2.txt."}
        ],
    },
]

MESSAGES_WITH_THINKING = [
    {"role": "user", "content": [{"type": "text", "text": "Complex question"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Let me reason through this carefully..."},
            {"type": "text", "text": "Here is my answer."},
        ],
    },
]

OPENAI_MESSAGES = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "Hi there!"},
]


class TestFullRenderer:
    def test_includes_everything(self):
        renderer = ConversationRenderer(detail="full")
        result = renderer.render_messages(SAMPLE_MESSAGES)
        assert "[msg #0 USER]" in result
        assert "What files are in /tmp?" in result
        assert "Bash" in result
        assert '{"command": "ls /tmp"}' in result or "command" in result
        assert "file1.txt\nfile2.txt" in result or "file1.txt" in result
        assert "There are 2 files" in result

    def test_includes_thinking(self):
        renderer = ConversationRenderer(detail="full")
        result = renderer.render_messages(MESSAGES_WITH_THINKING)
        assert "thinking" in result.lower()
        assert "Let me reason through this carefully" in result

    def test_openai_string_content(self):
        renderer = ConversationRenderer(detail="full")
        result = renderer.render_messages(OPENAI_MESSAGES)
        assert "Hello" in result
        assert "Hi there!" in result


class TestSkeletonRenderer:
    def test_includes_user_and_assistant_text(self):
        renderer = ConversationRenderer(detail="skeleton")
        result = renderer.render_messages(SAMPLE_MESSAGES)
        assert "What files are in /tmp?" in result
        assert "There are 2 files" in result

    def test_tool_calls_are_summarized(self):
        renderer = ConversationRenderer(detail="skeleton")
        result = renderer.render_messages(SAMPLE_MESSAGES)
        assert "[tool_call #1:" in result
        assert "Bash" in result
        # Should NOT have full tool input/output
        assert "file1.txt\nfile2.txt" not in result

    def test_tool_results_show_line_count(self):
        renderer = ConversationRenderer(detail="skeleton")
        result = renderer.render_messages(SAMPLE_MESSAGES)
        assert "[tool_result #1:" in result or "TOOL_RESULT #1" in result
        assert "(2 lines)" in result

    def test_thinking_omitted(self):
        renderer = ConversationRenderer(detail="skeleton")
        result = renderer.render_messages(MESSAGES_WITH_THINKING)
        assert "thinking" not in result.lower() or "thinking" not in result
        assert "Let me reason" not in result
        assert "Here is my answer" in result

    def test_messages_are_numbered(self):
        renderer = ConversationRenderer(detail="skeleton")
        result = renderer.render_messages(SAMPLE_MESSAGES)
        assert "[msg #0" in result
        assert "[msg #1" in result
        assert "[msg #2" in result
        assert "[msg #3" in result

    def test_tool_calls_are_numbered(self):
        two_tool_messages = [
            {"role": "user", "content": [{"type": "text", "text": "Do two things"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {"path": "/tmp/x"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t2", "content": "data", "is_error": False},
                ],
            },
        ]
        renderer = ConversationRenderer(detail="skeleton")
        result = renderer.render_messages(two_tool_messages)
        assert "#1:" in result
        assert "#2:" in result


class TestHeadlineRenderer:
    def test_first_user_message_truncated(self):
        renderer = ConversationRenderer(detail="headline")
        long_msg = "x" * 2000
        messages = [{"role": "user", "content": [{"type": "text", "text": long_msg}]}]
        result = renderer.render_messages(messages)
        max_len = 1050  # 1000 chars + label overhead
        assert len(result) < max_len

    def test_includes_metadata(self):
        renderer = ConversationRenderer(detail="headline")
        result = renderer.render_messages(
            SAMPLE_MESSAGES,
            metadata={"slug": "sorted-brewing-mitten", "last_prompt": "check the files"},
        )
        assert "sorted-brewing-mitten" in result
        assert "check the files" in result
        assert "What files are in /tmp?" in result

    def test_works_without_metadata(self):
        renderer = ConversationRenderer(detail="headline")
        result = renderer.render_messages(SAMPLE_MESSAGES)
        assert "What files are in /tmp?" in result


class TestDefaultDetail:
    def test_default_is_skeleton(self):
        renderer = ConversationRenderer()
        assert renderer.detail == "skeleton"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_conversation_renderer.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ConversationRenderer**

Create `src/django_ergo/conversation/renderer.py`:

```python
"""Conversation renderer with tiered detail levels.

Detail levels:
- headline: slug + last_prompt (if available) + first user message (1000 chars)
- skeleton: user messages + assistant text, tool calls summarized, thinking omitted
- full: everything verbatim
- custom: async callable for LLM-powered rendering
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationSession

HEADLINE_MAX_CHARS = 1000


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

    def render_messages(self, messages: list[dict], metadata: dict | None = None) -> str:
        """Render from reconstructed message dicts."""
        if self.detail == "headline":
            return self._render_headline(messages, metadata or {})
        if self.detail == "skeleton":
            return self._render_skeleton(messages)
        if self.detail == "full":
            return self._render_full(messages)
        if self.detail == "custom":
            msg = "Custom rendering requires render_async(). Use render_async() instead."
            raise ValueError(msg)
        msg = f"Unknown detail level: {self.detail}"
        raise ValueError(msg)

    async def render_async(self, session: ConversationSession, **kwargs) -> str:
        """Async render — required for custom strategies."""
        if self.detail == "custom" and self.custom_fn:
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
                    content.append({
                        "type": "tool_use", "id": block.tool_use_id,
                        "name": block.tool_name, "input": block.tool_input or {},
                    })
                elif block.block_type == "tool_result":
                    content.append({
                        "type": "tool_result", "tool_use_id": block.tool_result_for,
                        "content": block.tool_result_content or "", "is_error": block.is_error,
                    })
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
            lines.append(f"[last_prompt: \"{last_prompt}\"]")

        first_user_text = self._find_first_user_text(messages)
        if first_user_text:
            truncated = first_user_text[:HEADLINE_MAX_CHARS]
            lines.append(f"First message: {truncated}")

        return " ".join(lines) if len(lines) <= 2 and not first_user_text else "\n".join(lines)

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
                msg_parts = []
                for block in content:
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
                        if isinstance(result_content, str):
                            line_count = len(result_content.strip().splitlines())
                        else:
                            line_count = 1
                        error_tag = " ERROR" if block.get("is_error") else ""
                        msg_parts.append(
                            f"[tool_result #{tc_num}{error_tag}: ({line_count} lines)]"
                        )
                    elif block_type == "thinking":
                        pass  # omit thinking in skeleton
                if msg_parts:
                    lines.append(f"[msg #{msg_num} {role}]: {' '.join(msg_parts)}")
            elif content is not None:
                lines.append(f"[msg #{msg_num} {role}]: {content}")

        return "\n".join(lines)

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
                        lines.append(f"[msg #{msg_num} {role} thinking]: {block['thinking']}")
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
            if len(val_str) > 50:
                val_str = val_str[:47] + "..."
            parts.append(f'{key}="{val_str}"')
        return ", ".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_conversation_renderer.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/renderer.py tests/test_conversation_renderer.py
git commit -m "feat: add ConversationRenderer with headline/skeleton/full detail levels"
```

---

### Task 2: ChatWithHistoryToolkit — Scoped Tools

**Files:**
- Create: `src/django_ergo/conversation/history_toolkit.py`
- Test: `tests/test_conversation_toolkit.py`

- [ ] **Step 1: Write failing tests for toolkit**

```python
# tests/test_conversation_toolkit.py
"""Tests for ChatWithHistoryToolkit scoped tools."""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit
from django_ergo.conversation.models import (
    ClaudeContentBlock,
    ClaudeMessage,
    ConversationSession,
)
from django_ergo.conversation.renderer import ConversationRenderer

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture
def session_a(user):
    s = ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="completed",
        metadata={"slug": "red-fox-jumps"},
    )
    m0 = ClaudeMessage.objects.create(session=s, role="user", sequence=0)
    ClaudeContentBlock.objects.create(message=m0, block_type="text", sequence=0, text="List files")
    m1 = ClaudeMessage.objects.create(session=s, role="assistant", sequence=1, stop_reason="tool_use")
    ClaudeContentBlock.objects.create(
        message=m1, block_type="tool_use", sequence=0,
        tool_use_id="toolu_01", tool_name="Bash", tool_input={"command": "ls /tmp"},
    )
    m2 = ClaudeMessage.objects.create(session=s, role="user", sequence=2)
    ClaudeContentBlock.objects.create(
        message=m2, block_type="tool_result", sequence=0,
        tool_result_for="toolu_01", tool_result_content="file1.txt\nfile2.txt",
    )
    m3 = ClaudeMessage.objects.create(session=s, role="assistant", sequence=3, stop_reason="end_turn")
    ClaudeContentBlock.objects.create(
        message=m3, block_type="text", sequence=0, text="Found 2 files.",
    )
    return s


@pytest.fixture
def session_b(user):
    s = ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="completed",
    )
    m0 = ClaudeMessage.objects.create(session=s, role="user", sequence=0)
    ClaudeContentBlock.objects.create(message=m0, block_type="text", sequence=0, text="Explain auth")
    m1 = ClaudeMessage.objects.create(session=s, role="assistant", sequence=1, stop_reason="end_turn")
    ClaudeContentBlock.objects.create(
        message=m1, block_type="thinking", sequence=0, thinking="Let me think about auth...",
    )
    ClaudeContentBlock.objects.create(
        message=m1, block_type="text", sequence=1, text="Auth uses JWT tokens.",
    )
    return s


@pytest.fixture
def toolkit(session_a, session_b):
    return ChatWithHistoryToolkit(sessions=[session_a, session_b])


class TestRenderOverview:
    def test_includes_all_sessions(self, toolkit, session_a, session_b):
        overview = toolkit.render_overview()
        assert str(session_a.id) in overview
        assert str(session_b.id) in overview
        assert "List files" in overview
        assert "Explain auth" in overview

    def test_uses_skeleton_by_default(self, toolkit):
        overview = toolkit.render_overview()
        # Skeleton omits thinking
        assert "Let me think" not in overview
        # Skeleton summarizes tool calls
        assert "[tool_call #1:" in overview


class TestHasTool:
    def test_recognizes_own_tools(self, toolkit):
        assert toolkit.has_tool("history_view_conversation") is True
        assert toolkit.has_tool("history_get_tool_call") is True
        assert toolkit.has_tool("history_get_message_range") is True
        assert toolkit.has_tool("history_get_thinking") is True

    def test_rejects_unknown_tools(self, toolkit):
        assert toolkit.has_tool("search_kb") is False
        assert toolkit.has_tool("view_conversation") is False


class TestExecuteViewConversation:
    def test_view_skeleton(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_view_conversation", {"session_id": str(session_a.id)}
        )
        assert "List files" in result
        assert "[tool_call #1:" in result

    def test_view_full(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_view_conversation",
            {"session_id": str(session_a.id), "detail": "full"},
        )
        assert "file1.txt" in result

    def test_invalid_session(self, toolkit):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "history_view_conversation", {"session_id": "nonexistent"}
            )


class TestExecuteGetToolCall:
    def test_get_tool_call(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_tool_call",
            {"session_id": str(session_a.id), "tool_call_number": 1},
        )
        assert "Bash" in result
        assert "ls /tmp" in result
        assert "file1.txt" in result

    def test_invalid_tool_call_number(self, toolkit, session_a):
        with pytest.raises(ValueError, match="not found"):
            toolkit.execute_tool(
                "history_get_tool_call",
                {"session_id": str(session_a.id), "tool_call_number": 99},
            )


class TestExecuteGetMessageRange:
    def test_get_range(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_message_range",
            {"session_id": str(session_a.id), "start": 0, "end": 1},
        )
        assert "List files" in result
        assert "Bash" in result

    def test_single_message(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_message_range",
            {"session_id": str(session_a.id), "start": 3, "end": 3},
        )
        assert "Found 2 files" in result


class TestExecuteGetThinking:
    def test_get_thinking(self, toolkit, session_b):
        result = toolkit.execute_tool(
            "history_get_thinking",
            {"session_id": str(session_b.id), "message_number": 1},
        )
        assert "Let me think about auth" in result

    def test_no_thinking_in_message(self, toolkit, session_a):
        result = toolkit.execute_tool(
            "history_get_thinking",
            {"session_id": str(session_a.id), "message_number": 0},
        )
        assert "no thinking" in result.lower() or result == ""


class TestGetToolsSchema:
    def test_returns_tool_configs(self, toolkit):
        from django_ergo.conversation.adapters import ClaudeToolAdapter

        adapter = ClaudeToolAdapter()
        schemas = toolkit.get_tools_schema(adapter)
        tool_names = [s["name"] for s in schemas]
        assert "history_view_conversation" in tool_names
        assert "history_get_tool_call" in tool_names
        assert "history_get_message_range" in tool_names
        assert "history_get_thinking" in tool_names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_conversation_toolkit.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ChatWithHistoryToolkit**

Create `src/django_ergo/conversation/history_toolkit.py`:

```python
"""ChatWithHistoryToolkit — scoped tools for agent drill-down into past conversations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
            "session_id": {"type": "string", "required": True, "description": "UUID of the conversation session"},
            "detail": {"type": "string", "required": False, "description": "Detail level: headline, skeleton, or full. Default: skeleton"},
        },
    },
    {
        "name": "history_get_tool_call",
        "description": "Get the full input and output for a specific tool call by number",
        "parameters": {
            "session_id": {"type": "string", "required": True, "description": "UUID of the conversation session"},
            "tool_call_number": {"type": "integer", "required": True, "description": "Tool call number (e.g., 1, 2, 3)"},
        },
    },
    {
        "name": "history_get_message_range",
        "description": "Get full-detail rendering of a range of messages by number",
        "parameters": {
            "session_id": {"type": "string", "required": True, "description": "UUID of the conversation session"},
            "start": {"type": "integer", "required": True, "description": "Start message number (inclusive)"},
            "end": {"type": "integer", "required": True, "description": "End message number (inclusive)"},
        },
    },
    {
        "name": "history_get_thinking",
        "description": "Get the thinking block content for a specific assistant message",
        "parameters": {
            "session_id": {"type": "string", "required": True, "description": "UUID of the conversation session"},
            "message_number": {"type": "integer", "required": True, "description": "Message number"},
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

        messages = self.renderer._get_messages_from_session(session)
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

        messages = self.renderer._get_messages_from_session(session)
        selected = messages[start : end + 1]

        full_renderer = ConversationRenderer(detail="full")
        # Render only selected messages but preserve original numbering
        lines = []
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
            content = msg.get("content")
            if isinstance(content, str):
                lines.append(f"[msg #{msg_num} {role}]: {content}")
            elif isinstance(content, list):
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text":
                        lines.append(f"[msg #{msg_num} {role}]: {block['text']}")
                    elif bt == "thinking":
                        lines.append(f"[msg #{msg_num} {role} thinking]: {block['thinking']}")
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

        return "\n".join(lines)

    def _get_thinking(self, args: dict) -> str:
        session = self._get_session(args["session_id"])
        target_msg_num = args["message_number"]

        messages = self.renderer._get_messages_from_session(session)
        if target_msg_num >= len(messages):
            msg = f"Message #{target_msg_num} not found"
            raise ValueError(msg)

        msg_data = messages[target_msg_num]
        content = msg_data.get("content", [])
        if not isinstance(content, list):
            return "No thinking blocks in this message."

        thinking_parts = []
        for block in content:
            if block.get("type") == "thinking":
                thinking_parts.append(block["thinking"])

        if not thinking_parts:
            return "No thinking blocks in this message."

        return "\n\n".join(thinking_parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_conversation_toolkit.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/history_toolkit.py tests/test_conversation_toolkit.py
git commit -m "feat: add ChatWithHistoryToolkit with scoped drill-down tools"
```

---

### Task 3: Runner Integration — extra_tools Parameter

**Files:**
- Modify: `src/django_ergo/conversation/runner.py`
- Modify: `tests/test_conversation_runner.py`

- [ ] **Step 1: Write failing tests for extra_tools**

Add to `tests/test_conversation_runner.py`:

```python
class TestExtraTools:
    @patch("django_ergo.conversation.runner.tool_registry")
    def test_extra_tools_handled_first(self, mock_registry):
        """When extra_tools has the tool, it handles it — not the global registry."""
        from unittest.mock import MagicMock

        extra = MagicMock()
        extra.has_tool.return_value = True
        extra.execute_tool.return_value = "toolkit result"

        # Verify has_tool is checked — we can't easily test the full async flow
        # without pytest-asyncio, so test the helper
        assert extra.has_tool("history_view_conversation") is True
        extra.execute_tool.assert_not_called()

    def test_extra_tools_none_is_backward_compatible(self):
        """run_conversation_turn still works without extra_tools."""
        import inspect

        from django_ergo.conversation.runner import run_conversation_turn

        sig = inspect.signature(run_conversation_turn)
        params = list(sig.parameters.keys())
        assert "extra_tools" in params
        assert sig.parameters["extra_tools"].default is None
```

- [ ] **Step 2: Run tests to verify the second test fails**

Run: `python -m pytest tests/test_conversation_runner.py::TestExtraTools -v`
Expected: FAIL — `extra_tools` parameter doesn't exist yet

- [ ] **Step 3: Update runner with extra_tools support**

Update `src/django_ergo/conversation/runner.py`:

```python
"""Conversation turn runner — tool execution loop above the engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django_ergo.tools import tool_registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.engine import EngineResponse
    from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit
    from django_ergo.conversation.models import ConversationSession


@dataclass
class PendingApproval:
    """Yielded when a tool requires user approval before execution."""

    tool_use_id: str
    tool_name: str
    arguments: dict


def _tool_requires_approval(tool_name: str, workflow) -> bool:
    tool_config = tool_registry.get_tool(tool_name)
    if not tool_config or not tool_config.requires_approval:
        return False
    if workflow:
        tools_config = workflow.get_tools_config()
        approved = tools_config.get("approved_tools", [])
        if tool_name in approved:
            return False
    return True


async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: ChatWithHistoryToolkit | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    adapter = engine.get_tool_adapter()
    async for response in engine.send(session, message):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)

            # Check extra_tools first (e.g., history toolkit)
            if extra_tools and extra_tools.has_tool(name):
                result = extra_tools.execute_tool(name, args)
                async for continuation in engine.submit_tool_result(
                    session, response.tool_use["id"], result
                ):
                    yield continuation
                continue

            if _tool_requires_approval(name, session.workflow):
                yield PendingApproval(
                    tool_use_id=response.tool_use["id"], tool_name=name, arguments=args
                )
                return
            result = tool_registry.execute_tool(
                name=name, user=session.user, arguments=args, approved=True
            )
            async for continuation in engine.submit_tool_result(
                session, response.tool_use["id"], result
            ):
                yield continuation
        else:
            yield response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_conversation_runner.py -v`
Expected: All PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/runner.py tests/test_conversation_runner.py
git commit -m "feat: add extra_tools parameter to run_conversation_turn for toolkit integration"
```

---

### Task 4: Pipeline Integration — Use Renderer

**Files:**
- Modify: `src/django_ergo/conversation/pipelines.py`
- Modify: `tests/test_conversation_pipelines.py`

- [ ] **Step 1: Write failing test for renderer integration**

Add to `tests/test_conversation_pipelines.py`:

```python
class TestRendererIntegration:
    def test_pipeline_uses_skeleton_by_default(self, session_with_messages):
        from django_ergo.conversation.renderer import ConversationRenderer

        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ]
        engine.generate = AsyncMock(
            return_value=EngineResponse(event_type="done", text="Summary"),
        )

        result = async_to_sync(run_conversation_pipeline)(
            session_with_messages, engine, system="Summarize.",
        )

        call_kwargs = engine.generate.call_args.kwargs
        # Should use skeleton format (numbered messages)
        assert "[msg #0" in call_kwargs["prompt"]

    def test_pipeline_accepts_custom_renderer(self, session_with_messages):
        from django_ergo.conversation.renderer import ConversationRenderer

        engine = MagicMock()
        engine.reconstruct_messages.return_value = [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        ]
        engine.generate = AsyncMock(
            return_value=EngineResponse(event_type="done", text="Summary"),
        )

        renderer = ConversationRenderer(detail="full")
        result = async_to_sync(run_conversation_pipeline)(
            session_with_messages, engine, system="Summarize.", renderer=renderer,
        )

        call_kwargs = engine.generate.call_args.kwargs
        # Full detail should be in prompt
        assert "[msg #0" in call_kwargs["prompt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_conversation_pipelines.py::TestRendererIntegration -v`
Expected: FAIL — `renderer` parameter doesn't exist

- [ ] **Step 3: Update pipelines to use renderer**

Update `run_conversation_pipeline` in `src/django_ergo/conversation/pipelines.py`:

```python
async def run_conversation_pipeline(
    session: ConversationSession,
    engine: Engine,
    system: str,
    response_model: type | None = None,
    renderer: ConversationRenderer | None = None,
) -> EngineResponse:
    """Run a conversation through a one-shot generative pipeline.

    Uses ConversationRenderer for token-efficient formatting.
    Defaults to skeleton detail level.
    """
    if renderer is None:
        renderer = ConversationRenderer(detail="skeleton")

    messages = engine.reconstruct_messages(session)
    transcript = renderer.render_messages(messages)

    prompt = f"Here is a conversation transcript:\n\n{transcript}"

    return await engine.generate(
        prompt=prompt,
        system=system,
        response_model=response_model,
    )
```

Add the import at the top of pipelines.py:

```python
from django_ergo.conversation.renderer import ConversationRenderer
```

Remove or deprecate `_format_conversation_as_text` — keep it for backward compat but add a deprecation note:

```python
def _format_conversation_as_text(messages: list[dict]) -> str:
    """Deprecated: use ConversationRenderer(detail='full').render_messages() instead."""
    return ConversationRenderer(detail="full").render_messages(messages)
```

- [ ] **Step 4: Run all pipeline tests**

Run: `python -m pytest tests/test_conversation_pipelines.py -v`
Expected: All PASS (existing tests may need minor adjustments for new format)

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/pipelines.py tests/test_conversation_pipelines.py
git commit -m "feat: integrate ConversationRenderer into pipelines, default to skeleton"
```

---

### Task 5: CLI Import Metadata Extraction

**Files:**
- Modify: `src/django_ergo/conversation/importers/claude_cli.py`
- Modify: `tests/test_conversation_import.py`

- [ ] **Step 1: Write failing test for metadata extraction**

Add to `tests/test_conversation_import.py`:

```python
SAMPLE_SESSION_WITH_METADATA = [
    {"type": "user", "message": {"role": "user", "content": "Hello"}, "uuid": "msg-001", "sessionId": "session-abc", "slug": "red-fox-jumps"},
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}], "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}, "uuid": "msg-002", "slug": "red-fox-jumps"},
    {"type": "system", "subtype": "turn_duration", "durationMs": 5000, "messageCount": 2, "slug": "red-fox-jumps"},
    {"type": "last-prompt", "lastPrompt": "Hello", "sessionId": "session-abc"},
]


class TestMetadataExtraction:
    def test_slug_extracted(self, user):
        session = async_to_sync(ClaudeCLIImporter().import_conversation)(
            SAMPLE_SESSION_WITH_METADATA, user
        )
        assert session.metadata.get("slug") == "red-fox-jumps"

    def test_last_prompt_extracted(self, user):
        session = async_to_sync(ClaudeCLIImporter().import_conversation)(
            SAMPLE_SESSION_WITH_METADATA, user
        )
        assert session.metadata.get("last_prompt") == "Hello"

    def test_duration_extracted(self, user):
        session = async_to_sync(ClaudeCLIImporter().import_conversation)(
            SAMPLE_SESSION_WITH_METADATA, user
        )
        assert session.metadata.get("total_duration_ms") == 5000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_conversation_import.py::TestMetadataExtraction -v`
Expected: FAIL — metadata fields not populated

- [ ] **Step 3: Update importer to extract metadata**

In `src/django_ergo/conversation/importers/claude_cli.py`, update `import_conversation` to do a first pass extracting metadata before processing messages:

```python
async def import_conversation(self, data: list[dict], user, workflow=None) -> ConversationSession:
    session_id = ""
    slug = None
    last_prompt = None
    total_duration_ms = 0

    # First pass: extract metadata from all event types
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

    session = await ConversationSession.objects.acreate(
        user=user, workflow=workflow, engine_type="claude", transport_type="cli",
        status="paused", session_id=session_id, metadata=metadata,
    )

    # Second pass: import messages (existing logic, unchanged)
    for seq, msg_data in enumerate(data):
        msg_type = msg_data.get("type")
        if msg_type not in ("user", "assistant"):
            continue
        # ... rest unchanged ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_conversation_import.py -v`
Expected: All PASS (existing + new)

- [ ] **Step 5: Commit**

```bash
git add src/django_ergo/conversation/importers/claude_cli.py tests/test_conversation_import.py
git commit -m "feat: extract slug, last_prompt, duration from Claude CLI session metadata"
```

---

### Task 6: Verify Full Suite and Push

- [ ] **Step 1: Run full conversation test suite**

Run: `python -m pytest tests/test_conversation_*.py tests/test_import_command.py -v`
Expected: All PASS

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `python -m pytest tests/test_settings.py tests/test_tool_registry_unit.py -v`
Expected: All PASS

- [ ] **Step 3: Commit any remaining changes and push**

```bash
git push
```
