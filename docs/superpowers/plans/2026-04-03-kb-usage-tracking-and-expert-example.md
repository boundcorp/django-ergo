# KB Usage Tracking and Expert Example Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ConversationKBUsage` model to track which conversations use which KBs, add `get_bound_knowledgebases()` to toolkits, have the runner record usage automatically, and validate with an integration test.

**Architecture:** New M2M through-model links sessions to KBs with mode. Toolkit ABC gets a non-abstract `get_bound_knowledgebases()` method. KB toolkits override it. Runner calls it on each toolkit and records usage via `aget_or_create`.

**Tech Stack:** Django 4.2+, existing models, Toolkit ABC

**Spec:** `docs/superpowers/specs/2026-04-03-kb-usage-tracking-and-expert-example-design.md`

---

## File Structure

```
src/django_ergo/conversation/
├── models.py               # MODIFY: add KBUsageMode + ConversationKBUsage
├── toolkit.py              # MODIFY: add get_bound_knowledgebases()
├── runner.py               # MODIFY: record usage before send()

src/django_ergo/
├── kb_toolkit.py           # MODIFY: override get_bound_knowledgebases()
├── kb_write_toolkit.py     # MODIFY: override get_bound_knowledgebases()
├── kb_suggest_toolkit.py   # MODIFY: override get_bound_knowledgebases()

src/django_ergo/migrations/
├── 0006_conversationkbusage.py  # NEW: auto-generated migration

tests/
├── test_kb_usage_tracking.py    # NEW: model + toolkit method + runner integration
├── test_kb_expert_example.py    # NEW: full stack integration test
```

---

### Task 1: ConversationKBUsage Model + Migration

**Files:**
- Modify: `src/django_ergo/conversation/models.py`
- Create: `src/django_ergo/migrations/0006_conversationkbusage.py` (auto-generated)
- Create: `tests/test_kb_usage_tracking.py`

- [ ] **Step 1: Write failing tests for the model**

```python
# tests/test_kb_usage_tracking.py
"""Tests for ConversationKBUsage model and toolkit integration."""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationKBUsage
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import KBUsageMode
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user(username="testuser", password="testpass")


@pytest.fixture()
def kb(user):
    return Knowledgebase.objects.create(
        name="Test KB", description="For testing", owner_id=str(user.id),
    )


@pytest.fixture()
def session(user):
    return ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="active",
    )


class TestKBUsageMode:
    def test_choices(self):
        assert KBUsageMode.READ == "read"
        assert KBUsageMode.WRITE == "write"
        assert KBUsageMode.SUGGEST == "suggest"


class TestConversationKBUsageModel:
    def test_create_usage(self, session, kb):
        usage = ConversationKBUsage.objects.create(
            session=session, knowledgebase=kb, mode=KBUsageMode.READ,
        )
        assert usage.session == session
        assert usage.knowledgebase == kb
        assert usage.mode == "read"

    def test_unique_together(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session, knowledgebase=kb, mode=KBUsageMode.READ,
        )
        with pytest.raises(Exception):
            ConversationKBUsage.objects.create(
                session=session, knowledgebase=kb, mode=KBUsageMode.READ,
            )

    def test_same_kb_different_modes(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session, knowledgebase=kb, mode=KBUsageMode.READ,
        )
        ConversationKBUsage.objects.create(
            session=session, knowledgebase=kb, mode=KBUsageMode.WRITE,
        )
        assert ConversationKBUsage.objects.filter(session=session).count() == 2

    def test_session_reverse_relation(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session, knowledgebase=kb, mode=KBUsageMode.READ,
        )
        assert session.kb_usages.count() == 1

    def test_kb_reverse_relation(self, session, kb):
        ConversationKBUsage.objects.create(
            session=session, knowledgebase=kb, mode=KBUsageMode.READ,
        )
        assert kb.conversation_usages.count() == 1


class TestToolkitGetBoundKnowledgebases:
    def test_kb_toolkit_returns_read(self, kb):
        from django_ergo.kb_toolkit import KBToolkit

        toolkit = KBToolkit(knowledgebases=[kb])
        bindings = toolkit.get_bound_knowledgebases()
        assert len(bindings) == 1
        assert bindings[0] == (kb, "read")

    def test_kb_toolkit_multiple_kbs(self, user):
        from django_ergo.kb_toolkit import KBToolkit

        kb2 = Knowledgebase.objects.create(
            name="KB 2", description="Second", owner_id=str(user.id),
        )
        kb3 = Knowledgebase.objects.create(
            name="KB 3", description="Third", owner_id=str(user.id),
        )
        toolkit = KBToolkit(knowledgebases=[kb2, kb3])
        bindings = toolkit.get_bound_knowledgebases()
        assert len(bindings) == 2
        assert all(mode == "read" for _, mode in bindings)

    def test_kb_write_toolkit_returns_write(self, kb):
        from django_ergo.kb_write_toolkit import KBWriteToolkit

        toolkit = KBWriteToolkit(knowledgebase=kb)
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == [(kb, "write")]

    def test_kb_suggest_toolkit_returns_suggest(self, kb):
        from django_ergo.kb_suggest_toolkit import KBSuggestToolkit

        toolkit = KBSuggestToolkit(knowledgebase=kb)
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == [(kb, "suggest")]

    def test_history_toolkit_returns_empty(self, session):
        from django_ergo.conversation.history_toolkit import ChatWithHistoryToolkit

        toolkit = ChatWithHistoryToolkit(sessions=[session])
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == []

    def test_default_returns_empty(self):
        from django_ergo.conversation.toolkit import Toolkit

        class FakeToolkit(Toolkit):
            def has_tool(self, n):
                return False

            def execute_tool(self, n, a):
                return ""

            def get_tools_schema(self, a):
                return []

            def render_overview(self):
                return ""

        assert FakeToolkit().get_bound_knowledgebases() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kb_usage_tracking.py -v`
Expected: FAIL — `ImportError` (KBUsageMode doesn't exist yet)

- [ ] **Step 3: Add model to conversation/models.py**

Add at the end of `src/django_ergo/conversation/models.py`:

```python
class KBUsageMode(models.TextChoices):
    READ = "read", "Read"
    WRITE = "write", "Write"
    SUGGEST = "suggest", "Suggest"


class ConversationKBUsage(TimeStampedMixin):
    """Tracks which knowledgebases are used in which conversations and how."""

    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="kb_usages",
    )
    knowledgebase = models.ForeignKey(
        "django_ergo.Knowledgebase",
        on_delete=models.CASCADE,
        related_name="conversation_usages",
    )
    mode = models.CharField(max_length=10, choices=KBUsageMode.choices)

    class Meta:
        unique_together = [["session", "knowledgebase", "mode"]]

    def __str__(self):
        return f"{self.session_id} -> {self.knowledgebase_id} ({self.mode})"
```

- [ ] **Step 4: Generate migration**

Run: `python -m django makemigrations django_ergo --settings=tests.example_app.settings`
Expected: Creates `0006_conversationkbusage.py`

- [ ] **Step 5: Apply migration**

Run: `python -m django migrate --settings=tests.example_app.settings`

- [ ] **Step 6: Add get_bound_knowledgebases() to Toolkit ABC**

In `src/django_ergo/conversation/toolkit.py`, add this method to the `Toolkit` class (after `render_overview`):

```python
    def get_bound_knowledgebases(self) -> list[tuple]:
        """Return [(knowledgebase, mode), ...] for usage tracking.

        Override in KB toolkits. Default returns empty list.
        """
        return []
```

- [ ] **Step 7: Override in KBToolkit**

In `src/django_ergo/kb_toolkit.py`, add this method to the `KBToolkit` class:

```python
    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(kb, "read") for kb in self.knowledgebases.values()]
```

- [ ] **Step 8: Override in KBWriteToolkit**

In `src/django_ergo/kb_write_toolkit.py`, add this method to the `KBWriteToolkit` class:

```python
    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(self.knowledgebase, "write")]
```

- [ ] **Step 9: Override in KBSuggestToolkit**

In `src/django_ergo/kb_suggest_toolkit.py`, add this method to the `KBSuggestToolkit` class:

```python
    def get_bound_knowledgebases(self) -> list[tuple]:
        return [(self.knowledgebase, "suggest")]
```

- [ ] **Step 10: Run tests**

Run: `python -m pytest tests/test_kb_usage_tracking.py -v`
Expected: All PASS

- [ ] **Step 11: Run ruff and commit**

Run: `ruff check --fix src/django_ergo/conversation/models.py src/django_ergo/conversation/toolkit.py src/django_ergo/kb_toolkit.py src/django_ergo/kb_write_toolkit.py src/django_ergo/kb_suggest_toolkit.py tests/test_kb_usage_tracking.py && ruff format src/django_ergo/conversation/models.py src/django_ergo/conversation/toolkit.py src/django_ergo/kb_toolkit.py src/django_ergo/kb_write_toolkit.py src/django_ergo/kb_suggest_toolkit.py tests/test_kb_usage_tracking.py`

```bash
git add src/django_ergo/conversation/models.py src/django_ergo/conversation/toolkit.py src/django_ergo/kb_toolkit.py src/django_ergo/kb_write_toolkit.py src/django_ergo/kb_suggest_toolkit.py src/django_ergo/migrations/ tests/test_kb_usage_tracking.py
git commit -m "feat: add ConversationKBUsage model and get_bound_knowledgebases() to toolkits"
```

---

### Task 2: Runner Records Usage

**Files:**
- Modify: `src/django_ergo/conversation/runner.py`
- Modify: `tests/test_conversation_runner.py`

- [ ] **Step 1: Write failing test for usage recording**

Add to `tests/test_conversation_runner.py`:

```python
class TestRunnerRecordsUsage:
    def test_record_kb_usage_function(self):
        """_record_kb_usage exists and is importable."""
        from django_ergo.conversation.runner import _record_kb_usage

        assert callable(_record_kb_usage)
```

- [ ] **Step 2: Add _record_kb_usage to runner**

In `src/django_ergo/conversation/runner.py`, add this async function after `_find_toolkit_for_tool`:

```python
async def _record_kb_usage(
    session: ConversationSession,
    toolkits: list[Toolkit],
) -> None:
    """Record KB usage for all toolkits bound to knowledgebases."""
    from django_ergo.conversation.models import ConversationKBUsage

    for toolkit in toolkits:
        for kb, mode in toolkit.get_bound_knowledgebases():
            await ConversationKBUsage.objects.aget_or_create(
                session=session, knowledgebase=kb, mode=mode,
            )
```

In `run_conversation_turn`, add the recording call after building `toolkits` and `additional_tool_schemas`, before the `async for response in engine.send(...)` line:

```python
    if toolkits:
        await _record_kb_usage(session, toolkits)
```

The full function becomes:

```python
async def run_conversation_turn(
    engine: Engine,
    session: ConversationSession,
    message: str,
    extra_tools: list[Toolkit] | None = None,
) -> AsyncIterator[EngineResponse | PendingApproval]:
    adapter = engine.get_tool_adapter()
    toolkits = extra_tools or []
    additional_tool_schemas = (
        _collect_toolkit_schemas(toolkits, adapter) if toolkits else None
    )

    if toolkits:
        await _record_kb_usage(session, toolkits)

    async for response in engine.send(
        session, message, additional_tools=additional_tool_schemas
    ):
        if response.event_type == "tool_use":
            name, args = adapter.parse_tool_call(response.tool_use)

            # Check toolkits first (e.g., KB toolkit, history toolkit)
            toolkit = _find_toolkit_for_tool(toolkits, name)
            if toolkit is not None:
                result = toolkit.execute_tool(name, args)
                async for continuation in engine.submit_tool_result(
                    session,
                    response.tool_use["id"],
                    result,
                    additional_tools=additional_tool_schemas,
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
                session,
                response.tool_use["id"],
                result,
                additional_tools=additional_tool_schemas,
            ):
                yield continuation
        else:
            yield response
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_conversation_runner.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/django_ergo/conversation/runner.py tests/test_conversation_runner.py
git commit -m "feat: runner auto-records KB usage from toolkit bindings"
```

---

### Task 3: Integration Test — Expert System Example

**Files:**
- Create: `tests/test_kb_expert_example.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_kb_expert_example.py
"""Integration test: static expert system example.

Exercises the full KB toolkit stack end-to-end:
- Create a KB with articles
- Use KBToolkit to search/browse
- Use KBWriteToolkit to add content
- Verify ConversationKBUsage tracking
"""
import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation.models import ConversationKBUsage
from django_ergo.conversation.models import ConversationSession
from django_ergo.kb_toolkit import KBToolkit
from django_ergo.kb_write_toolkit import KBWriteToolkit
from django_ergo.models import Article
from django_ergo.models import Knowledgebase

User = get_user_model()
pytestmark = pytest.mark.django_db

LAWN_CARE_ARTICLES = [
    ("0", "Mowing", "Mow your lawn regularly at a height of 3 inches. Never cut more than one-third of the grass blade at once."),
    ("1", "Watering", "Water deeply but infrequently. Aim for 1 inch of water per week, preferably in the early morning."),
    ("2", "Fertilizing", "Apply fertilizer in spring and fall. Use a slow-release nitrogen fertilizer for best results."),
    ("20", "Spring Fertilizing", "In spring, apply fertilizer when the grass starts actively growing, usually when soil temps reach 55F."),
    ("21", "Fall Fertilizing", "Fall fertilization is the most important application. Apply 4-6 weeks before the first expected frost."),
    ("3", "Weed Control", "Prevent weeds with a thick, healthy lawn. Apply pre-emergent herbicide in early spring before crabgrass germinates."),
]


@pytest.fixture()
def user():
    return User.objects.create_user(username="gardener", password="testpass")


@pytest.fixture()
def lawn_kb(user):
    kb = Knowledgebase.objects.create(
        name="Lawn Care Expert",
        description="Complete guide to lawn care and maintenance",
        owner_id=str(user.id),
    )
    for code, title, content in LAWN_CARE_ARTICLES:
        Article.objects.create(
            knowledgebase=kb, hierarchy_code=code, title=title, content=content,
        )
    return kb


@pytest.fixture()
def session(user):
    return ConversationSession.objects.create(
        user=user, engine_type="claude", transport_type="api", status="active",
    )


class TestExpertSystemSetup:
    def test_kb_has_articles(self, lawn_kb):
        assert lawn_kb.articles.count() == len(LAWN_CARE_ARTICLES)

    def test_hierarchy_structure(self, lawn_kb):
        top_level = lawn_kb.articles.filter(hierarchy_code__regex=r"^.$")
        assert top_level.count() == 4  # 0, 1, 2, 3
        children_of_2 = lawn_kb.articles.filter(hierarchy_code__startswith="2", hierarchy_code__regex=r"^..$")
        assert children_of_2.count() == 2  # 20, 21


class TestReadToolkit:
    def test_overview_shows_top_level(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        overview = toolkit.render_overview()
        assert "Lawn Care Expert" in overview
        assert "Mowing" in overview
        assert "Watering" in overview
        assert "Fertilizing" in overview
        assert "Weed Control" in overview
        # Nested articles should NOT be in overview
        assert "Spring Fertilizing" not in overview

    def test_table_of_contents(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool("kb_table_of_contents", {"kb_name": "Lawn Care Expert"})
        assert "0: Mowing" in result
        assert "1: Watering" in result
        assert "2: Fertilizing" in result
        # Nested articles should be indented
        assert "  20: Spring Fertilizing" in result
        assert "  21: Fall Fertilizing" in result
        assert "3: Weed Control" in result

    def test_get_article(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool(
            "kb_get_article",
            {"kb_name": "Lawn Care Expert", "hierarchy_code": "0"},
        )
        assert "Mowing" in result
        assert "3 inches" in result

    def test_search(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool(
            "kb_search",
            {"query": "when should I water my lawn", "kb_name": "Lawn Care Expert"},
        )
        # Should return results or graceful error (no OpenAI key in tests)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_list_kbs(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        result = toolkit.execute_tool("kb_list", {})
        assert "Lawn Care Expert" in result
        assert "6" in result  # 6 articles


class TestWriteToolkit:
    def test_add_article_to_expert(self, lawn_kb):
        toolkit = KBWriteToolkit(knowledgebase=lawn_kb)
        result = toolkit.execute_tool(
            "kb_create_article",
            {
                "title": "Aeration",
                "content": "Aerate your lawn in fall to reduce soil compaction and improve root growth.",
                "parent_code": None,
            },
        )
        assert "Aeration" in result
        # Should get next top-level code: "4"
        assert Article.objects.filter(knowledgebase=lawn_kb, hierarchy_code="4").exists()

    def test_add_sub_article(self, lawn_kb):
        toolkit = KBWriteToolkit(knowledgebase=lawn_kb)
        result = toolkit.execute_tool(
            "kb_create_article",
            {
                "title": "Winter Fertilizing",
                "content": "In cold climates, skip winter fertilization entirely.",
                "parent_code": "2",
            },
        )
        assert "22" in result
        assert Article.objects.filter(knowledgebase=lawn_kb, hierarchy_code="22").exists()


class TestUsageTracking:
    def test_read_toolkit_reports_bindings(self, lawn_kb):
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        bindings = toolkit.get_bound_knowledgebases()
        assert len(bindings) == 1
        assert bindings[0] == (lawn_kb, "read")

    def test_write_toolkit_reports_bindings(self, lawn_kb):
        toolkit = KBWriteToolkit(knowledgebase=lawn_kb)
        bindings = toolkit.get_bound_knowledgebases()
        assert bindings == [(lawn_kb, "write")]

    def test_usage_recorded_in_db(self, session, lawn_kb):
        from django_ergo.conversation.models import ConversationKBUsage

        # Simulate what the runner does
        toolkit = KBToolkit(knowledgebases=[lawn_kb])
        for kb, mode in toolkit.get_bound_knowledgebases():
            ConversationKBUsage.objects.get_or_create(
                session=session, knowledgebase=kb, mode=mode,
            )

        usages = ConversationKBUsage.objects.filter(session=session)
        assert usages.count() == 1
        assert usages.first().mode == "read"
        assert usages.first().knowledgebase == lawn_kb

    def test_multi_toolkit_usage(self, session, lawn_kb):
        from django_ergo.conversation.models import ConversationKBUsage

        read_toolkit = KBToolkit(knowledgebases=[lawn_kb])
        write_toolkit = KBWriteToolkit(knowledgebase=lawn_kb)

        for toolkit in [read_toolkit, write_toolkit]:
            for kb, mode in toolkit.get_bound_knowledgebases():
                ConversationKBUsage.objects.get_or_create(
                    session=session, knowledgebase=kb, mode=mode,
                )

        usages = ConversationKBUsage.objects.filter(session=session)
        assert usages.count() == 2
        modes = set(usages.values_list("mode", flat=True))
        assert modes == {"read", "write"}
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_kb_expert_example.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_kb_expert_example.py
git commit -m "test: add expert system integration test for KB toolkit stack"
```

---

### Task 4: Full Suite Verification

- [ ] **Step 1: Run all KB and conversation tests**

Run: `python -m pytest tests/test_kb_toolkit.py tests/test_kb_write_toolkit.py tests/test_kb_suggest_toolkit.py tests/test_kb_usage_tracking.py tests/test_kb_expert_example.py tests/test_toolkit_protocol.py tests/test_conversation_runner.py -v`
Expected: All PASS

- [ ] **Step 2: Run broader regression check**

Run: `python -m pytest tests/test_conversation_models.py tests/test_settings.py tests/test_tool_registry_unit.py -v`
Expected: All PASS

- [ ] **Step 3: Push**

```bash
git push
```
