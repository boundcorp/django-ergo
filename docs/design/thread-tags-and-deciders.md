# Thread tags, Deciders and Projects

Status: design draft, 2026-11-25. Nothing here is built yet.

This doc covers three related requests:

1. Ergonaut threads get tags from a vocabulary the bot configures, and every
   thread list can filter on them.
2. Decide whether "Projects" is an Ergo core concept or only a devbox plugin,
   and whether it should move into core as a dev extension pack.
3. Add Deciders: a new kind of small LLM integration that picks labels from a
   fixed candidate list (classify or rerank). It runs on the models already
   hosted on Rigel for Hindsight, with Hindsight Cloud as a backup. Agents also
   get a tool to update a thread's metadata when its direction changes.

Paths are relative to the repo root unless marked otherwise. `ergo-bots/` means
`boundcorp/ergo-bots` and `infra-octo/` means `boundcorp/infra-octo` (main).
Line numbers are from `main` at b52a47b.

---

## 1. Current state

### 1.1 Threads (`ConversationSession`)

- **Model:** `src/django_ergo/conversation/models.py:59-186`. Indexes are
  `(user, -created_at)`, `status` and `engine_type`. The database is Postgres
  only (`ergonaut/ergonaut/settings/project.py:107`). JSONB `||` merges are
  already in use.
- **Status:** `SessionStatus` is active, paused, completed or failed
  (`models.py:26-31`, field at `:88`). Both "resolved" and "archived" are
  stored as `completed`. The archive and reopen bookkeeping lives in
  `metadata`, in `bots/archival.py:51-97`.
- **Title:**
  - `user_title` (`models.py:98`). NULL means the title is automatic. It is a
    column, not a metadata key, so a stale writer cannot undo a rename;
    `save()` protects it with `F()` (`:154-178`).
  - The automatic title is `metadata["title"]`.
  - The display rule is in `conversation/roles.py:33-40`.
- **Role:** `metadata["bot_role"]` ∈ {main, chat, thread} (`roles.py:11-21`).
- **Summary:** there is no general summary field.
  - `metadata["resolved_summary"]` is set by `ergo_thread_resolve`
    (`bots/orchestrator.py:1132-1135`, capped at 2000 characters).
  - The portfolio snapshot shows the latest `ConversationCompaction.summary`
    (`models.py:447`; `bots/snapshot.py:169-205`).
- **Priority, tags, labels, project:** none exist anywhere on a thread.
- **`metadata` keys written today:** `bot_role`, `title`, `chat`,
  `started_by*`, `archived_*`, `resolved_*`, `pinned`, `todos`, `skills`,
  `bot_compaction`, `effort`, `routed_model`, `retry_route`, `turn_budget`,
  `orca`, `telegram*`, `voice_session_id`, `rendered_cache` and
  `project_name` (the CLI import only).
- **Metadata write hazard:** most writers read metadata and then call
  `.update(metadata=...)` with the whole dict (`bots/todos.py:94-103`,
  `runtime.py:1211-1219`).
  - Only `name_thread` does an atomic JSONB merge
    (`ergonaut/ergonaut/apps/bots/tasks.py:1132-1137`).
  - `pin_session` merges into a possibly stale copy
    (`ergonaut/ergonaut/api/bots.py:1935-1947`).
  - **Tags must not live in `metadata`.** A read-modify-write anywhere else
    would silently drop them.

### 1.2 Thread listing and filtering

| Surface | Where | Filters today |
|---|---|---|
| `GET /sessions` (sidebar, Threads page, Sessions page) | `ergonaut/ergonaut/api/bots.py:1120-1135`; `SessionOut` `:123-156`; `session_out()` `:651-675`; `bucket()` `:689-697` | `bot`, `q` (message text or title), `status` (exact). Ordered by `-updated_at`, capped at 200 |
| `GET /bots/session-search` | `api/bots.py:1053-1117` | `q` only |
| Thread-changing endpoints | `/title` `:1887-1906`, `/pin` `:1931-1947`, `/permanent`, `/close`, `/model`, `/effort` | n/a |
| `ergo_thread_list` | `bots/orchestrator.py:590`; row shape `_row()` `:433-465` | `bot`, `query` (every word must match the title, chat name or id prefix), `include_archived`, `limit` ≤ 50 |
| `ergo_portfolio_status` | `bots/orchestrator.py:518` calls `bots/snapshot.py:portfolio_status`; thread rows `_thread_row` `:271-308` | `bot`, `lookback_hours`, `limit`/`offset`, `resolved_limit`, `sources` |
| "Bots and threads" context block | `bots/overview.py:114-120, 205-256` | none (one line per chat) |

Frontend (`ergonaut/frontend/src`):

- `App.tsx:79` loads `api.sessions()` once with no filters, so the sidebar and
  Threads page filter on the client.
- `components/Sidebar.tsx:167-240` loops over bots and renders
  `ThreadGroups`.
- `components/ThreadList.tsx`:
  - groups by bucket (pinned, review, waiting, working, idle, resolved,
    `:12-28`);
  - `SlimRow` is the sidebar row (`:219-246`);
  - `FullRow` is the Threads page row, with PR and worker chips around
    `:318`.
- `pages/ThreadsPage.tsx:104-117` has the bot filter chip row
  (`.dashboard-filters`, `?bot=`).
- `pages/Sessions.tsx:8-60` has the text search, bot select and status
  control (server-side filters).
- `pages/Chat.tsx:775,815` holds the `SessionTitle` editor. A tag editor
  would sit next to it.
- `components/Threads.tsx:17` has `PrChip`, which a tag chip can copy.

### 1.3 Titles and summaries

- New threads from the UI get a provisional title (first 6 words) and then
  `queue_thread_naming` (`api/bots.py:1257-1274`).
- That runs the Celery task `name_thread` (`apps/bots/tasks.py:1109-1148`),
  which calls `Bot.thread_metadata()` (`src/django_ergo/bots/runtime.py:857`).
- `thread_metadata()` is a structured call, kind `new_thread_metadata`. Its
  pydantic `ThreadMetadata` has a single `title` field. It runs on
  `make_ancillary_engine(..., use_session=False)`, which is the bot's model,
  not a cheap tier.
- Agent-created threads take `title` from `ergo_thread_send` and are never
  auto-named.
- **No agent tool can change a thread's title or metadata after creation.**

### 1.4 bot.yaml

- **Format:** plain dataclasses with hand-written validation
  (`src/django_ergo/bots/definition.py`).
  - The docstring lists every key (`:8-86`).
  - `BotDefinition` is at `:219-270`; `from_dict` at `:272-362`.
  - Unknown top-level keys are ignored silently.
- **Strict per-block parsers to copy:**
  - `_self_health` (`:189`) rejects unknown keys and returns a frozen
    dataclass.
  - `_schedules` (`:445-468`) handles named entries and checks for
    duplicates.
- **Precedent for library defaults that a bot extends:**
  - `DJANGO_ERGO["DEFAULT_SKILLS"]` (`settings.py:70`), plus
    `skills: {include, exclude, defaults: false}` in bot.yaml.
  - The merge happens in `Bot._default_skills` (`runtime.py:257-266`).
- **Deployment-wide YAML precedent:** `providers.yaml` sits at the top of the
  bot path. It is parsed by `bots/providers.py:597-617`, loaded once per
  registry (`ergonaut/ergonaut/apps/bots/loading.py:131`), and stored on
  `BotRegistry.providers` (`bots/registry.py:109-110`).
- **Settings merging:** `DJANGO_ERGO` settings replace whole keys; nested
  values are not merged (`settings.py:240-260`).

### 1.5 The "Assistant" abstraction is now `StructuredCall`

Cabal's Assistant became Ergo's structured call. `docs/structured-calls.md:129-144`
maps `AssistantSpec` → `StructuredCallSpec`, `Assistant` →
`StructuredCall` and `run_assistant` → `run_structured_call`.

- **Spec:** `StructuredCallSpec` (`conversation/structured.py:247-286`) has
  `kind`, `system_prompt`, `response_model` (offered to the model as the
  `submit_output` tool), `max_turns`, `max_tokens` and `budget`.
- **Run:** `run_structured_call(...)` (`structured.py:1458-1550`) writes a
  `StructuredCall` row (`conversation/models.py:348+`) holding the request,
  response, transcript, tokens and `cost_usd`.
- **Cost:** `add_request_cost` (`structured.py:587`, `pricing.py:270-300`)
  prices each request from `pricing.py` plus `DJANGO_ERGO["MODEL_PRICES"]`.
  CLI-transport calls are skipped, and an unknown price is stored as NULL
  (unpriced).
- **Engine choice:**
  - providers.yaml tiers (`auto/small` … `auto/xlarge`) resolved through
    `routing.pick_model` (`routing.py:426+`), and checked by
    `model_choice.check` (`model_choice.py:133-195`).
  - `Bot.make_ancillary_engine(session, purpose=...)` (`runtime.py:906-927`)
    applies the private-pool policy.
- **Tests:** fake clients in `tests/test_conversation_structured.py:57-126`.
  `make_bot()` is in `tests/test_bots.py:290-302`, and the titling test is at
  `:2398-2410`.
- **Existing small-decision precedents:**
  - The **bash reviewer** (`plugins/bash_review.py:599-687`) calls
    `engine.complete()` directly on `auto/small` and returns strict JSON
    `{allow|escalate}`. It records no StructuredCall row and no cost. That is
    a gap the Decider should not repeat.
  - Routing-rule compilation (`routing.py:640-680`).
  - Compaction (`compaction.py:313-368`).
  - Attachment look (`plugins/attachments.py:374-392`).
- **Rerankers:** Ergo makes no reranker or embedding call for labels today.
  Hindsight's reranker runs server-side (`plugins/hindsight_memory.py:397`).

### 1.6 Hindsight inference (Rigel + Octo + Hindsight Cloud)

| Role | Model | Rigel (GPU, primary) | Octo `llm` ns (CPU backup) | Protocol |
|---|---|---|---|---|
| Cross-encoder rerank | `hindsight-ai/hs-rerank-1` (114M) | TEI 1.9.3 fp16, Caddy `10.20.30.109:8802` (key on every route) | `hs-rerank` svc :80 (TEI cpu-1.9.3) plus a `rerank-cap` sidecar :8090 that scores the top 40 only | TEI `POST /rerank`, `GET /info`, `/health` |
| Decide (small LLM) | `hs-decide-1-flash` (alias `hs-decide`, 32k context) | llama-swap behind Caddy `10.20.30.109:8800` | `hs-decide` svc :8000 (`ghcr.io/boundcorp/hs-decide-serve:1.0.0`) | `POST /v1/systemone`; `GET /v1/models` is OpenAI-style |
| Embed | `hindsight-ai/hs-embed-1` (384-dim) | none yet (infra-octo PR #309 is still a WIP) | `hs-embed` svc :80 | TEI `POST /embed` |
| General instruct | `qwen3-4b` (Qwen3-4B-Instruct-2507 Q4_K_M) | llama-swap on demand, ttl 300s. **Loading it evicts hs-decide** (exclusive swap group) | none | OpenAI `/v1/chat/completions` on :8800 |

Live checks on 2026-11-25, all read-only:

- Rigel `/v1/models` lists `hs-decide` (loaded) and `qwen3-4b` (unloaded).
- TEI `/info` on Rigel reports `hs-rerank-1`, fp16, max batch 512.
- GPU memory in use is 4.5/8 GiB.
- Gateway backends `be_decide_rigel` and `be_rerank_*` are UP.
  `be_rerank_rigel` recently flapped on an L4 timeout.

How things are routed (`infra-octo/kubernetes/apps/llm/gateway/haproxy.cfg`):

- **`llm-gateway` :8080** is internal and needs `GATEWAY_API_KEY`. Its routes
  are `/decide`, `/embed`, `/rerank` and `/hindsight` (`:110-128`).
  - `/decide` fails over Rigel → **Hindsight Cloud**
    (`inference.hindsight.vectorize.io`, `/v1/systemone`, 60s timeout) → Octo
    CPU. A 402 from Cloud sends traffic to Octo CPU for 15 minutes
    (`:169-212`).
  - **`/rerank` on :8080 is Octo CPU only** (`:222-230`).
- **:8082** is Hindsight's own unkeyed rerank route. It tries Rigel first with
  a 1s timeout, then Octo top-40, and answers 424 so that Hindsight's Cohere-
  compatible paid reranker (`inference.hindsight.vectorize.io/v1/rerank`,
  configured in `hindsight-start/start.sh:21-31`) takes over.
- **:8888** is the Hindsight API that Ergonaut uses. Ergo's
  `HINDSIGHT_URL` points at the gateway (the doc at
  `docs/hindsight-memory.md:22` is out of date).
- **NetworkPolicy:** the `llm` namespace admits only the `ergonaut` namespace,
  and only on :8080 and :8888 (`networkpolicy.yaml:78-93`). Devbox cannot
  reach the gateway.
- **Ergo has no failover logic of its own.** All failover lives in the
  gateway.
- **Secret and env names** (no values recorded here):
  - `ergonaut-runtime` (`HINDSIGHT_URL`, `HINDSIGHT_API_KEY`)
  - `llm-gateway` (`GATEWAY_API_KEY`, `RIGEL_DECIDE_API_KEY`)
  - `hindsight-inference` (`HINDSIGHT_INFERENCE_API_KEY`)
- **Rigel's own stack has no repo.** It lives in `~/llm` on rigel, run by
  `rigel-llm-*` user systemd units. A gamewatch unit stops the whole target
  while a game is running, so **Rigel is often unavailable by design**.

### 1.7 Projects today

**There is no Project model, table or registry for dev work anywhere.** "Project"
currently means one of four separate things:

1. **A repo identity, computed when needed (core library).**
   - `plugins/agent_usage_scan.py:255-303` `project(cwd)` returns
     `key = "remote:" + sha256(canonical_remote)[:24]`, the repo name, the
     remote, a confidence and so on. `canonical_remote()` is at `:211-225`.
   - Orca workers store the result in `worker.state["project"]`
     (`plugins/orca.py:1351-1365`, backfilled by `resolve_project` at
     `:1313-1329`).
   - Native sessions record `AgentWorkspaceObservation.project_key`
     (`conversation/models.py:1024-1058`; `conversation/agent_history.py:27-46`,
     whose docstring says it works "without inventing a shared project row").
2. **Cost grouping by repo (Ergonaut).**
   - `ergonaut/ergonaut/api/costs.py:495-590` builds `by_project` from
     `worker.state["project"]`, rendered by `frontend/src/pages/Costs.tsx`.
   - The management command `agent_projects` backfills it.
   - `CostOperation.project_ref` (`costs/models.py:76`) is free text that
     **nothing fills in** outside tests.
3. **The devbox bot's "Project analytics" page.**
   - `ergo-bots/devbox/tools/projects.py:17-126` is a read-only
     `@page_action` that groups the viewer's devbox threads, workers and
     usage by canonical remote.
   - The page is `devbox/pages/projects.jhtml`, pinned in
     `devbox/bot.yaml:14` (added in #137).
   - It is used only by the devbox bot.
4. **Domain projects (not repos).**
   - Storytime has `Project(BotTable)` with a unique `slug`
     (`ergo-bots/storytime/tables.py:31-45`). Spend is attributed by slug
     (`--project SLUG`, and the video-job tag `project:<slug>`).
   - The engine repo has its own `apps/projects/models.py:449`.
   - math billing also has an unrelated `projects.Project`.
   - The root `boundcorp` bot (`ergo-bots/bot.yaml:16-95`) defines three
     "project chats" (storytime, holyword, meditate) whose repos are written
     in prose only. Its `agents.md:5-13` says pure single-repo dev work gets
     no tracked project.

The closest thing to a canonical project list is the "Project cards" table
in `~/ENVIRONMENT.md`, which records each repo's tier
(revenue/client/sandbox/infra). There is also the standalone
`~/p/leeward/project-tracker` SQLite scanner, which sits outside Ergo.

---

## 2. Tag data model

### 2.1 Options considered

| Option | Filter speed | Audit and provenance | Concurrency | Verdict |
|---|---|---|---|---|
| A. `metadata["tags"]` list | JSONB `@>` with a GIN index on all of metadata | none | **unsafe**: whole-dict writers drop it (§1.1) | no |
| B. `ArrayField` column `tags` on the session | GIN `&&` / `@>`, fast | none | safe with atomic `array_append`/`array_remove` | good read path, but no provenance |
| C. M2M to a global `Tag` table | joins | per-link rows | safe | vocabulary in the DB clashes with vocabulary in config |
| **D. `ThreadTag` rows (truth) + `tags` array (index)** | GIN on the array | full | one writer function updates both inside a transaction | **recommended** |

The vocabulary lives in config (§3), so a global Tag table would only copy
it. Option D keeps provenance and history in rows, and keeps filtering a
single indexed array lookup that is easy to put in `SessionOut`.

### 2.2 Schema sketch

```python
# src/django_ergo/conversation/models.py
class ConversationSession(...):
    # Active tags, denormalized from ThreadTag. Written only by tags.set_tags().
    tags = ArrayField(models.CharField(max_length=80), default=list, blank=True)
    class Meta:
        indexes = [..., GinIndex(fields=["tags"], name="session_tags_gin")]

class ThreadTag(models.Model):
    """One tag on a thread, live or removed. Removed rows are the history."""
    session = models.ForeignKey(ConversationSession, related_name="tag_rows", on_delete=models.CASCADE)
    family = models.CharField(max_length=40, blank=True)   # "" for bare tags
    value = models.CharField(max_length=80)                 # "storytime", "high", "fix"
    source = models.CharField(max_length=10, choices=[("user",)*2, ("agent",)*2, ("decider",)*2, ("derived",)*2])
    confidence = models.FloatField(null=True)               # decider score; NULL for user/agent
    actor = models.CharField(max_length=120, blank=True)    # username, bot name, decider name or rule
    reason = models.CharField(max_length=300, blank=True)   # agent's stated reason / decider evidence
    call = models.ForeignKey("StructuredCall", null=True, on_delete=models.SET_NULL)  # the decider run
    created_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True)
    removed_by = models.CharField(max_length=120, blank=True)
    class Meta:
        constraints = [UniqueConstraint(fields=["session", "family", "value"],
                                        condition=Q(removed_at__isnull=True), name="one_live_tag")]
        indexes = [Index(fields=["family", "value"])]
```

- **Display form:** a tag is written `family:value`, or just `value` for
  family-less tags. Values are lowercase slugs (`[a-z0-9][a-z0-9._-]*`).
- **Single writer:** `django_ergo.bots.tags.set_tags(session, add=[...],
  remove=[...], source, actor, reason, call=None)` is the only code that
  changes tags. It does four things:
  1. Validates each tag against the bot's vocabulary (§3).
  2. Applies the family rules:
     - an **exclusive** family replaces its previous value;
     - **precedence** is user > agent > derived > decider, so a lower source
       never removes or replaces a tag set by a higher one.
  3. Writes the `ThreadTag` rows, then rewrites `session.tags` from the live
     rows with one `UPDATE … SET tags = …` that leaves `updated_at` alone,
     the same way pin works.
  4. Calls `notify()`.
- **Audit:** a removal sets `removed_at`. History is
  `ThreadTag.objects.filter(session=…)`. Nothing is ever hard-deleted.
- **Migration:** one migration that adds the column, the GIN index and the
  table, with no data step. The backfill is a separate, opt-in management
  command (§8, PR 8). It is never run against the live DB without Lee's
  go-ahead.
- **Title and summary stay where they are.** `user_title` and
  `metadata.title` keep their current rules. Summary becomes a new column,
  `summary` (TextField, blank). It is not a metadata key, for the reason in
  §1.1. It is written by `update_metadata` and on resolve, with
  `resolved_summary` kept for compatibility.

---

## 3. Vocabulary config

### 3.1 Layers

1. **Library defaults:** `DJANGO_ERGO["DEFAULT_TAGS"]` in `settings.py`. This
   follows the `DEFAULT_SKILLS` pattern.
2. **Deployment-wide file:** `tags.yaml` at the top of the bot path, beside
   `providers.yaml`, loaded once per registry. It holds the shared vocabulary
   and the project registry (§3.3). Every bot sees the same `project:` slugs.
3. **Per bot:** a `tags:` block in `bot.yaml` adds families, adds values to
   existing families, or excludes them. The parser is strict, in the style of
   `_self_health`.

### 3.2 Example

```yaml
# tags.yaml (deployment-wide; beside providers.yaml)
families:
  kind:
    exclusive: true          # at most one live value
    display: bare            # chip shows "fix", filter accepts "fix" or "kind:fix"
    decider: thread-kind     # which Decider picks this family (§5); omit = never auto
    values:
      fix:      "Repair broken or wrong behavior; incidents and debugging"
      feature:  "Build or extend a capability, page, tool or content"
      research: "Investigate, compare, audit or answer a question; no change shipped"
      review:   "Review PRs, health checks, recurring audits"
      ops:      "Deploy, merge, credentials, access, infra operations"
      design:   "UX, architecture, specs and plans before building"
  priority:
    exclusive: true
    set_by: [user, agent]    # deciders never set priority (see §5.6)
    values: [paused, low, medium, high, urgent]
  project:
    exclusive: true          # one primary project; omit when genuinely cross-project
    decider: thread-project
    values_from: projects    # templated family: values come from the registry below

projects:                    # the registry behind project:*
  - slug: ergonaut
    name: Ergonaut / django-ergo
    aliases: [ergo, django-ergo]
    repos: [boundcorp/django-ergo]
    tier: infrastructure
  - slug: storytime
    name: Storytime studio
    repos: [boundcorp/storytime]
    tier: sandbox
  - slug: audiographic
    name: AudioGraphic audience app
    tier: sandbox
  - slug: 321-meditate
    aliases: [meditate]
    repos: [boundcorp/321-meditate]
    tier: revenue
  - slug: ficsit          # not a repo; fine
    name: Satisfactory blueprints

# bot.yaml (per bot)
tags:
  include: [kind, priority, project]   # default: every shared family
  families:
    ficsit:                            # bot-only family
      values: [blueprint, factory, route]
  extend:
    kind: {values: {question: "Quick one-off ask"}}
```

**Validation, all at load time:**

- unknown keys are an error;
- each family's `values` and `values_from` are mutually exclusive;
- `decider` must name a configured Decider (§5.2);
- project slugs and aliases must be unique;
- a bot that excludes a family loses its filters and chips, but tags already
  stored on its threads stay.

`set_tags` normalizes aliases (`project:meditate` →
`project:321-meditate`). It rejects unknown values from every source except
`user`. A user can create a value on the spot, but only in a family marked
`open: true` (off by default).

### 3.3 How `project:*` values are listed

**Recommendation:** an explicit registry (`projects:` in `tags.yaml`), keyed by
a **lowercase slug**, with `aliases`, optional `repos: [owner/repo]` and
optional `tier`.

- **Slugs, not numeric ids or shortcodes.** Slugs are readable in tags, URLs
  and agent prompts, and they survive a DB reset. Shortcodes collide
  (storytime vs AudioGraphic).
- **`repos` connects the registry to the repo identity already in core.**
  The dev pack (§7) maps each worker's `canonical_remote` to a slug, so a
  thread whose workers all ran in `boundcorp/storytime` gets
  `project:storytime` with `source=derived`, without a Decider call.
- **Projects without a repo are still projects.** That includes ficsit,
  household, design, and the AudioGraphic app vs the Storytime studio. A
  registry built from repos alone would miss them.
- **`tier` lets an optional `risk:production` filter be derived** from the
  project's deploy tier instead of guessed by a model.

Alternatives:

- **(b) Derive the list from repo remotes alone** (devbox's approach). It
  misses non-repo projects and produces names like `remote:ab12…`.
- **(c) A `Project` BotTable editable in the UI.** Nicer to edit, but it is
  per bot, and the vocabulary moves out of reviewed config. Worth revisiting
  if Lee wants to add projects from a phone.
- **(d) A free-form `project:<anything>` matching a regex.** It drifts into
  synonyms (`holy-word`/`holyword`), which all three research slices warned
  against.

Seed the registry from the `~/ENVIRONMENT.md` project cards and
`.tags-research/thread-corpus.md` (§9).

---

## 4. Filtering

### 4.1 API

- **`SessionOut` gains:**
  - `tags: list[str]` (display form);
  - `tag_sources: dict[str, str]`, so the UI can mark decider tags as
    suggestions;
  - `summary`.
- **Tag query parameters on `GET /sessions`:**
  - `tag` may repeat. Separate `tag` parameters AND together; a comma inside
    one parameter is OR. Example:
    `?tag=project:storytime&tag=kind:fix,kind:feature`.
  - `tag=project:*` means "has any project tag".
  - `tag=-project:*` means "no project" (the untagged review queue).
  - `not_tag=` excludes.
  - SQL: `tags @> ARRAY[...]`, `tags && ARRAY[...]`, and a
    `EXISTS (unnest … LIKE 'project:%')` for families. All of these are
    served by the GIN index except the family wildcard, which is fine at
    current volume.
- **New endpoints:**
  - `POST /sessions/{id}/tags {add: [], remove: []}` with `source=user`,
    following the pin pattern.
  - `GET /bots/{bot}/tags` returns the effective vocabulary, with
    descriptions and project names, for pickers.
  - `GET /sessions/{id}/tags/history` returns the audit trail.
- **`bot` filter:** stays a column filter, not a tag. The research slices
  agreed that derived facts (`bot`, `status`/bucket, PRs, worker counts,
  size) should stay structured filters and not be copied into tags.

### 4.2 UI

- **Chips:**
  - In `FullRow` (`ThreadList.tsx`), placed next to the PR chips.
  - In `SlimRow`, shown on hover only, or limited to `priority` and
    `project` so the sidebar stays clean.
- **Threads page** (`ThreadsPage.tsx:104-117`): a tag chip row under the bot
  chips, kept in the URL as `?tag=`, with client-side filtering because
  `App.tsx` already holds the list.
- **Sessions page:** a tag multi-select sent to the server.
- **Chat header:** a tag editor next to `SessionTitle` (`Chat.tsx:775`).
  Decider tags appear dimmed with a ✓ (accept, which turns them into user
  tags) and a × (reject, which removes them and records the rejection, so the
  Decider never proposes that value again on this thread).
- **Sidebar:** an optional "filter by project" select, remembered per
  browser through `persistedFlag.ts`.

### 4.3 Agent tools

- **`ergo_thread_list`** gains:
  - `tags: list[str]`, with the same AND/OR grammar as the API;
  - `tags` in each row (`_row()`).
- **`ergo_portfolio_status`** gains:
  - `tags` as a filter;
  - `tags` in each `_thread_row`;
  - optionally `group_by="project"`, which answers "what's in flight for
    Storytime" in one call.
- **"Bots and threads" context line** (`overview.py`): appends `[project, priority]`
  when present, to give the model orientation cheaply.

---

## 5. Deciders

### 5.1 The interface

A Decider is a named, configured, cheap function. **It chooses from a closed
list of candidates** and never generates free text.

```python
# src/django_ergo/conversation/deciders.py  (core layer: no bots/ imports)
@dataclass(frozen=True)
class Candidate:
    id: str            # "project:storytime"
    text: str          # what the model scores: "Storytime studio: scripts, episodes, Ryan rendering…"

@dataclass(frozen=True)
class Pick:
    id: str
    score: float       # normalized 0..1
    rank: int

@dataclass
class Decision:
    picks: list[Pick]          # already thresholded and capped
    scores: list[Pick]         # every candidate, for audit and calibration
    backend: str               # "rerank:rigel", "rerank:octo-cpu", "llm:auto/small", ...
    call: StructuredCall       # the audit row (see 5.4)

class Decider(Protocol):
    name: str
    def decide(self, query: str, candidates: Sequence[Candidate], *,
               k: int = 1, threshold: float, margin: float = 0.0,
               user=None, session=None, purpose: str) -> Decision: ...
```

**Backends:**

1. **`rerank`** (the default and cheapest).
   - Request: TEI `POST /rerank {query, texts, raw_scores: false}` against
     `hs-rerank-1`. The query is a short digest of the thread (§5.5); the
     texts are each candidate's `text`.
   - A single cross-encoder pass scores every candidate. There is no
     generation, so the output can only be one of the candidates.
2. **`llm`** (when reranking is too weak, e.g. a `kind` that depends on
   phrasing).
   - A structured call (`run_structured_call`) whose `response_model` has
     `Literal[...]` fields built from the candidate ids. `max_turns=1`,
     `max_tokens≈64`, routed to `auto/small` through `pick_model` and
     `model_choice.check`, the way the bash reviewer is.
   - Pydantic rejects any value outside the list, and the call corrects
     itself once.
   - It respects `make_ancillary_engine`'s private-pool rules.
3. **`decide`** (optional, later). This would use `hs-decide-1-flash`
   through the gateway's `/decide` route, which already has the full Rigel →
   Hindsight Cloud → Octo CPU chain. It is held back because `/v1/systemone`
   is Hindsight's own protocol and is not known to support general
   classification prompts (§10, Q6).

**Combining backends:** `rerank`, then `llm` only when the top-2 margin is
below `margin`. Most calls end at the reranker.

### 5.2 Config (providers.yaml)

Deciders are model routing, so they belong beside the tiers:

```yaml
# providers.yaml
deciders:
  thread-kind:
    backend: rerank
    endpoint: llm-gateway          # named endpoint below
    threshold: 0.55
    margin: 0.10
    escalate: llm                  # on low margin, ask auto/small
    llm_model: auto/small
  thread-project:
    backend: rerank
    endpoint: llm-gateway
    threshold: 0.60
    k: 1
endpoints:
  llm-gateway:
    url: ${ERGO_DECIDER_URL}       # http://llm-gateway.llm.svc.cluster.local:8080
    api_key_env: ERGO_DECIDER_API_KEY   # Secret; value never in yaml
    rerank_path: /rerank-ergo
    timeout_seconds: 2.0
```

The library default is `DJANGO_ERGO["DECIDERS"] = {}`. With nothing
configured, auto-tagging stays off and nothing breaks.

### 5.3 Rigel primary, Hindsight Cloud fallback

All failover stays in the gateway, as it does today. Ergo makes one HTTP call
with a short timeout.

- **Gap:** on :8080, `/rerank` is Octo CPU only (§1.6). Ergonaut cannot use
  :8082 because it is unkeyed, belongs to Hindsight, and is not admitted by
  NetworkPolicy.
- **Proposed infra-octo PR:** add a keyed `/rerank-ergo` route on :8080 with
  backends:
  1. `be_rerank_rigel` (1s timeout);
  2. Octo `rerank-cap` :8090 (1.5s, top 40, which is plenty for label
     lists);
  3. optionally Hindsight Cloud's Cohere-compatible `/v1/rerank`.

  For label reranking the candidate lists are tiny (6-40 items), so the Octo
  CPU backup is fast enough. **Recommendation: Rigel → Octo CPU, no paid hop
  by default.** The paid hop can be added behind a flag if Rigel and Octo
  both turn out unreliable.
- **Response header:** the gateway adds `X-Llm-Backend: rigel|octo|cloud` so
  Ergo can record which backend answered and price only paid hops.
- **Ergo side:**
  - `timeout_seconds` (default 2s), no retries;
  - on timeout or 5xx, `Decision` is empty and recorded as `status=failed`;
  - the tagging job reschedules itself once (Celery countdown 10 minutes),
    then gives up.

  Tags are optional; a failed Decider never blocks a turn.

### 5.4 Cost and audit

- **Audit row:** every `decide()` writes one `StructuredCall` row:
  - `kind="decider:<name>"`;
  - `request` = the query digest plus candidate ids;
  - `response` = the scores;
  - `metadata.backend`.

  This reuses the existing table, admin and tests. No new model.
- **Cost:**
  - `rerank` on Rigel or Octo costs $0 (self-hosted). It is recorded as
    `cost_usd=0`, not NULL, so the cost views do not show it as unpriced.
  - The Cloud hop (if enabled) is priced from `MODEL_PRICES["hs-rerank-1"]`.
  - The `llm` escalation is priced by `add_request_cost`, as usual.
- **Visibility:** a daily count and cost summary by `kind` appears in the
  self-health metrics (`bots/self_health.py`), so a runaway loop is visible.

### 5.5 When it runs, and keeping it cheap

| Trigger | What | Notes |
|---|---|---|
| Thread created (in `name_thread`, after the title) | `thread-kind`, `thread-project` | Query = title + first user message (cut to 1500 characters) |
| First compaction, or 40 messages, whichever comes first | rerun both | Query = title + latest compaction summary. Only fills families that have no user or agent tag |
| Resolve (`ergo_thread_resolve`) | rerun both | Query = title + `resolved_summary`. Final tags for the record |
| `ergo_thread_update_metadata(retag=true)` | rerun | The agent says the direction changed |
| Derived (no model) | `project:` from worker remotes (dev pack) | Runs on worker scan |

Cost controls:

- Never more than **3 automatic runs per thread** (`metadata` counter, or
  count the `StructuredCall` rows).
- Skip any family that already has a user or agent tag.
- Skip `named`/`main` chats unless configured, since their topic varies by
  nature (slice C).
- Candidate text comes from config descriptions, so one rerank request is one
  query against at most ~40 short texts.
- Run in the Celery `bot_tasks` queue, never inside a turn.

### 5.6 Keeping picks inside the vocabulary

- **Candidates come only from the effective vocabulary** of the thread's bot
  and family. The Decider cannot return anything else.
  - Reranking scores the given texts, so it cannot invent a label.
  - The `llm` backend's `Literal` type validates against the list.
- **Thresholds:**
  - an exclusive family takes the top-1 only if `score ≥ threshold` and
    `top1 − top2 ≥ margin`;
  - a multi-valued family takes up to `k` candidates above the threshold.
- **Abstaining is a valid answer:** no tag beats `project:misc`. Slices B and
  C both flagged `misc` as a catch-all that hides gaps. The `-project:*`
  filter becomes the review queue.
- **Families a Decider never touches:**
  - `priority`, because all three research slices said urgency must come from
    explicit intent and not wording;
  - any family with `set_by` that leaves out `decider`.
- **Respecting the user:** Decider tags are `source=decider` with a
  confidence. They never overwrite user or agent tags, and a rejected value is
  remembered (§4.2).
- **Calibration:** the ~300 titles labeled in §9 become a fixture
  (`tests/fixtures/thread_tag_eval.jsonl`). An opt-in test (`TEST_DECIDER=true`,
  like `TEST_OPENAI`) reports accuracy and abstain rate per family against
  the real endpoint. The default test run uses a fake reranker.

---

## 6. `ergo_thread_update_metadata` agent tool

Ergo tool names start with `ergo_`, so the tool is
`ergo_thread_update_metadata`. It is registered with the other `ergo_thread_*`
tools in `bots/orchestrator.py`.

| Arg | Type | Effect |
|---|---|---|
| `thread` | str, optional | Defaults to the current thread. Other threads are allowed only under the `ergo_thread_resolve` ownership rule: same user, and either this bot started it or it is this bot's thread |
| `title` | str ≤ 80 | Writes `metadata.title` **only when `user_title IS NULL`**. Otherwise it returns `"kept user's title"`. A user rename always wins |
| `summary` | str ≤ 2000 | Writes `session.summary`, a one-paragraph "what this thread is about now". It is shown in the portfolio snapshot and used as the Decider query |
| `add_tags` / `remove_tags` | list[str] | `source=agent` and must be in the vocabulary. The agent cannot remove `user` tags, and can remove `decider` tags |
| `priority` | enum | Shorthand for setting `priority:*`. Allowed only if the family's `set_by` includes `agent`, and the tool description says "only when the user stated urgency" |
| `project` | slug or alias | Shorthand for `project:*` |
| `retag` | bool | Queues a Decider run for families without user or agent tags |
| `reason` | str, required | Stored on each `ThreadTag.reason` and shown in the history |

- **Permissions:** the tool is available to every bot by default and can be
  turned off with `tools.exclude`, as usual. It never changes `status`;
  resolve, reopen and close keep their own tools.
- **Rate limit:** 10 calls per thread per hour, so a confused agent cannot
  churn tags.
- **Prompt guidance:** a few lines go into the core "threads" skill and
  instructions: call the tool when the user changes the task or project, not
  every turn.

---

## 7. Projects: recommendation

**Recommendation: split it. A small, generic piece goes in core. The repo
analytics go into an official "dev" plugin bundle. Domain projects stay where
they are.**

| Piece | Where | Why |
|---|---|---|
| Project registry (`projects:` in `tags.yaml`: slug, name, aliases, repos, tier) and the `project:*` family | **Core** (`bots/tags.py`) | `project:` is a default tag family on every bot, and many projects are not repos. It is plain config with no dev assumptions |
| Repo identity (`canonical_remote`, `project_key`, `AgentWorkspaceObservation`) | **Core, already there** | No change |
| Mapping a worker's canonical remote to a registry slug, giving `project:` tags with `source=derived` | **Dev pack** | It only matters where coding agents run |
| Project analytics page (move `ergo-bots/devbox/tools/projects.py` + `pages/projects.jhtml`), keyed by slug and falling back to remote | **Dev pack** | Makes the devbox page reusable by any dev bot |
| Filling `CostOperation.project_ref` with the slug (agent usage, and the thread's project tag for chat turns) | **Dev pack** writes it; core keeps the field | Gives the costs page per-project totals across chat and agent spend |
| Mapping PRs (`pull_requests:` in bot.yaml) to projects | **Dev pack** | |
| Storytime `Project` (series/episodes), math billing projects | **Stay in their bots and apps** | Different meaning. They may *use* the same slugs as `project:` tags (Storytime already tags video jobs `project:<slug>`) but are not merged |

- **Form of the dev pack:** an official plugin, `plugins/dev.py`
  (`plugins: [{name: dev}]`), alongside `orca`, `bash` and `kubectl`, rather
  than a separate package. If it grows past one plugin, it becomes a skill
  library entry that turns on `dev`, `orca`, `bash` and `kubectl` together
  ("dev extensions").
- **No `Project` DB model for now.** The registry is config, and a slug is the
  key everywhere. Revisit only if projects need fields that change at runtime
  (budgets, status), which is option (c) in §3.3.

---

## 8. Phased plan

Each phase is one small PR with tests. The PRs run in this order, and each one
can ship alone.

1. **Vocabulary config.**
   - `bots/tags.py`: `TagVocabulary`, `Family`, `Project`, alias
     normalization.
   - `tags:` parsing in `definition.py`, plus `tags.yaml` loading next to
     providers.yaml.
   - `DJANGO_ERGO["DEFAULT_TAGS"]`.
   - Docs: `docs/bots.md` and a new `docs/tags.md`.
   - No DB.
   - Tests: strict parsing errors, merge and exclude, alias resolution,
     templated `values_from`.
2. **Storage.**
   - `ThreadTag` and `ConversationSession.tags`/`summary`, with the migration.
   - `set_tags()`: precedence, the exclusive rule, the array kept in step,
     no change to `updated_at`.
   - Tests cover all four rules, plus concurrent writers.
3. **API.**
   - `tags`/`summary` in `SessionOut`, the `?tag=` grammar, and the
     `POST /tags`, vocabulary and history endpoints.
   - Ergonaut API tests.
4. **UI.**
   - Chips (FullRow, SlimRow), the Threads page tag filter row, the Sessions
     page select, and the chat header editor with accept and reject.
   - Frontend tests where they exist, plus screenshots in the PR.
5. **Agent tools.**
   - `tags` filters in `ergo_thread_list` and `ergo_portfolio_status`, and
     tags in rows and the overview line.
   - The `ergo_thread_update_metadata` tool.
   - Tests for permissions, user-title protection and the rate limit.
6. **Decider core.**
   - `conversation/deciders.py`, the `llm` backend (structured call with
     `Literal`), `deciders:` in providers.yaml, `StructuredCall` audit rows.
   - Hooks in `name_thread` and resolve.
   - Fake-client tests, including abstain, margin and "never sets priority".
7. **Rerank backend.**
   - The TEI `/rerank` client, the endpoint config, the fake reranker, and the
     opt-in eval against the §9 fixture.
   - Ships with a separate **infra-octo PR** for the `/rerank-ergo` gateway
     route and `X-Llm-Backend`, plus an `ERGO_DECIDER_*` Secret, through Flux.
8. **Dev pack and backfill.**
   - `plugins/dev.py`: the derived `project:` tags from workers, the moved
     analytics page and `CostOperation.project_ref`.
   - `manage.py backfill_thread_tags --dry-run`, which shows suggestions only.
     A real run needs Lee's go-ahead.

---

## 9. Tag vocabulary from history review

Three research agents tagged about 300 real thread titles by hand:

- **Slice A:** devbox bot, 10-02 to 11-09, 120 threads.
- **Slice B:** devbox bot, 11-09 to 11-25, 153 threads.
- **Slice C:** the other bots (boundcorp, ads, storytime, ficsit, design,
  kitchen).

They worked from titles only, so every label is provisional. Their full
proposals and coverage tables are in the coordinator's research worktrees and
are not committed here.

### 9.1 Where they agree

- **Use a small shared core, extended per bot.** Slice C showed that a
  dev-only vocabulary does not fit household, editorial, ads or blueprint
  work. Bot-specific families such as `ficsit:{blueprint,factory,route}`,
  `story:{writing,production,design,audience}`,
  `ads:{campaign,creative,content,analytics}`,
  `kitchen:{recipe,shopping,integration}` and `design:{brand,ux,asset}`
  belong in each bot's `tags:` block.
- **Derive facts; let people and models choose intent.** `bot`, open or
  resolved state, worker and PR status, spend and consent, and size come from
  records. They stay structured filters and are not copied into tags. Only
  `kind`, `project` and `priority` (plus bot families) are chosen.
- **Priority must be explicit.** Never infer it from message count, wording
  such as "fix", or thread length. `paused` is a state, not an urgency (A),
  and Ergo already has `SessionStatus.paused`. **Open question Q1.**
- **One primary `project:` per thread**, with canonical lowercase slugs plus
  aliases. Keep `storytime` (studio) separate from `audiographic` (app).
  Normalize `holy-word`→`holyword` and `meditate`→`321-meditate` (C). Do not
  use `project:misc` as a permanent bucket; prefer no tag plus a review
  filter (B, C).
- **Keep history and provenance.** Re-tag on a real change of scope, not on
  every message.

### 9.2 The `kind` family: three proposals, one merged set

| Slice A | Slice B | Slice C / Lee | **Proposed** |
|---|---|---|---|
| build | implementation, task | feature | `feature` |
| debug | incident | fix | `fix` |
| research | research | research | `research` |
| — (in research) | review | — | `review` (recurring health checks and audits are a large share of slice B) |
| ops | — | — | `ops` (merge, deploy, credentials, access) |
| design | planning | — | `design` |
| question | — | — | (bot extension only) |

### 9.3 `project:` seed list

The seed list comes from the corpus's "known projects" line, the slices and
the ENVIRONMENT.md project cards:

> ergonaut (aliases ergo, django-ergo), ergo-bots, devbox, infra-octo, homeops
> (cluster-boundcorp-homeops), hindsight, rigel, storytime, audiographic,
> holyword, 321-meditate (meditate), ads (ad_manager lineage), papa, pds,
> justposted, math, gigabrain, gigabot, print-toys, orca, ficsit, kitchen.

**Coverage, title-only, from slice B:** every thread got a `kind`, and every
thread got a `project` *only after* falling back to `misc`. About 29 of the
153 (19%) needed `misc`. Many of those were vague "(link) …" titles or
cross-cutting tooling work. This supports the per-family abstain design and
the `-project:*` review queue, rather than a forced pick.

### 9.4 Rejected candidates

- `size:` (from message count) and `state:` / `actor:` / `bot:`: these are
  derived facts, already filterable or cheap to add as structured filters.
- `area:`: overlaps `project:` (A's own warning).
- Free-form synonym families (`bug`/`fix`, `ops`/`infra`).
- `risk:production`: useful, but **derive it from the project `tier`**, not a
  model. It can be a later optional derived family.

---

## 10. Open questions for Lee

1. **Priority values.** Keep `priority:paused`, or rely on the existing
   `paused` status and use `priority:{low,medium,high,urgent}`? May agents
   set priority when the user states urgency (`set_by: [user, agent]`), or
   only users?
2. **Bare tags.** Show `kind` values bare (`fix`, `feature`, `research`), as
   you wrote them, while storing them as `kind:*`? (Recommended.) The
   alternative is truly family-less tags.
3. **One project or several.** Exclusive `project:` (one primary) is
   recommended. Should cross-project threads allow two?
4. **Where the registry lives.** `tags.yaml` beside providers.yaml
   (recommended), in each bot.yaml, or a BotTable editable from the UI?
5. **Paid fallback for reranking.** Rigel → Octo CPU only (recommended, $0),
   or also Hindsight Cloud for label reranking?
6. **hs-decide.** Is `hs-decide-1-flash` / `/v1/systemone` meant for general
   prompts? If so, it would be a better `llm` escalation than `auto/small`,
   with a free primary and an existing Cloud fallback. Separately, `qwen3-4b`
   evicts hs-decide on Rigel, so it is not a good Decider target.
7. **Auto-tag the existing threads?** A backfill is a dry-run command first.
   Would you rather accept suggestions one at a time in the UI, or apply all
   tags above the threshold in bulk?
8. **Dev pack scope.** Move the devbox analytics page into the official `dev`
   plugin now (PR 8), or keep it in ergo-bots until tags have been in use for
   a while?
