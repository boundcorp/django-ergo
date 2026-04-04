# KB Absorption Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `absorb_conversation()` — a pipeline function that reviews a conversation and proposes KB changes via KBSuggestToolkit, returning suggestions for human review.

**Architecture:** The function renders the source conversation, builds a system prompt with KB context (name, description, TOC), creates a temporary absorption session, and runs a conversation turn with KBSuggestToolkit + KBToolkit. The agent calls suggest tools which accumulate in memory. Returns the KBSuggestToolkit.

**Tech Stack:** Django 4.2+, existing toolkits, ConversationRenderer, conversation runner

**Spec:** `docs/superpowers/specs/2026-04-04-kb-absorption-pipeline-design.md`

---

## File Structure

```
src/django_ergo/
├── kb_pipelines.py              # NEW: absorb_conversation() + ABSORB_SYSTEM prompt

tests/
├── test_kb_pipelines.py         # NEW: tests with mocked engine
```

---

### Task 1: absorb_conversation Implementation

**Files:**
- Create: `src/django_ergo/kb_pipelines.py`
- Create: `tests/test_kb_pipelines.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_kb_pipelines.py
"""Tests for KB pipelines — absorb_conversation."""
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.models import ConversationSession
from django_ergo.kb_pipelines import ABSORB_SYSTEM
from django_ergo.kb_pipelines import absorb_conversation
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def source_session(user):
    session = ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="completed",
    )
    # Add some messages via Claude models so the renderer has something to render
    from django_ergo.conversation.models import ClaudeContentBlock
    from django_ergo.conversation.models import ClaudeMessage

    m0 = ClaudeMessage.objects.create(session=session, role="user", sequence=0)
    ClaudeContentBlock.objects.create(
        message=m0, block_type="text", sequence=0,
        text="I prefer morning deployments and use pytest for testing.",
    )
    m1 = ClaudeMessage.objects.create(
        session=session, role="assistant", sequence=1, stop_reason="end_turn",
    )
    ClaudeContentBlock.objects.create(
        message=m1, block_type="text", sequence=1,
        text="Got it, I'll remember your preferences.",
    )
    return session


@pytest.fixture()
def target_kb(user):
    kb = Knowledgebase.objects.create(
        name="Personal Notes",
        description="Facts about the user's preferences and habits",
        owner_id=str(user.id),
    )
    Article.objects.create(
        knowledgebase=kb, hierarchy_code="0", title="Tools",
        content="The user likes VS Code.",
    )
    return kb


class TestAbsorbSystem:
    def test_prompt_template_has_placeholders(self):
        assert "{kb_name}" in ABSORB_SYSTEM
        assert "{kb_description}" in ABSORB_SYSTEM
        assert "{kb_toc}" in ABSORB_SYSTEM

    def test_prompt_can_be_formatted(self, target_kb):
        formatted = ABSORB_SYSTEM.format(
            kb_name=target_kb.name,
            kb_description=target_kb.description,
            kb_toc=target_kb.get_table_of_contents(),
        )
        assert "Personal Notes" in formatted
        assert "preferences and habits" in formatted


class TestAbsorbConversation:
    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_returns_suggest_toolkit(self, mock_runner, source_session, target_kb):
        """absorb_conversation returns a KBSuggestToolkit."""
        from django_ergo.kb_suggest_toolkit import KBSuggestToolkit

        async def fake_runner(*args, **kwargs):
            # Simulate agent calling kb_suggest_create
            toolkits = kwargs.get("extra_tools", [])
            for tk in toolkits:
                if tk.has_tool("kb_suggest_create"):
                    tk.execute_tool("kb_suggest_create", {
                        "title": "Deploy Preferences",
                        "content": "User prefers morning deployments.",
                    })
            # Yield a done response
            yield EngineResponse(event_type="done", text="Absorbed.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        result = async_to_sync(absorb_conversation)(
            source_session, target_kb, engine,
        )

        assert isinstance(result, KBSuggestToolkit)
        suggestions = result.get_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["action"] == "create"
        assert suggestions[0]["title"] == "Deploy Preferences"

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_creates_absorption_session(self, mock_runner, source_session, target_kb):
        """absorb_conversation creates a temporary session for the absorption agent."""

        async def fake_runner(*args, **kwargs):
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        initial_count = ConversationSession.objects.count()
        async_to_sync(absorb_conversation)(source_session, target_kb, engine)
        assert ConversationSession.objects.count() == initial_count + 1

        absorption_session = ConversationSession.objects.order_by("-created_at").first()
        assert absorption_session.metadata.get("absorption_source") == str(source_session.id)

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_prompt_includes_conversation_transcript(self, mock_runner, source_session, target_kb):
        """The message sent to the engine includes the rendered conversation."""
        captured_message = {}

        async def fake_runner(engine, session, message, **kwargs):
            captured_message["msg"] = message
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        async_to_sync(absorb_conversation)(source_session, target_kb, engine)

        assert "morning deployments" in captured_message["msg"]

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_prompt_includes_kb_context(self, mock_runner, source_session, target_kb):
        """The message includes KB name, description, and TOC."""
        captured_message = {}

        async def fake_runner(engine, session, message, **kwargs):
            captured_message["msg"] = message
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        async_to_sync(absorb_conversation)(source_session, target_kb, engine)

        msg = captured_message["msg"]
        assert "Personal Notes" in msg
        assert "preferences and habits" in msg

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_custom_system_prompt(self, mock_runner, source_session, target_kb):
        """Custom system prompt overrides the default."""
        captured_message = {}

        async def fake_runner(engine, session, message, **kwargs):
            captured_message["msg"] = message
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        async_to_sync(absorb_conversation)(
            source_session, target_kb, engine,
            system="Only extract deployment preferences.",
        )

        msg = captured_message["msg"]
        assert "Only extract deployment preferences" in msg

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_passes_both_toolkits(self, mock_runner, source_session, target_kb):
        """Both KBSuggestToolkit and KBToolkit are passed as extra_tools."""
        captured_tools = {}

        async def fake_runner(engine, session, message, extra_tools=None):
            captured_tools["tools"] = extra_tools
            yield EngineResponse(event_type="done", text="Done.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        async_to_sync(absorb_conversation)(source_session, target_kb, engine)

        tools = captured_tools["tools"]
        assert len(tools) == 2
        tool_types = {type(t).__name__ for t in tools}
        assert "KBSuggestToolkit" in tool_types
        assert "KBToolkit" in tool_types

    @patch("django_ergo.kb_pipelines.run_conversation_turn")
    def test_no_suggestions_returns_empty(self, mock_runner, source_session, target_kb):
        """If the agent makes no suggestions, the toolkit is empty."""

        async def fake_runner(*args, **kwargs):
            yield EngineResponse(event_type="done", text="Nothing to absorb.")

        mock_runner.side_effect = fake_runner

        engine = MagicMock()
        engine.engine_type = "claude"
        engine.get_tool_adapter.return_value = MagicMock()
        engine.get_tool_adapter.return_value.to_engine_schema.return_value = {"name": "mock"}

        result = async_to_sync(absorb_conversation)(
            source_session, target_kb, engine,
        )
        assert result.get_suggestions() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kb_pipelines.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement absorb_conversation**

```python
# src/django_ergo/kb_pipelines.py
"""KB pipelines — knowledge base operations driven by conversation analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django_ergo.conversation.renderer import ConversationRenderer
from django_ergo.conversation.runner import run_conversation_turn
from django_ergo.kb_suggest_toolkit import KBSuggestToolkit
from django_ergo.kb_toolkit import KBToolkit

if TYPE_CHECKING:
    from django_ergo.conversation.engine import Engine
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.models import Knowledgebase

ABSORB_SYSTEM = """\
You are a knowledge base curator. You are reviewing a conversation to extract \
knowledge worth preserving into the knowledge base described below.

Knowledge Base: {kb_name}
Description: {kb_description}

Current table of contents:
{kb_toc}

Your job:
- Identify facts, decisions, preferences, and context from the conversation \
that belong in this knowledge base
- Use kb_suggest_create to propose new articles for new topics
- Use kb_suggest_update to propose improvements to existing articles
- Use kb_suggest_delete if conversation reveals an article is outdated or wrong
- Only propose changes relevant to this KB's description
- Avoid duplicating information already in the KB
- Use the KB read tools to check existing content before suggesting updates

Be selective. Not everything in a conversation belongs in a knowledge base."""


async def absorb_conversation(
    session: ConversationSession,
    target_kb: Knowledgebase,
    engine: Engine,
    system: str | None = None,
    renderer: ConversationRenderer | None = None,
) -> KBSuggestToolkit:
    """Review a conversation and propose KB changes.

    Renders the conversation, runs an absorption agent that calls
    KBSuggestToolkit tools, and returns the toolkit with accumulated
    suggestions for human review.

    Args:
        session: The source conversation to absorb from.
        target_kb: The knowledge base to propose changes for.
        engine: Engine to power the absorption agent.
        system: Custom system prompt. If None, uses ABSORB_SYSTEM with KB context.
        renderer: Custom renderer for the source conversation. Defaults to skeleton.

    Returns:
        KBSuggestToolkit with accumulated suggestions. Call
        get_suggestions() to review, apply_suggestions() to apply.
    """
    from django_ergo.conversation.models import ConversationSession as SessionModel

    # Render the source conversation
    if renderer is None:
        renderer = ConversationRenderer(detail="skeleton")
    transcript = renderer.render(session)

    # Build the prompt
    if system is None:
        toc = target_kb.get_table_of_contents()
        system_text = ABSORB_SYSTEM.format(
            kb_name=target_kb.name,
            kb_description=target_kb.description,
            kb_toc=toc if toc else "(empty — no articles yet)",
        )
    else:
        system_text = system

    message = f"{system_text}\n\n---\n\nConversation to review:\n\n{transcript}"

    # Create toolkits
    suggest_toolkit = KBSuggestToolkit(knowledgebase=target_kb)
    read_toolkit = KBToolkit(knowledgebases=[target_kb])
    toolkits = [suggest_toolkit, read_toolkit]

    # Create temporary absorption session
    absorption_session = await SessionModel.objects.acreate(
        user=session.user,
        engine_type=engine.engine_type,
        transport_type="api",
        status="active",
        metadata={"absorption_source": str(session.id)},
    )

    # Run the absorption agent — drain all responses
    async for _response in run_conversation_turn(
        engine, absorption_session, message, extra_tools=toolkits,
    ):
        pass  # Toolkit tools are handled by the runner internally

    # Mark absorption session as completed
    absorption_session.status = "completed"
    await absorption_session.asave(update_fields=["status"])

    return suggest_toolkit
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kb_pipelines.py -v`
Expected: All PASS

- [ ] **Step 5: Run ruff**

Run: `ruff check --fix src/django_ergo/kb_pipelines.py tests/test_kb_pipelines.py && ruff format src/django_ergo/kb_pipelines.py tests/test_kb_pipelines.py`

- [ ] **Step 6: Commit**

```bash
git add src/django_ergo/kb_pipelines.py tests/test_kb_pipelines.py
git commit -m "feat: add absorb_conversation() pipeline for KB knowledge extraction"
```

---

### Task 2: Full Suite Verification

- [ ] **Step 1: Run all KB tests**

Run: `python -m pytest tests/test_kb_toolkit.py tests/test_kb_write_toolkit.py tests/test_kb_suggest_toolkit.py tests/test_kb_usage_tracking.py tests/test_kb_expert_example.py tests/test_kb_pipelines.py tests/test_toolkit_protocol.py -v`
Expected: All PASS

- [ ] **Step 2: Run broader regression check**

Run: `python -m pytest tests/test_conversation_runner.py tests/test_conversation_pipelines.py tests/test_settings.py tests/test_tool_registry_unit.py -v`
Expected: All PASS

- [ ] **Step 3: Push**

```bash
git push
```
