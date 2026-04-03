# KB Usage Tracking and Expert Example Design

**Date:** 2026-04-03
**Status:** Draft
**Author:** Lee / Claude

## Overview

Add a `ConversationKBUsage` model to track which conversations use which KBs and in what mode (read/write/suggest). Add a `get_bound_knowledgebases()` method to the Toolkit ABC. Have the runner record usage automatically. Validate the full stack with an integration test (static expert system example).

## Problem

There's no way to know which conversations touched which KBs. This information is needed for future features: absorption pipelines (which conversations should feed into a KB?), self-organization (which KBs are heavily used and how?), and retrospective analysis.

## Goals

1. **ConversationKBUsage model** — M2M through-model linking sessions to KBs with mode
2. **Toolkit.get_bound_knowledgebases()** — generic method for usage tracking, no isinstance checks
3. **Runner auto-records usage** — when extra_tools are provided, runner records KB bindings
4. **Integration test** — exercises full read toolkit + usage tracking pipeline

## Non-Goals

- Per-tool-call tracking (just session-level binding)
- Usage analytics or reporting
- Runnable example scripts (just tests for now)

---

## Component 1: ConversationKBUsage Model

New model in `src/django_ergo/conversation/models.py`:

```python
class KBUsageMode(models.TextChoices):
    READ = "read", "Read"
    WRITE = "write", "Write"
    SUGGEST = "suggest", "Suggest"

class ConversationKBUsage(TimeStampedMixin):
    session = FK(ConversationSession, related_name="kb_usages")
    knowledgebase = FK(Knowledgebase, related_name="conversation_usages")
    mode = CharField(max_length=10, choices=KBUsageMode.choices)

    class Meta:
        unique_together = [["session", "knowledgebase", "mode"]]
```

Requires a new migration.

---

## Component 2: Toolkit.get_bound_knowledgebases()

Add a non-abstract method to the `Toolkit` ABC in `src/django_ergo/conversation/toolkit.py`:

```python
def get_bound_knowledgebases(self) -> list[tuple]:
    """Return [(knowledgebase, mode), ...] for usage tracking. Default: empty."""
    return []
```

Override in KB toolkits:
- `KBToolkit.get_bound_knowledgebases()` → `[(kb, "read") for kb in self.knowledgebases.values()]`
- `KBWriteToolkit.get_bound_knowledgebases()` → `[(self.knowledgebase, "write")]`
- `KBSuggestToolkit.get_bound_knowledgebases()` → `[(self.knowledgebase, "suggest")]`

`ChatWithHistoryToolkit` inherits the default (empty list).

---

## Component 3: Runner Records Usage

In `run_conversation_turn()`, after building the toolkit list and before the first `engine.send()` call, record usage:

```python
for toolkit in toolkits:
    for kb, mode in toolkit.get_bound_knowledgebases():
        await ConversationKBUsage.objects.aget_or_create(
            session=session, knowledgebase=kb, mode=mode,
        )
```

Uses `aget_or_create` (async) since the runner is async. Idempotent — repeated turns don't create duplicates.

---

## Component 4: Integration Test (Expert System Example)

A test in `tests/test_kb_expert_example.py` that exercises:

1. Create a KB with several articles
2. Create a `ConversationSession`
3. Create `KBToolkit` bound to the KB
4. Verify `render_overview()` includes KB info and TOC
5. Execute `kb_search`, `kb_get_article`, `kb_table_of_contents` directly
6. Create `KBWriteToolkit`, execute `kb_create_article`
7. Verify `ConversationKBUsage` records are created with correct modes when toolkits report their bindings

No real LLM calls. Tests the toolkit plumbing and usage tracking.

---

## File Organization

```
src/django_ergo/conversation/
├── models.py               # MODIFY: add ConversationKBUsage + KBUsageMode
├── toolkit.py              # MODIFY: add get_bound_knowledgebases()
├── runner.py               # MODIFY: record usage before send()

src/django_ergo/
├── kb_toolkit.py           # MODIFY: override get_bound_knowledgebases()
├── kb_write_toolkit.py     # MODIFY: override get_bound_knowledgebases()
├── kb_suggest_toolkit.py   # MODIFY: override get_bound_knowledgebases()

src/django_ergo/migrations/
├── NNNN_conversationkbusage.py  # NEW: migration for usage model

tests/
├── test_kb_expert_example.py    # NEW: integration test
```

---

## Relationship to Existing Code

- `ConversationSession` in `conversation/models.py` → gains `kb_usages` reverse relation
- `Knowledgebase` in `models.py` → gains `conversation_usages` reverse relation
- `Toolkit` ABC → gains non-abstract `get_bound_knowledgebases()` method
- `run_conversation_turn` → records usage before first send(), uses async `aget_or_create`
- All three KB toolkits → override `get_bound_knowledgebases()` with one-liner
- `ChatWithHistoryToolkit` → unchanged (inherits empty default)
