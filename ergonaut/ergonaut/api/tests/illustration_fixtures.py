"""A representative thread and the illustration a good model returns for it.

Shared by the structural tests; ``docs/screenshots/illustrate`` was rendered from it.
"""

from __future__ import annotations

from ergonaut.api.illustration_spec import IllustrationSpec

EVIDENCE = [
    {
        "line": 2556,
        "role": "user",
        "text": (
            "Add an Illustrate button above the composer. It opens a modal of suggested topics with labeled "
            "checkboxes: an explanation, current status, a PR or a plan. I want it to look designed, not like a memo."
        ),
    },
    {
        "line": 2559,
        "role": "assistant",
        "text": (
            "Proposed flow: the thread and its authorized files feed a suggestion call, the user picks up to three "
            "topics, a generation job builds a static HTML attachment, and the attachment keeps its citations."
        ),
    },
    {
        "line": 2562,
        "role": "assistant",
        "text": (
            "Done so far: suggestion call, signed snapshots, durable Celery job and the chat dialog are merged. "
            "Still open: richer templates and a decision on whether charts may read attachments."
        ),
    },
    {
        "line": 2565,
        "role": "user",
        "text": "Charts only if there is real data. Phase 1 took 3 days, phase 2 is estimated at 5 days and phase 3 at 8 days.",
    },
    {
        "line": 2568,
        "role": "assistant",
        "text": (
            "Blocked: the free-form page lane until the sandbox review lands. Next: components for timeline, status "
            "board, flow and diagram, then screenshot tests on a phone viewport."
        ),
    },
    {"line": 2571, "role": "user", "text": "Compare a template-only approach with letting the model write raw HTML."},
]

TOPICS = [
    {
        "id": "timeline",
        "title": "How the feature got here",
        "purpose": "Decisions and milestones in order.",
        "kind": "timeline",
        "evidence_refs": [2556, 2562],
    },
    {
        "id": "status",
        "title": "Where things stand",
        "purpose": "Done, blocked and open items.",
        "kind": "status",
        "evidence_refs": [2562, 2568],
    },
    {
        "id": "plan",
        "title": "How it works and what comes next",
        "purpose": "The generation flow and the rollout phases.",
        "kind": "plan",
        "evidence_refs": [2559, 2565, 2568],
    },
]

AS_OF_LINE = 2571


def spec_dict() -> dict:
    return {
        "title": "Illustrate: from one button to a sourced visual page",
        "kicker": "Plan · status · timeline",
        "summary": (
            "The suggestion call, signed snapshots and the generation job are merged; the visual templates and the "
            "free-form page lane are not. Charts stay off until a message supplies real numbers."
        ),
        "takeaways": [
            "Backend path is merged; presentation is the open work.",
            "Templates, not raw model HTML, keep pages safe and consistent.",
            "The page lane is blocked on the sandbox review.",
            "Only phase durations are quoted in the thread, so only those are charted.",
        ],
        "blocks": [
            {
                "type": "timeline",
                "topic": "timeline",
                "heading": "From request to merged backend",
                "intro": "Read top to bottom; each marker says whether the step is finished.",
                "evidence_refs": [2556, 2562],
                "items": [
                    {
                        "when": "Request",
                        "title": "Illustrate button and topic modal asked for",
                        "detail": "Labeled checkboxes for explanation, status, PR or plan.",
                        "state": "done",
                        "evidence_refs": [2556],
                    },
                    {
                        "when": "Proposal",
                        "title": "Suggest, pick, generate, attach",
                        "detail": "Up to three topics per request; the attachment keeps its citations.",
                        "state": "done",
                        "evidence_refs": [2559],
                    },
                    {
                        "when": "Merged",
                        "title": "Suggestion call, signed snapshots, Celery job, chat dialog",
                        "state": "done",
                        "evidence_refs": [2562],
                    },
                    {
                        "when": "Now",
                        "title": "Richer templates",
                        "detail": "Timeline, status board, flow and diagram components.",
                        "state": "in_progress",
                        "evidence_refs": [2568],
                    },
                ],
            },
            {
                "type": "prose",
                "topic": "timeline",
                "heading": "Why typed components",
                "evidence_refs": [2556],
                "body": "The ask was a page that looks designed, not a memo.\n\nTyped components give that look without letting the model write markup.",
            },
            {
                "type": "status_board",
                "topic": "status",
                "heading": "Status by item",
                "intro": "Counts cover only the items listed here.",
                "evidence_refs": [2562, 2568],
                "items": [
                    {"title": "Suggestion call", "detail": "Merged.", "state": "done", "evidence_refs": [2562]},
                    {"title": "Signed snapshots", "state": "done", "evidence_refs": [2562]},
                    {"title": "Durable Celery job", "state": "done", "evidence_refs": [2562]},
                    {
                        "title": "Visual components",
                        "detail": "Timeline, board, flow, diagram.",
                        "state": "in_progress",
                        "evidence_refs": [2568],
                    },
                    {
                        "title": "Free-form page lane",
                        "detail": "Waits for the sandbox review.",
                        "state": "blocked",
                        "evidence_refs": [2568],
                    },
                    {
                        "title": "Charts may read attachments?",
                        "state": "decision",
                        "evidence_refs": [2562],
                    },
                    {"title": "Phone screenshot tests", "state": "next", "evidence_refs": [2568]},
                ],
            },
            {
                "type": "stats",
                "topic": "status",
                "heading": "Numbers the thread states",
                "evidence_refs": [2565],
                "stats": [
                    {"label": "Phase 1 (finished)", "value": "3 days", "evidence_ref": 2565},
                    {
                        "label": "Phase 3 estimate",
                        "value": "8 days",
                        "note": "An estimate, not a measurement.",
                        "evidence_ref": 2565,
                    },
                    {"label": "Phase 2 p95 latency", "value": "420 ms", "evidence_ref": 2565},
                ],
            },
            {
                "type": "chart",
                "topic": "status",
                "heading": "Phase length in days",
                "intro": "Phase 1 is actual; phases 2 and 3 are estimates.",
                "chart": "bar",
                "unit": "days",
                "points": [
                    {"label": "Phase 1", "value": 3, "evidence_ref": 2565},
                    {"label": "Phase 2 (est.)", "value": 5, "evidence_ref": 2565},
                    {"label": "Phase 3 (est.)", "value": 8, "evidence_ref": 2565},
                ],
            },
            {
                "type": "chart",
                "topic": "status",
                "heading": "Generation latency over time",
                "chart": "line",
                "unit": "s",
                "points": [
                    {"label": "Mon", "value": 41, "evidence_ref": 2562},
                    {"label": "Tue", "value": 38, "evidence_ref": 2562},
                ],
            },
            {
                "type": "flow",
                "topic": "plan",
                "heading": "From click to attachment",
                "evidence_refs": [2559],
                "steps": [
                    {"title": "Suggest", "detail": "A typed list of topics from the thread.", "evidence_refs": [2559]},
                    {"title": "Pick", "detail": "Up to three, plus a free-form request."},
                    {"title": "Generate", "detail": "A durable job fills typed components.", "state": "done"},
                    {"title": "Attach", "detail": "Rendered, sandboxed and cited.", "state": "done"},
                ],
            },
            {
                "type": "diagram",
                "topic": "plan",
                "heading": "Who talks to whom",
                "intro": "Boxes are parts; arrows show what feeds what.",
                "evidence_refs": [2559],
                "nodes": [
                    {
                        "id": "thread",
                        "label": "Chat thread + files",
                        "kind": "data",
                        "detail": "Authorized, bounded snapshot.",
                    },
                    {"id": "suggest", "label": "Suggestion call", "kind": "system"},
                    {"id": "user", "label": "You pick topics", "kind": "actor"},
                    {
                        "id": "job",
                        "label": "Generation job",
                        "kind": "system",
                        "detail": "Celery, retried twice. Templates draw the page.",
                    },
                    {"id": "page", "label": "Sandboxed HTML attachment", "kind": "artifact"},
                ],
                "edges": [
                    {"source": "thread", "target": "suggest", "label": "reads"},
                    {"source": "suggest", "target": "user", "label": "proposes"},
                    {"source": "user", "target": "job", "label": "selects"},
                    {"source": "thread", "target": "job", "label": "cites"},
                    {"source": "job", "target": "page", "label": "typed spec"},
                    {"source": "page", "target": "user", "label": "feedback"},
                ],
            },
            {
                "type": "comparison",
                "topic": "plan",
                "heading": "Templates or raw model HTML",
                "evidence_refs": [2571],
                "columns": ["Typed components", "Model-written HTML"],
                "rows": [
                    {"label": "Safety", "cells": ["Escaped by construction", "Needs a sanitizer"]},
                    {"label": "Consistency", "cells": ["Same design every time", "Varies per run"]},
                    {"label": "Range", "cells": ["Limited to the component set", "Anything"]},
                ],
            },
            {
                "type": "cards",
                "topic": "plan",
                "heading": "Rollout phases",
                "evidence_refs": [2565],
                "cards": [
                    {"title": "Phase 1: backend path", "body": "Suggest, snapshot, job, attachment.", "state": "done"},
                    {
                        "title": "Phase 2: components",
                        "body": "Typed visuals and phone-width checks.",
                        "state": "in_progress",
                    },
                    {
                        "title": "Phase 3: page lane",
                        "body": "Free-form pages after the sandbox review.",
                        "state": "blocked",
                    },
                ],
            },
        ],
        "caveats": ["The thread names no latency measurements, so none are charted."],
    }


def build_spec() -> IllustrationSpec:
    return IllustrationSpec.model_validate(spec_dict())
