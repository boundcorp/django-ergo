# Spec: Agent session usage on the Costs page

Repo: boundcorp/django-ergo (public; keep deployment details out). Branch from `main`; open a PR.

## Goal
Coding agents that Orca workers run (Claude Code, Codex, omp) spend tokens that Ergonaut never sees,
because the agent runs outside Ergo's structured calls. Capture that usage per worker and show it on
the Costs page as its own **Agent sessions** section, separate from the existing Usage section (bot
turns). Workers in a remote Orca environment count too. Subscription usage is tokens, not dollars.

## Where the data is
Orca's CLI does not report usage: `orchestration worker-read` returns transcript messages without
usage, and Orca strips the transcript path from its output (checked against stablyai/orca main,
2026-10-05; its own usage trackers in `src/main/{claude,codex}-usage/` are desktop-only, with no CLI
command). The agents' own session files carry usage, on the host that holds the worktree (the Orca
plugin's `files_host`, already used over ssh by `orca_attach`):

- **Claude Code**: `~/.claude/projects/<slug>/**/*.jsonl`, where slug is the worktree path with every
  non-alphanumeric character replaced by `-` (subagent files sit in `<session>/subagents/`). Rows with
  `type: "assistant"` carry `message.usage` (`input_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `output_tokens`), `message.model` and `timestamp`. A streamed message repeats
  its row: dedupe by `message.id` + `requestId`, keeping the largest counts. Skip model `<synthetic>`.
- **Codex**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. The `session_meta` row's `payload.cwd`
  says which worktree; `turn_context` rows give `payload.model`; `event_msg` rows with
  `payload.type == "token_count"` carry `payload.info.last_token_usage` (else take the change in
  `total_token_usage`; skip repeated identical snapshots and `info: null`). Codex `input_tokens`
  includes `cached_input_tokens`: store uncached input as input minus cached, cached as cache read.
  `reasoning_output_tokens` is part of output.
- **omp**: `~/.omp/agent/sessions/<folder>/*.jsonl`. The first row is `{"type": "session", "cwd": ...}`;
  rows with `type: "message"` and `message.role == "assistant"` carry `message.usage` (`input`,
  `output`, `cacheRead`, `cacheWrite`) and `message.model`. Treat omp as best effort.

Attribute usage to a worker by worktree path plus time window: rows stamped between the Worker's
`created_at` (less a minute) and its `completed_at` (or now). Two workers running in the same
worktree at the same time would double count; note it in the docs, don't solve it.

## Capture
- A standalone, stdlib-only script (e.g. `src/django_ergo/plugins/agent_usage_scan.py`,
  `scan(agent, cwd, since, until) -> {"models": {model: {input, cache_write, cache_read, output,
  reasoning, requests, first_at, last_at}}}`). The Orca plugin pipes it to `python3 -` over
  `ssh <files_host>` (or runs it locally when `files_host` is empty) and reads one JSON line back.
- `orca_start_worker` records `agent` and the worktree in the Worker's state so the watcher knows what
  to scan; resolve the worktree path once and keep it in state.
- `orca:watch` scans at most every `usage_minutes` (new plugin key, default 10) and once more when the
  dispatch settles or fails. Best effort: a failed scan keeps the last numbers and logs, never fails
  the worker. Existing workers without the new state are skipped.
- Store results in a new model in `django_ergo.conversation` (e.g. `AgentUsage`): worker (FK, SET_NULL),
  session (FK), bot_name, source (`"orca"`), agent, model, the token parts, requests, first_at,
  last_at, timestamps. One row per (worker, model), replaced on each scan. Migration included.

## Costs API and page
- `GET /api/costs` gains `agents: {headline: {sessions, tokens, cache_hit}, rows: [...]}` for usage whose
  `last_at` is in range, with the same bot filter and scoping (superusers see all, others their own
  sessions' workers). A row: worker id, worker title, agent, model, chat id and title, bot, worker
  status, the four token parts plus reasoning, tokens, cache hit, requests. Keep every existing field.
- `Costs.tsx`: an **Agent sessions** section after Usage, same look: tiles (agent sessions, tokens,
  cache hit), a token-mix bar, and a table (worker title linking to its chat, agent, model, tokens,
  cache hit, requests, status). Agent tokens do not go into the Usage section's totals.

## Tests and docs
- Library tests: the scanner on fixture files for all three agents (dedupe, cached split, time window,
  cwd matching); the watcher stores and refreshes `AgentUsage` and survives a failed scan.
- `ergonaut/ergonaut/apps/bots/tests/test_costs.py`: the `agents` block, range, bot filter, scoping.
- Docs: the Orca plugin docstring and `docs/bots.md` (new key, what is captured), `docs/ergonaut.md`
  (Costs page). Update `TODO.md`.

## Acceptance
`make pytest`, `cd ergonaut && make test`, ruff, and frontend lint/typecheck pass. Light and dark
screenshots of the Costs page on the PR.
