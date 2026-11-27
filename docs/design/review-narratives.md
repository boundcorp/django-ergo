# Review narratives

Status: design, no runtime code yet. Starter definitions are in
`src/django_ergo/bots/review_library/`.

## 1. Problem

Lee wants to say any of these to any bot and get a useful review back:

- "do a planning review": a **different model of similar capability** reads
  the plan.
- "do a ponytail review", "security review", "UX review": a reviewer with a
  fixed personality and checklist.
- "do a swarmed security review": split the target into 5-10 slices, send a
  cheap model at each, then merge the results into one report.

Today each of these is a hand-written brief. They drift between bots, never
say which model reviewed, and a "second opinion" often runs on the same
model that wrote the work.

Not every Ergonaut installation has Orca. Some bots have only the generic
`ergo_agent_start` (`bots/agents.py`, backed by whatever `AgentManager` a
plugin provides: Orca or `plugins/local_shell`); some have no agent tool at
all; and agents outside Ergo (Claude Code, Codex) should be able to use the
same reviews. So the core must not start anything itself.

## 2. Goals and non-goals

Goals:

- A review **kind** is a data file. Adding a kind is adding a file; an
  installation or a bot can add or override kinds without a code change.
- One loader returns **text**: how to run the review, the brief for each
  reviewer, and how to merge the results. The calling bot runs it with
  whatever agent tool it has, or does the review itself.
- Kind and **strategy** (single, cross-model, swarm) are independent:
  "swarmed security review" is `security` + `swarm`.
- Model choice uses the installation's tiers when it has them and falls back
  to a plain-text constraint when it does not.
- Every finding says which model produced it, so "a different model" can be
  audited.

Non-goals (for now):

- A phrase parser. The model maps "ponytail review" to the call.
- Running reviewers from the library, or a new worker type. A programmatic
  fan-out helper is phase 3 and optional.
- Enforcing model choice at launch. We record it and audit it (section 9).
- Replacing Claude Code's `/code-review` or `/security-review`; the briefs
  are compatible with being pasted into either.

## 3. Where it lives

There is no merged "dev tooling library" yet. The closest pieces are:

- the agent skill library, `src/django_ergo/bots/skill_library/`
  (`ergo-developer`, `ergo-client`, ...), installable into Claude Code and
  Codex by `skill_library/install.py`;
- the proposed official `dev` plugin (`plugins/dev.py`) in the open design
  PR #398 (`docs/design/thread-tags-and-deciders.md`, section "Form of the
  dev pack"), which bundles `dev`, `orca`, `bash` and `kubectl`.

Proposal, following the import layers (core < `bots` < `plugins`):

| Piece | Path | Why there |
| --- | --- | --- |
| Definitions | `src/django_ergo/bots/review_library/*.md` | Sibling of `skill_library/`, same "library ships files, bot overrides by name" rule. |
| Loader | `src/django_ergo/bots/reviews.py` | Needs `bots.providers` and `bots.routing` for tiers; no plugin imports. |
| Tool + skill | `src/django_ergo/bots/skill_library/reviews/` (`SKILL.md`, `tools.py`) | A library skill any bot can include; `install: [claude, codex]` so external agents get it too. |
| CLI | `ergonaut reviews brief ...` and `python -m django_ergo.bots.reviews` | For agents with no Ergo tools at all. |

When the `dev` plugin lands it lists `reviews` in its default skills, so dev
bots get it without naming it. Nothing in `reviews` depends on `dev` or
`orca`.

## 4. Data format

A definition is Markdown with YAML front matter, like `SKILL.md`. The body
is the reviewer's personality and checklist, pasted into each reviewer's
brief as is.

```yaml
---
name: security                 # unique; file name without .md
aliases: [security review, sec review, appsec]
summary: One line, shown in the tool description.
targets: [diff, plan, app]     # what it can review; the first is the default
reviewer:
  policy: same_tier_any        # cross_model | same_tier_any | cheap
  tier: same                   # same (as author) | small | medium | large | xlarge
swarm:
  n: 8                         # default slice count, clamped to 2..12
  tier: small                  # tier for swarm reviewers
  slice_by: [file, risk]       # hints for decomposition, in preference order
findings: default              # findings schema name (section 8)
source:                        # optional provenance for adapted text
  url: ...
  license: MIT
---
Personality and checklist in plain Markdown.
```

Targets:

- `plan`: a plan or design doc (path, URL, or pasted text).
- `diff`: a branch, commit range or PR (`owner/repo#123`).
- `app`: a running app (URL plus the flows to try).

Lookup order, later wins by `name`:

1. `django_ergo/bots/review_library/` (built in).
2. Each directory in `DJANGO_ERGO["REVIEW_DIRS"]` (installation-wide, e.g.
   a shared directory in the bot config repo).
3. The bot's own folder, `reviews/*.md`.

A later file with the same `name` replaces the whole definition (no merging
of checklists; copy and edit). `enabled: false` in an override hides a
built-in kind. An alias that collides with another kind's name or alias is
a load error, so the tool description is never ambiguous.

## 5. Loader API

```python
from django_ergo.bots.reviews import review_brief, review_kinds

review_brief(
    kind: str,                # name or alias
    *,
    target: str = "",         # path, URL, PR ref or pasted text
    target_kind: str = "",    # plan | diff | app; default: definition's first
    strategy: str = "",       # single | cross_model | swarm; default from policy
    n: int | None = None,     # swarm size; default from definition
    author_model: str = "",   # e.g. "anthropic/claude-opus-5-5"
    bot: Bot | None = None,   # for overrides and the tier catalog
    focus: str = "",          # free text from the user ("only the auth code")
) -> ReviewBrief
```

`ReviewBrief` is a dataclass with a `text` property; tools return `text`.
Its parts are:

1. **How to run it.** The strategy, the number of reviewers, the model
   constraint (resolved or as text, section 6), and the exact steps:
   "start one agent with brief B on model M" or "split the target into N
   slices as follows, start one agent per slice with brief B + slice".
   Includes a fallback: "if you cannot start agents, do the review
   yourself and say so in the report".
2. **The reviewer brief**, ready to paste as an agent's `brief`: target,
   personality and checklist, the findings format, and the instruction to
   report its own model (section 9). For a swarm, it has a `{slice}` slot.
3. **Synthesis instructions** for the caller (section 7).

`review_kinds(bot=None)` lists kinds with aliases, summary and targets; the
tool description is built from it.

The tool, in the `reviews` skill:

```
ergo_review_brief(kind, target, target_kind?, swarmed?, n?, focus?)
```

`swarmed=true` selects the swarm strategy; otherwise the definition's
policy decides. `author_model` is filled from the calling chat's current
model (`ToolContext`), not by the model, so it cannot be guessed wrong. The
tool description reads, for example:

> Get instructions for a review. Kinds: planning (plan review, design
> review, second opinion), security (sec review, appsec), ux (usability
> review, ui review), ponytail (lazy review, yagni review). Say
> swarmed=true for "swarmed ... review".

Entry points:

| Caller | What it does with the brief |
| --- | --- |
| Bot with Orca or local shell | `ergo_agent_start(brief=..., model=... or tier=...)` per reviewer, waits for worker reports, then synthesizes. |
| Bot with another start-agent tool | Same steps with its own tool; the brief is plain text. |
| Bot with no agent tool | Does a single review itself, says that the strategy was downgraded and which model ran it. |
| Claude Code / Codex | The installed `reviews` skill runs the CLI and uses its own subagents. |
| Library code | Calls `review_brief()` directly. |

## 6. Model selection

The only non-text part. Inputs: the author model, the definition's
`reviewer.policy` and `tier`, and the bot's providers (`bots/providers.py`
tiers and `DEFAULT_TIER_MODELS`, `bots/routing.py` `tier_of`, `pick_model`
and `pick_agent`, filtered by the active pool in `bots/pools.py`).

1. **Author tier.** Look up `author_model` in the tier lists; the lowest
   tier that lists it is its tier. Unknown model: assume `large` and say so.
2. **Reviewer tier.** `same` means the author's tier; `cross_model` may go
   one tier up when the same tier has no other family, never down.
   `cheap` and swarm reviewers use the named tier (default `small`).
3. **Family.** The provider plus model line (`anthropic/claude-*`,
   `openai/gpt-*`, `google/gemini-*`, `xai/grok-*`). `cross_model` excludes
   the author's family; `same_tier_any` prefers another family and falls
   back to the same one.
4. **Candidates** come from `routing.agents[tier]` when the reviewer is a
   coding agent (`pick_agent`, subscription only) and from
   `routing.tiers[tier]` otherwise. Pool privacy and `switched_off()`
   providers are respected, so a private chat never gets a reviewer on a
   provider its pool forbids.
5. **Output.** Resolved: "use `openai/gpt-6.1-sol` (medium, not anthropic)".
   Unresolved (no providers, no candidate, or library used outside a bot):
   the constraint as text, "use a medium-tier model from a family other than
   anthropic/claude", plus the candidates the loader did find.

For swarms, the brief names a list of small models and asks the caller to
rotate through them, so one model's blind spot does not cover every slice.

## 7. Swarms: decomposition and synthesis

The loader does not split the target; it cannot read a PR or an app. It
tells the caller how:

1. Collect the target (diff stat and files, plan sections, or app flows).
2. Make N slices (default from the definition, 5-10, never more than the
   number of natural units). Prefer `slice_by` in order: by **file** (group
   small files; one big file can be its own slice), by **concern** (auth,
   data, API, UI), by **risk** (what can lose data or money first), by
   **flow** or **screen** for apps. Every slice says what it owns and lists
   the rest of the change in one line each for context.
3. Write the slice plan in the chat before starting agents, so the user can
   see and correct it.
4. Start one reviewer per slice with the brief and its slice.
5. Synthesize:
   - drop findings outside the reviewer's slice unless they are blockers;
   - merge duplicates (same location and same claim), keeping the best
     evidence and listing every reviewer that found it;
   - rank by severity, then by how many reviewers agree;
   - re-check every blocker and major against the code yourself before
     reporting it, and mark unconfirmed ones;
   - one report: verdict, findings table, then what was not reviewed
     (failed slices, skipped files, a downgraded strategy).

A failed or silent reviewer does not block the report; its slice is listed
as not reviewed.

## 8. Findings format

Each reviewer ends its report with a fenced JSON block, so synthesis does
not have to parse prose:

```json
{
  "review": "security",
  "slice": "auth views",
  "reviewer": {"model": "openai/gpt-6.1-sol", "agent": "codex", "self_reported": true},
  "verdict": "proceed_after_fixes",
  "findings": [
    {
      "severity": "major",
      "location": "src/app/views.py:120",
      "claim": "Session list does not filter by owner.",
      "evidence": "Query at line 120 has no owner filter; /sessions?bot=x returns all users' rows.",
      "fix": "Filter by request.user as list_sessions does.",
      "confidence": "high"
    }
  ],
  "not_checked": ["websocket consumers"]
}
```

Severity: `blocker`, `major`, `minor`, `nit`. Verdicts: `proceed`,
`proceed_after_fixes`, `rethink`. A definition can name another schema
later; `default` is the only one in phase 1. The synthesized report uses the
same shape with `reviewers: [...]` per finding.

## 9. Audit and enforcement

Text cannot enforce "use a different model". We make violations visible:

- **Self-report.** The brief tells each reviewer to fill `reviewer.model`
  with the model it runs on.
- **Launch record.** When the caller used `ergo_agent_start`, the worker's
  `state` already holds `agent`, `model`, `effort` and `tier`
  (`bots/agents.py`). The synthesized report quotes those, not the self-
  report, when it has them.
- **Check.** Phase 2 adds `ergo_review_record(kind, author_model, report)`:
  it stores the review as a `StructuredCall`-style row (kind, strategy,
  author model, reviewer models with their source, finding counts) and
  flags a `cross_model` review whose reviewer shares the author's family,
  or a model that does not match the worker's launch record.

## 10. Phased plan

1. **Loader and skill.** `bots/reviews.py` (parse, lookup order, alias
   check, tier resolution, `ReviewBrief`), `skill_library/reviews/` with
   `ergo_review_brief`, the CLI, package-data for `review_library/*.md`,
   tests, and `docs/reviews.md` plus a row in `docs/skills.md`.
2. **Audit.** `ergo_review_record`, the stored row, and a family-mismatch
   flag shown in the thread report.
3. **Optional fan-out.** `run_review(...)` for bots with an
   `AgentManager`: starts the reviewers, waits on their workers and runs
   synthesis as a structured call. Only if phase 1 shows bots doing the
   fan-out badly.
4. **More kinds** as files: performance, migration safety, test coverage,
   docs.

## 11. Open questions for Lee

1. **Home.** `bots/review_library/` plus a `reviews` library skill, or wait
   for the `dev` plugin from #398 and put everything there?
2. **Planning review tier.** Same tier only, or allow one tier up when the
   other family has nothing at the author's tier (current proposal: allow
   up, never down)?
3. **Cross-family on a single-family installation.** Fall back to a
   different model of the same family with a warning, or refuse?
4. **Who decomposes a swarm.** The calling bot (proposed), or a cheap
   planner agent whose slice plan the bot only checks?
5. **Ponytail scope.** The source is an authoring skill; this definition
   adapts it to review only. Should a "ponytail review" also allow the
   reviewer to push back on the request itself (the upstream "ultra"
   level)?
6. **Override granularity.** Whole-file replace (proposed) or allow a bot to
   append to a built-in checklist?

## Attribution

`review_library/ponytail.md` is adapted, in paraphrase, from
[DietrichGebert/ponytail `skills/ponytail/SKILL.md`](https://github.com/DietrichGebert/ponytail/blob/df9e89941d50419fd5bcf1de41e28097f1db49b2/skills/ponytail/SKILL.md),
MIT License, Copyright (c) 2026 DietrichGebert. Its front matter records
the source.
