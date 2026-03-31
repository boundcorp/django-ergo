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
            {
                "type": "text",
                "text": "There are 2 files in /tmp: file1.txt and file2.txt.",
            }
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
        assert "thinking" not in result.lower()
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
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "ok",
                        "is_error": False,
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Read",
                        "input": {"path": "/tmp/x"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": "data",
                        "is_error": False,
                    },
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
            metadata={
                "slug": "sorted-brewing-mitten",
                "last_prompt": "check the files",
            },
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


class TestCustomRenderer:
    def test_custom_raises_on_sync_call(self):
        renderer = ConversationRenderer(detail="custom", custom_fn=lambda s: "x")
        with pytest.raises(ValueError, match="render_async"):
            renderer.render_messages([{"role": "user", "content": "hi"}])
