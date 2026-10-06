# Django Ergo - Development Tasks

The bots and Ergonaut backlog. Older 2025 tasks are in [docs/archive/TODO-2025.md](docs/archive/TODO-2025.md).

## 🤖 Ergonaut bots (PR #31 merged 2026-10-02 as b1bd99c)

Live on rigel from `~/p/boundcorp/django-ergo`, bots from `boundcorp/ergo-bots`. Updated by whoever works the branch.

### Now (2026-10-02, refreshed 2026-10-03)

- [x] **PR #31 review and merge**: two review passes (22 findings) fixed in 2e54508/e3a2345; CI green; merged b1bd99c.
- [x] **Bots that learn**: `ergo_kb` `write: true` gives `ergo_kb_write` (kb/*.md only, committed and pushed). Kitchen turned on in ergo-bots.
- [ ] **Deploy plan** for Ergonaut on the octo cluster (modeled on kitchen-mgmt's infra/octo).
- [x] **Schedules in each bot's bot.yaml** (`schedules:`, run every minute by Celery beat; bot page lists them with the next run). PR from `claude/bot-schedules`.

- [x] **Toolkit pre-seeding**: toolkits declare tool calls run and written into the chat before the first completion; orchestration pre-seeds `ergo_bot_list` (names + YAML descriptions); skills use the same path; boundcorp's instructions stop listing bots.
- [x] **Turns as Celery tasks**: web messages and approvals queue a turn task (inline without a broker); Redis pub/sub wakes the SSE stream; rigel runs `up web worker beat`.
- [x] **Thread-to-thread messaging**: messages record a sender; replies go back to the sender's thread; `ergo_thread_list(bot)` and async `ergo_thread_send(bot, root|<id>|new, message)`; hop limit; replaces sync `threads_*` and `ergo_bot_call`.
- [x] **Thread inactivity and archival**: beat archives threads idle `sessions.archive_after_days` (default 7); archived threads collapse in the sidebar; messaging one reopens it.
- [x] **`@bot_task` for custom tools**: run a bot-folder function on a worker and wait for or await its result.

### Next

- [x] **Docs** (PR #69; OpenAI made the default in examples and docs in a follow-up): full README; guides for getting started, building bots, skills, tools, plugins, memory and KBs, data tables, attachments, schedules, running Ergonaut, development; 2025 planning docs and old specs moved to `docs/archive/`.
- [x] **Skills unified + named chats** (PR #33, merged): lazy skills (load/unload, always per chat, requires), main replaces root, named chats in bot.yaml, schedule targets.
- [x] **Skill library + skillbuilder**: Ergo ships reusable skill folders (`bots/skill_library/`) a bot includes by name (`skills.include`, chat skills or `requires`); skills declare the plugins they need (`plugins:` front matter, conflicting bot.yaml settings fail the load); `@bot_tool(seed=True)`. First library skill: `skillbuilder` (config_repo in PR mode plus a guide to tools, workers, attachments, seeding, tables, schedules and .jhtml pages).
- [x] **Switch a chat's model across engines**: the picker sets `ConversationSession.model` for every later turn. Messages are engine-neutral (`SessionMessage` + `MessageBlock`, rendered per engine when sent; migration 0030 copied OpenAI chats in). Drop the legacy `OpenAIMessage` table next release.
- [x] Every bot gets `skillbuilder` (ergo-bots #48), and `DJANGO_ERGO["DEFAULT_SKILLS"]` (default `["skillbuilder"]`) gives it to every folder bot unless bot.yaml excludes it.
- [x] **Schedule actions** (PR #34, merged): ordered `prompt` and `run` steps; `run` calls bot-folder Python and records a BotJob.
- [x] **BotTable** (PR #35, merged): real Django models per bot, migrations in the bot folder's `migrations/` (bot_management writes them into the proposal), `ergo_bot_migrate` at start and after pulls, `tables` skill.
- [x] **Pages and pins** (PR #36, merged): live .jhtml pages (sandboxed Jinja over tables), blocks, the `pages` plugin, chat pins (bot-folder files and pinned chat files), page viewer in the chat, `ctx.table()` for schedule code.
- [x] **Fixes from the Ad Manager test** (PR #37, merged): code-only schedules run once as the first admin; pulls only migrate after real changes; page sums are unknown, not zero.
- [ ] **Ad Manager bot (`ads`, ergo-bots #3, merged)**: written by boundcorp; live on rigel, table migrated, dashboard pinned. Token in place 2026-10-02; first 30-day pull: 63 rows, 16 campaigns, $302.54 (matches the 10-01 report for Sep 29-30). Pulls every 12 h.
- [x] Draft page preview (PR #38, merged): `ergo_config_repo_preview` / `ergo_bot_preview`.
- [x] Files: viewer for chat files (#39), bot folder browser (#39) with proposed changes, diffs, merge/close/discard (#40, #42, #43); pinned files in the sidebar (#40); `ergo_config_repo_delete` (#40, #41).
- [x] A broken bot folder no longer stops the others: skipped at start, its last good version kept on reload, shown to admins in the sidebar.
- [x] Workers under threads (#48): polling workers, busy while running, result back to the thread; `orca_start_worker`.
- [x] Stop, steer and interrupt a running turn: messages go through a per-session inbox and steer the turn at its next step; Stop (`POST /sessions/{id}/stop`) ends it as `stopped`; "Stop & send" interrupts (`mode: "interrupt"`).
- [x] Model picker from `providers.yaml` (#65, #67); recover chats left busy by a dead turn (#66); `ergonaut manage wait_idle` (#70); window/rolling compaction names (#71); stub older large tool results (#68).
- [x] Steer and stop turns another chat or worker started (#73); Unsend (#75); usage saved after every request (#76, #77); Resume button for a failed turn with plain-words error cards (#78); files shared with thread messages and `orca_upload` (#79).
- [x] Stop bot-to-bot ping-pong (#81, replaced #74): a second request to a chat with one still open is refused; reply headers tell bots not to send thanks or nudges.
- [x] Ergonaut restyled to the dark design handoff (#83).
- [x] Threads by status: the sidebar groups each bot's threads as Pinned, Waiting on you, Working, Idle and Resolved, with the bot's one-line `ChatReply.status`; a full Threads page across bots with PR and worker chips and pins (includes #98's Resolved wording).
- [x] Inter-bot permissions: upward messages always allowed (`ergo_message_up` for bots without orchestration), new threads decided by the target's `threads.allow_create`, cross-bot threads record who started them, `ergo_thread_stop` and `ergo_thread_archive` for threads a bot started (`TURN_STOPPER`).
- [x] Markdown in chat messages, KB articles, skills and instructions; tool images and files a bot makes shown in the transcript as they arrive; bot.yaml `icon` and `color` in the sidebar; titles and icons on pins; thin scrollbars; long chats load the newest 50 messages with See more for older ones.
- [x] Thread cards (status, reply, PRs) for work sent to other chats and workers; collapsed replies; "Sent to" notes; thread links; PRs recorded from replies and worker results with live state from `gh`.
- [x] Ergonaut's `DJANGO_ERGO["OPEN_PRS"]` reads the recorded PRs, and thread cards use `orchestrator.thread_status`.
- [x] Claude on a subscription: `transport: cli` providers run Claude through the logged-in Claude Code CLI (`conversation/engines/claude_code.py`).
- [ ] Routing by tier: `auto/low|medium|high` chats and bots, `agents` tiers for `orca_start_worker(tier=...)`, limits from each CLI's 5-hour and weekly windows, `routing.md` compiled to limits (`bots/routing.py`). Ergonaut's Routing page (`/routing`, `GET/PUT/DELETE /api/routing`) shows usage, picks and switches and edits the priorities. A turn refused at its limit offers Retry on the tier's next model (never automatic).
- [ ] OpenAI on a ChatGPT subscription: `transport: cli` on `openai` providers runs Codex `app-server` (`conversation/engines/codex_cli.py`); try it on a logged-in machine (`examples/codex_subscription_demo.py`), then install Codex in the Ergonaut image. (Tested on rigel 2026-10-05: works on gpt-6-sol and gpt-6-luna.)
- [ ] Ergonaut image: install the Claude Code CLI and keep its login (`CLAUDE_CONFIG_DIR` volume or `CLAUDE_CODE_OAUTH_TOKEN`); show subscription usage as such on Costs instead of list prices.
- [ ] `ergonaut manage resume_failed --kind credits` (resume every chat a credit outage failed): written in bda8305 on the closed #74 branch, never merged.
- [x] `ergo_thread_resolve` (was archive): finished threads are resolved by their orchestrator or themselves, refused while anything is open; the block marks "ready to resolve".
- [x] The block marks "ready to resolve" only on threads the viewing bot may resolve (`orchestrator.can_resolve`, shared with the tools); a parent no longer sees an actionable hint on a sub-bot's self-started thread, and the refusal names who can resolve it.
- [x] Each chat is told which chat it is ("This chat" block), and upward messages are one-way reports unless they `ask` (no acknowledgement turns).
- [x] "Bots and threads" context block for orchestrating chats: every reachable bot, its chats with their status, latest-message snippets and open PRs, capped near 3k tokens (`bots/overview.py`, `orchestrator.thread_status`, `pull_requests:`, `OPEN_PRS`).
- [x] Design to devbox handoff run live 2026-10-03: Design sent the History handoff itself, devbox picked Sonnet 5.5 from ModelsAvailable, an omp worker built it (#87 fixed omp model pinning), draft PR #88.
- [x] `introspection` built-in skill for every bot loaded from a folder: read-only `ergo_self_overview`, `ergo_self_files`, `ergo_self_read` (bot folder, and Ergo's source via `ergo:` paths).
- [x] A bot's send right after a forward (or a report) to the same thread no longer fails on the no-nudge rule (a report or forward has no reply to wait for); a busy thread queues messages in order, one turn each, and sends them on whenever its turn ends (also after a failed or stopped one, and promptly under Celery); `ergo_thread_send` results say `queued` and the position; optional `interrupt` replaces the chat's own running request, never the user's.
- [x] Live Orca worker activity: worker cards show the agent's latest tool calls and output, time since its last activity with a stalled flag (`stall_minutes`), and its full recent log (`/api/sessions/<id>/workers/<id>/log`).
- [x] Agent session usage: Orca worker session files record Claude Code, Codex, and omp tokens per worker; Costs shows them separately from bot turns.
- [ ] Try `orca_start_worker` live on devbox (after the Orca display restart).
- [ ] A visual blocks editor in the UI. Campaign names are Ad Manager's internal `AM:<uuid>:campaign` labels; friendly names would need Ad Manager's mapping.
- [x] **Agent skills**: `ergo-client` (with the `ergonaut-remote` command and `ergo_client_*` bot tools), `ergo-hosting`, `ergo-bot-development` and `ergo-developer` in the skill library, installable for Claude Code and Codex (`skill_library/install.py`); Ergonaut API keys (`Authorization: Bearer ergo_...`, API keys page, `ergonaut manage api_key`).
- [ ] Install the agent skills on the dev box, make an API key on the production server, and start delegating django-ergo and ergo-bots work through `ergonaut-remote`; feed what the runs show back into the skills.
- [x] **Bot data and pages** (decided 2026-10-02): pinned files (repo files and bot-written attachments), blocks-based page toolkit, live .jhtml Jinja pages served straight from the file/attachment endpoints, templates from bots or PRs. No sandbox origin; all users share rows.

- [x] Upgrade path: `wait_idle` also waits for queued or running workers; `ergonaut upgrade` and `ERGONAUT_AUTO_UPGRADE_SECONDS` upgrade to a new GitHub release once idle through a pluggable `ERGONAUT_UPGRADER` (`systemd`, `command`, or a class in the bot repo); images record `ERGONAUT_VERSION`.
- [ ] Start publishing GitHub releases of django-ergo (auto-upgrades follow `releases` by default; `ERGONAUT_UPGRADE_CHANNEL=branch:main` follows main).
- [ ] Bots with bash/orca in a multi-user Ergonaut: approvals are admin-only now; consider per-plugin approver lists.
- [ ] Managed brokers without REDIS_URL fall back to in-process turn locks only.


- [ ] A bot tool waiting on a @bot_task holds a worker slot; watch worker concurrency if many tools wait at once.

### Done

- [x] Pre-commit reconciled: ruff pinned to the dev version, prettier scoped to the Ergonaut frontend with its own config, the whole repo reformatted once; every hook passes, so commits no longer skip hooks.

- [x] Telegram passes on delegated replies that land in a root chat, and approvals a delegated request is waiting on (`notify_delegations`).

- [x] Changes section on the managing bot's page: open PRs with diff, Merge (squash, then pull) and Close; the unpublished draft with Discard. Admins only.

- [x] Delegation status in the UI: a chat lists what it's waiting on / working for; sidebar dots show threads busy with delegated work.

- [x] Bot repo pulls run as the `ergonaut.pull_bot_repos` beat task (a thread only without a broker).

- [x] `test_telegram_webhook_mode`: webhook_view is csrf-exempt by attribute (Django 4.2's decorator made it sync).

- [x] Bots see images and PDFs: 📎/paste attaches files to a message (native image/document parts), `ergo_attachments_look` for files already in a session, image thumbnails in the transcript.

- [x] Bots see images directly: tools can return `ToolResult`/`ToolImage` (Claude: image blocks in the tool result; OpenAI: a user message after the tool messages), `ergo_attachments_look` returns the image itself, images are downscaled to 1024px (Pillow) and only the latest two are sent, older ones become `[image omitted: ...]`.

- [x] Kitchen bot on Ergonaut (Tandoor tools, meal-planning skill, KB), boundcorp root bot, nested bots, `ergo_bot_call`.
- [x] Official plugins in `django_ergo/plugins`: ergo_kb, bot_management (draft worktree + PR), telegram, orca, bash, kubectl, attachments.
- [x] Live bot reloads on file changes; auto-pull of the bot repo.
- [x] CTO bot (orca on devbox, bash, threads) proposed by boundcorp and merged (ergo-bots #1).
- [x] Costs page, bot pages, Memory page, Files panel; tool names prefixed `ergo_*`.
