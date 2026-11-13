# Django Ergo - Development Tasks

The bots and Ergonaut backlog. Older 2025 tasks are in [docs/archive/TODO-2025.md](docs/archive/TODO-2025.md).

## 🤖 Ergonaut bots (PR #31 merged 2026-10-02 as b1bd99c)

Live on rigel from `~/p/boundcorp/django-ergo`, bots from `boundcorp/ergo-bots`. Updated by whoever works the branch.

### Now (2026-10-02, refreshed 2026-10-03)

- [x] **PR #31 review and merge**: two review passes (22 findings) fixed in 2e54508/e3a2345; CI green; merged b1bd99c.
- [x] **Bots that learn**: `ergo_kb` `write: true` gives `ergo_kb_write` (kb/*.md only, committed and pushed). Kitchen turned on in ergo-bots.
- [ ] **Deploy plan** for Ergonaut on the octo cluster (modeled on kitchen-mgmt's infra/octo).
- [x] **Schedules in each bot's bot.yaml** (`schedules:`, run every minute by Celery beat; bot page lists them with the next run). PR from `claude/bot-schedules`.
- [x] **Auditable schedule workflows**: trusted Python steps, named JSON context, safe conditions, message-only prompts, fixed timezones, silent no-op runs and failure notices.
- [x] **Read-only model discovery**: core catalog/list checks, persisted idempotent diffs, pricing/capability provenance and private/coding policy flags; new-thread review prompts never enable routing.
- [x] **Model report snapshot safety**: compact warning references and validate the exact workflow payload before committing the catalog baseline; oversized reports roll back rather than consuming the diff.

- [x] **Toolkit pre-seeding**: toolkits declare tool calls run and written into the chat before the first completion; orchestration pre-seeds `ergo_bot_list` (names + YAML descriptions); skills use the same path; boundcorp's instructions stop listing bots.
- [x] **Turns as Celery tasks**: web messages and approvals queue a turn task (inline without a broker); Redis pub/sub wakes the SSE stream; rigel runs `up web worker beat`.
- [x] **Thread-to-thread messaging**: messages record a sender; replies go back to the sender's thread; `ergo_thread_list(bot)` and async `ergo_thread_send(bot, root|<id>|new, message)`; hop limit; replaces sync `threads_*` and `ergo_bot_call`.
- [x] **Direct parent messaging**: orchestrating chats can send and forward to their parent bot's same-user named chats and threads without a main-chat relay; bot access and stop/resolve ownership remain enforced.
- [x] **Forwarded-message identity**: message author and provenance snapshots separate the actual Django/Telegram author from the forwarding bot and original chat/time; unprefixed API/UI content, generated model attribution, and distinct mobile-safe cards. Shared-chat membership and permissions remain future work.
- [x] **System event identity**: PR checks, worker updates/results, schedules and Resume notifications wake bots as source-labeled events, not the owner; provider attribution, event cards and history/search preserve the distinction.
- [x] **Opt-in passive Telegram groups**: attributed untrusted context, sender-only authorization, explicit-tag routing, atomic message sequencing and first-thread creation; single-poller deployment boundary documented.
- [x] **Thread inactivity and archival**: beat archives threads idle `sessions.archive_after_days` (default 7); archived threads collapse in the sidebar; messaging one reopens it.
- [x] **`@bot_task` for custom tools**: run a bot-folder function on a worker and wait for or await its result.

### Next

- [x] **Native RunPod plugin slice**: real `runpod:watch` Workers and scoped
  profiles/start/status/collect/terminate tools, durable Django ownership and
  locked installation-wide reservations around the existing external rental
  ledger; independent maintenance queue/command, cancel/orphan/deadline cleanup,
  no uncertain-create replay and idempotent partial attachments. See
  [host installation and single-host storage boundary](docs/runpod.md).
- [x] **Explicit quoted H3 hardware**: host profiles bind the GPU type into
  create ownership and the pre-download CUDA/VRAM check; suitable NVIDIA
  alternatives are not blocked by an incidental exact-Ada default.
- [x] **RunPod hardware envelope and stock fallback**: profiles declare GPU
  preferences, VRAM/RAM/vCPU/driver/compute-capability minimums, regions and an
  hourly ceiling as policy data; the Worker picks the best live offer, falls back
  on proven no-capacity refusals without counting a rental, and waits for stock
  to a deadline. GPU/region/cap changes are policy edits. See
  [offer selection](docs/runpod.md#launch-time-offer-selection-and-stock-fallback).
- [x] **Offline H3 startup and native transport fixes**: partition-local model
  index resolution, runtime-delivered launcher, restart-safe host-key handling,
  fingerprint-bound fast failure, Worker-owned authenticated SSH tunnels,
  absolute log-stream read budgets and real-rental admission counts.
- [x] **Retain native H3 failure evidence**: private model stderr, bounded
  redacted failure state and timings, and immutable off-Pod diagnostic collection
  before teardown; diagnostic storage errors never prevent rental cleanup.
- [x] **Supervised H3 pilot preparation**: pinned six-shot image-conditioned
  FL2VA runner, immutable archive/prompt versions and submission tombstones,
  PCM-verified 29s export/partial manifests, independently supervised cleaner
  freshness and deadline fixtures. See [runner](docs/runpod-h3.md) and
  [cleaner](docs/runpod-cleaner.md). No deployment, model/GPU run or live rental.
- [ ] **GPU execution**: the standalone controller now supports an explicitly
  gated live one-create path, exercised only with offline HTTP fixtures. Before
  the single pilot, install the native plugin's server policy and independent
  maintenance consumer, scoped account credential injection, durable shared
  single-host rental storage, and an authenticated Pod
  access/export path; admit an actual quote with storage/margin and measure
  cold conditioned fit under the unchanged ≤1h deadline. The request binds
  the quote, image digest, datacenter allowlist, original absolute deadline and
  zero optional/network volumes. Lee confirmed permitted-country operation/use;
  the LEGAL TEAM owns review, not engineering. The ≤$3 estimate is not a
  zero-overrun guarantee gate.
  Distributed/multi-host resource storage and broader multi-user/project quota
  policies are outside the bounded single-host integration.

- [ ] **Realtime voice prototype**: basic phone audio and conversation accepted by Lee on 2026-11-12; Bluetooth/output routing and exact launch-thread orchestration context/tools are not yet accepted. Deployed baseline on Octo for Lee's account only (`REALTIME_VOICE_USERS`, kill switch `REALTIME_VOICE_ENABLED`, `voice-bridge` Deployment). Hosted boundary/transcription correctness, spoken truthfulness, interruption/reconnect, billing reconciliation and retention review remain open. Wake-word detection is deferred and not implemented. See `docs/realtime-voice-progress.md` for evidence, follow-up slices and deployment/device gates.
  PR #297 is live as `6b33f825` / `ffa206f6` through infra-octo #171. The newest actual 22:46 owner call had no normal grant: client signal and acoustic boundary reached the bridge, followed by a ValueError callback before any durable delegation. Its persisted read-only engine-limit restriction and identical offline admission rejection distinguish this from an actual native handler failure; full historical exception text was not retained. Standby remains flag-gated; its original 90-second cap includes standby.
  Lee approved normal originating-thread costs at 21:15:41 UTC. After natural old-consumer exit and the fresh acknowledged quiet window, exact policy #170 merged at 23:34:50 (`eff442d9`); web/bridge-only reload and READ ONLY/no-dispatch scope/grant checks passed at 23:38:39 on unchanged source #297. Queue-role UIDs stayed unchanged; no real-owner task/provider test ran. Only new logical sessions receive the grant; the retained 22:46 call is not upgraded. UX #299 separately groups thread options, warns that Resolve cancels workers, and moves technical detail into Diagnostics. Android browser/version and physical routing remain unknown.
  First normal-thread call, 2026-11-13 02:10 UTC (Android Chrome 157 tab): the delegated answer reached the provider session 0.7 s after the bot replied and was spoken, but the voice first denied having tools (instruction wording, now a capability statement) and Lee heard no audio although the agent's track was subscribed and played out. Playback now attaches immediately, keeps Enable sound until an element is confirmed playing, alerts when the assistant speaks unplayed, and records `client_audio`/`bridge_audio` evidence. Physical Android/Bluetooth playback is still unaccepted.
  Nov13 15:51 Android Brave input follow-up: actual remote Silero speech occurred three times, but no provider transcript/assistant audio was retained; explicit End after 34.438 seconds, not cap expiry. Lee attempted both physical microphones; browser source switching remains unverified. Draft correction observes track/publication state instead of leaving ended input enabled, adds explicit same-room recovery/local-only existing-track probe and bounded payload-free bridge input stages. No forwarding/browser root cause, physical acceptance, policy/cost change or deploy is claimed. See the latest canonical input audit.
  Nov13 all-session audit enumerates all five production sessions and three preserved paid attempts read-only, with sanitized owner/time-filtered JSON/CSV tooling. The 02:10 completed backend ledger records Claude, not the earlier Codex route; third isolated input reconciles 1,994 cache-write tokens. Invoices, LiveKit allowance and silence billing remain UNKNOWN. Independent draft315 review fixes pending-mute/recovery exclusion and late-Unmute capture teardown offline; no rollout or new paid test. See `docs/realtime-voice-audit-20261113.md`.
  Successor review additionally fixes SDK-muted explicit recovery, stale capture-error guidance after successful Unmute, and publication-ownership loss across recovery restart/unmute. Four new correctness/ownership cases failed before their fixes; capture stops on disposal/unpublication. No physical microphone/provider-root-cause acceptance is implied.

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
- [x] Native history and token-based compaction for bots; digest/chunk summaries, token-budget tool results, stable prompts, reversible policy migration, and Ergonaut Context inspection.
- [x] Leaner dispatcher chats: Context panel compares the largest request (not the turn total) with the window, older turns send relayed messages and long tool arguments cut with a pointer to the full text, a repeated `send_reply` goes once, and `chats.<name>.compaction` in bot.yaml.
- [x] Model picker from `providers.yaml` (#65, #67); recover chats left busy by a dead turn (#66); `ergonaut manage wait_idle` (#70); window/rolling compaction names (#71); stub older large tool results (#68).
- [x] Steer and stop turns another chat or worker started (#73); Unsend (#75); usage saved after every request (#76, #77); Resume button for a failed turn with plain-words error cards (#78); files shared with thread messages and `orca_upload` (#79).
- [x] Stop bot-to-bot ping-pong (#81, replaced #74): a second request to a chat with one still open is refused; reply headers tell bots not to send thanks or nudges.
- [x] Ergonaut restyled to the dark design handoff (#83).
- [x] Threads by status: the sidebar groups each bot's threads as Pinned, Waiting on you, Working, Idle and Resolved, with the bot's one-line `ChatReply.status`; a full Threads page across bots with PR and worker chips and pins (includes #98's Resolved wording).
- [x] Main dashboard UX: task-first Threads page, activity jumps, launch/pinned-file shortcuts, mobile PR/worker evidence, accessible pin controls and loading/error states; Workspace keeps recent-four/API-order defaults and links to the dashboard.
- [x] Inter-bot permissions: upward messages always allowed (`ergo_message_up` for bots without orchestration), new threads decided by the target's `threads.allow_create`, cross-bot threads record who started them, `ergo_thread_stop` and `ergo_thread_archive` for threads a bot started (`TURN_STOPPER`).
- [x] Markdown in chat messages, KB articles, skills and instructions; tool images and files a bot makes shown in the transcript as they arrive; bot.yaml `icon` and `color` in the sidebar; titles and icons on pins; thin scrollbars; long chats load the newest 50 messages with See more for older ones.
- [x] Thread cards (status, reply, PRs) for work sent to other chats and workers; collapsed replies; "Sent to" notes; thread links; PRs recorded from replies and worker results with live state from `gh`.
- [x] Ergonaut's `DJANGO_ERGO["OPEN_PRS"]` reads the recorded PRs, and thread cards use `orchestrator.thread_status`.
- [x] Claude on a subscription: `transport: cli` providers run Claude through the logged-in Claude Code CLI (`conversation/engines/claude_code.py`).
- [x] Routing by built-in and custom tier: `auto/<tier>` chats and bots, independent `tiers`/`agents` overrides in providers.yaml, subscription-only catalog defaults (small Haiku/Luna, medium Sonnet/Sol 6.1, large Opus/Sol 6, xlarge Fable/Astra, each falling back to the tier below; `low`/`high` kept as aliases). Limits use only provider-reported windows, including Claude's model-scoped Fable weekly allowance and Codex's 7-day-only snapshot; full snapshots remove obsolete counters. Ergonaut's Routing page separates API keys, shows usage freshness, picks and switches, and edits priorities. Limit refusals offer a manual Retry on an eligible alternative (never automatic).
- [x] Routing & capacity limits stay current: `bots/usage_sync.py` runs `omp usage --redact --json` every 5 minutes (Celery beat) and on the page's **Refresh limits**, stores each window with its own `observed_at`, and records sync health in `UsageSync`; the page shows 5-hour and 7-day windows per account with explicit Unavailable/Stale states.
- [x] Pace-aware shared chat/worker routing: weekly elapsed-minus-used score with expiry urgency, short-window guard, exhausted exclusions, optional soft preferences, timing-preserving parsers, and logged pick reasons. See `docs/model-routing-review.md`.
- [ ] OpenAI on a ChatGPT subscription: `transport: cli` on `openai` providers runs Codex `app-server` (`conversation/engines/codex_cli.py`); try it on a logged-in machine (`examples/codex_subscription_demo.py`), then install Codex in the Ergonaut image. (Tested on rigel 2026-10-05: works on gpt-6-sol and gpt-6-luna.)
- [ ] Ergonaut image: install the Claude Code CLI and keep its login (`CLAUDE_CONFIG_DIR` volume or `CLAUDE_CODE_OAUTH_TOKEN`); show subscription usage as such on Costs instead of list prices.
- [ ] `ergonaut manage resume_failed --kind credits` (resume every chat a credit outage failed): written in bda8305 on the closed #74 branch, never merged.
- [x] `ergo_thread_resolve` (was archive): finished threads are resolved by their orchestrator or themselves, refused while anything is open; the block marks "ready to resolve".
- [x] The block marks "ready to resolve" only on threads the viewing bot may resolve (`orchestrator.can_resolve`, shared with the tools); a parent no longer sees an actionable hint on a sub-bot's self-started thread, and the refusal names who can resolve it.
- [x] Each chat is told which chat it is ("This chat" block), and upward messages are one-way reports unless they `ask` (no acknowledgement turns).
- [x] "Bots and threads" context block for orchestrating chats: every reachable bot, its chats with their status, latest-message snippets and open PRs, capped near 3k tokens (`bots/overview.py`, `orchestrator.thread_status`, `pull_requests:`, `OPEN_PRS`).
- [x] Design to devbox handoff run live 2026-10-03: Design sent the History handoff itself, devbox picked Sonnet 5.5 from ModelsAvailable, an omp worker built it (#87 fixed omp model pinning), draft PR #88.
- [x] `introspection` built-in skill for every bot loaded from a folder: read-only `ergo_self_overview`, `ergo_self_files`, `ergo_self_read` (bot folder, and Ergo's source via `ergo:` paths).
- [x] A bot's send right after a forward (or a report) to the same thread no longer fails, and the no-nudge refusal of a second request while an earlier one is open is gone: a chat may send any number of follow-ups, a busy thread queues them in order, one turn each (each reply routed back on its own), and sends them on whenever its turn ends (also after a failed or stopped one, and promptly under Celery); `ergo_thread_send` results say `queued` and the position; optional `interrupt` replaces the chat's own running request, never the user's. Bots are told follow-ups are fine and status pings aren't.
- [x] Live Orca worker activity: worker cards show the agent's latest tool calls and output, time since its last activity with a stalled flag (`stall_minutes`), and its full recent log (`/api/sessions/<id>/workers/<id>/log`).
- [x] Agent session usage: Orca worker session files record Claude Code, Codex, and omp tokens per worker; Costs shows them separately from bot turns.
- [x] local_shell jobs record Codex usage too, while they run, from the agents' session files (transcripts as fallback) and only for the worker's own runs; `agent_usage` backfills missed workers.
- [x] Agent work by project: the Orca usage scan records each worker's repository (every worktree of one remote is one project); Costs breaks agent tokens and list-price cost down by project in a tabbed breakdown; `agent_projects` backfills older workers.
- [x] Agents as a framework concept: `AgentManager` (`bots/agents.py`) from `BotPlugin.agent_managers()`, the `agents` skill (`ergo_agent_start`, `ergo_agent_reply`, `ergo_agent_stop`), `agent:<manager>` watcher workers. Orca is the first manager; `orca_start_worker` stays as an alias.
- [x] Durable Orca follow-up instructions: idempotent versioned ledger, explicit supersession, cooperative urgent checkpoints, truthful transport receipts and separate unverified acknowledgement claims with next actions; authenticated/model acceptance remains unknown without upstream attestation. Local terminal fencing classifies unsent mail and retains active-step wake requests; no terminal injection or automatic uncertain resend.
- [ ] Browser control: the `browser` plugin drives a Chrome over CDP (`docs/browser.md`): approved sessions (`ergo_browser_start`/`_stop`, a `browser:session` worker), ssh ControlMaster per pod with `ssh_host`. `ergo_browser_batch` and `ergo_browser_task` (a fast `driver_model` drives the tools and reports back). Chrome runs on rigel in a cage window (profile `~/.config/ergo-chrome`), started by the ssh key's forced launch script; devbox has it. Next: time `ergo_browser_task` against the big model driving the tools directly.
- [ ] Agent managers for ssh (agent CLIs on a host) and local bash. Local needs a design for Celery workers on different pods: a detached process on one pod can't be polled from another.
- [ ] Switch ergo-bots instructions from `orca_start_worker` to `ergo_agent_start`, then drop the alias.
- [ ] Try `ergo_agent_start` live on devbox (after the Orca display restart).
- [ ] A visual blocks editor in the UI. Campaign names are Ad Manager's internal `AM:<uuid>:campaign` labels; friendly names would need Ad Manager's mapping.
- [x] **Agent skills**: `ergo-client` (with the `ergonaut-remote` command and `ergo_client_*` bot tools), `ergo-hosting`, `ergo-bot-development` and `ergo-developer` in the skill library, installable for Claude Code and Codex (`skill_library/install.py`); Ergonaut API keys (`Authorization: Bearer ergo_...`, API keys page, `ergonaut manage api_key`).
- [ ] Install the agent skills on the dev box, make an API key on the production server, and start delegating django-ergo and ergo-bots work through `ergonaut-remote`; feed what the runs show back into the skills.
- [x] **Bot data and pages** (decided 2026-10-02): pinned files (repo files and bot-written attachments), blocks-based page toolkit, live .jhtml Jinja pages served straight from the file/attachment endpoints, templates from bots or PRs. All users share rows; pages are sandboxed (see page actions below).
- [x] **Page actions and live refresh**: `@page_action` functions a `.jhtml` page calls as the viewer with `ergo.call` (approval round trip, `PageActionCall` record, "Page actions since your last reply" context), pages re-render when a table they read changes (`table_changed`, `touch()`, SSE stream), folder pages served sandboxed with signed asset URLs. Spec: `docs/specs/page-actions.md`; example `examples/pantry/`.
- [x] Page forms, row-edit and button blocks (`ergo.table.*`) and `ergo.ask` (parts 3 and 4 of the spec).

- [x] Upgrade path: `wait_idle` also waits for queued or running workers; `ergonaut upgrade` and `ERGONAUT_AUTO_UPGRADE_SECONDS` upgrade to a new GitHub release once idle through a pluggable `ERGONAUT_UPGRADER` (`systemd`, `command`, or a class in the bot repo); images record `ERGONAUT_VERSION`.
- [ ] Start publishing GitHub releases of django-ergo (auto-upgrades follow `releases` by default; `ERGONAUT_UPGRADE_CHANNEL=branch:main` follows main).
- [ ] Bots with bash/orca in a multi-user Ergonaut: approvals are admin-only now; consider per-plugin approver lists.
- [x] A turn paused for approval no longer outlives a stop: Stop and `ergo_thread_stop` decline its tool calls and end it, and a chat waiting on such a thread shows as Blocked with the tool calls to Approve or Reject in its banner (and "blocked" in the Bots and threads block).
- [x] Resolving a thread cancels its workers and stops their agents (the Resolve button used to leave an agent waiting for input running, which held up upgrades); the idle sweep skips threads with a running worker.
- [ ] Expire approvals left unanswered (about an hour) and give cross-thread requests a deadline the sender hears about.
- [ ] Managed brokers without REDIS_URL fall back to in-process turn locks only.


- [ ] A bot tool waiting on a @bot_task holds a worker slot; watch worker concurrency if many tools wait at once.

### Private inference (`private_pools`)

- [x] Phase 1: `private_pools` in providers.yaml (strict schema, `allowed_bots`, reserved `personal` selector), the guarded Fireworks engine with per-turn grants minted in `Bot.make_engine`, public engines refusing `fireworks.ai`, coding agents refusing private models, the `InferenceRequest` ledger with exact prices and the optional `daily_usd_cap`, protected `FIREWORKS_*` env, `private_models` in the models API and the `private` block in the Costs API. Docs: `docs/bots.md` and `docs/ergonaut.md`, "Private inference".
- [x] Model choice for tasks: the built-in `models` skill (`ergo_models_list`, scoped to the calling bot: public tiers and routing's current picks, or only its private pool) and `bots.model_choice.check`, the one rule for a model argument (`agents.start`, `ergo_browser_task(model=...)`, bot tools). A private-pool bot can't start coding agents. Next: ergo-bots' devbox `models_list` and the ads/storytime starters move onto it.
- [x] Private bots (`private: true` in bot.yaml): closed to other bots' messages, thread lists, context block, history links and files, while they still message out; browser tasks of a private-pool bot stay in the pool.
- [ ] Phase 2: a separate credential-isolated private inference service (own workload and service user, key mounted only there, signed short-lived grants, egress limited to Fireworks), so bot, tool and agent processes never see `FIREWORKS_API_KEY`.
- [ ] Private coding agents: omp/Orca harnesses would have to carry and enforce a grant (subagents, defaults, model switches). Until then agents refuse private models.
- [ ] Frontend: a separate, API-billed group for `private_models` in the model picker, and the Costs page's `private` block.
- [ ] ergo-bots: the `personal` bot and a disabled `private_pools.personal` in providers.yaml; provision `FIREWORKS_API_KEY` as a Secret in infra-octo (`kubernetes/apps/ergonaut/app/deployment.yaml`); then enable.
- [ ] Lee decides: the `daily_usd_cap` value (and whether a UTC day is the right period), the default member (`deepseek-v4p1-flash-1017` or the original `deepseek-v4p1-flash`), whether titles, compaction and illustrations of the personal bot's chats should spend on the pool (they do now), and whether its voice notes may go to the deployment's public `AUDIO_TRANSCRIBER` (they do now, when one is set).

### Functional review (2026-10-30)

The review is in the project files (`reviews/functional-review/`). Done in one PR:

- [x] Security: kubectl flags and env-style redaction, a worker step lease, merge_main conflicts, bot-folder dotfiles and `.html`/`.svg` CSP, single-use page-action approvals, `ergo_kb` singular key and owner.
- [x] Deleted the legacy layer: workflow engine, global tool registry, `kb_*` toolkits and pipelines, history toolkit, dead helpers, Ergonaut leftovers. One tool dispatch (`conversation/tool_dispatch.py`).
- [x] Import-layer test; engines record usage windows without importing bots.
- [x] Private bots come from `private_pools[*].allowed_bots`, not the name `personal`.
- [x] One roles module, one root-only plugin gate, one liveness rule, `plugins/_common.py`, `daemon.start_daemon`.
- [x] Python >=3.11, one dependency source, frontend tests in CI, sign-out only on 401, SSE ids on the session stream, `usePersistedFlag`.

Still to do:

- [ ] Drop the `OpenAIMessage` table (a release after migration 0030) and decide on the legacy `Workflow`/`UserChat`/`ChatMessage` tables (a deployment decision).
- [ ] A shared CLI-engine subprocess base for `claude_code.py` and `codex_cli.py` (~150 duplicated lines).
- [ ] One git-checkout helper with an explicit pull policy (`reloading.pull_checkout` vs `folder_kb`).
- [ ] Frontend `format.ts`/`status.ts` (status maps, the two `countdown`s, window labels) and `upload` folded into `request`.
- [ ] `ergonaut/api/costs.py` should reuse `Price.parts` and the subscription exclusion.
- [ ] Mechanical splits: `conversation/models.py`, `ergonaut/api/bots.py`, `apps/bots/tasks.py`, `Chat.tsx` (`useSessionStream`, `usePendingTurn`).
- [ ] Second-tier review items: orca `TRACKED_ONLY` with a leading flag, `chat_session` get-or-create race, atomic metadata updates, bash timeouts killing children, bounded approval retries, `StructuredCallSpec.max_tokens` with a passed engine, emoji/`null` in page blocks, the empty OpenAI replay tier.

### Done

- [x] Reviewed bash approval: `approve: review` lets a reviewer model (routed, tool-less) pass plain read-only commands the chat owner asked for in the current turn (signed-in browser messages or verified forwards only), fails closed on any doubt, checks for a message or stop right before the command starts, and records every decision in `bash_reviews`. Accepted residual risks (Lee, 2026-11-13): best-effort secret redaction of the messages sent to the reviewer, and path-based approval with no symlink binding.

- [x] Self-wakes in every chat and thread: the built-in `wake` skill (`ergo_wake_later`, `ergo_wake_cancel`) schedules one later turn with a note, fired once from beat as an automated event (never retried), capped at one pending per chat, 1 minute to 24 hours ahead and 3 in a row without a human message; shown above the composer and in the admin.

- [x] Todo lists in every chat and thread: the built-in `todos` skill (`ergo_todo_write`, `ergo_todo_update`, like Claude Code's TodoWrite) keeps a checklist in the session's metadata, in context every turn, shown in Ergonaut's thread view; `todos: false` in bot.yaml turns it off.

- [x] Pre-commit reconciled: ruff pinned to the dev version, prettier scoped to the Ergonaut frontend with its own config, the whole repo reformatted once; every hook passes, so commits no longer skip hooks.

- [x] Telegram shared thread: `target_thread` puts every chat in one thread, `respond_to` picks the people and messages answered, `replies: tool` sends with `ergo_telegram_message`, `AuthorProfile` keeps each Telegram user's name and photo (shown in Ergonaut), photos sent one at a time batch (`photo_batch_wait`), and turns hold Ergonaut's session lock (`CHANNEL_TURN_RUNNER`).

- [x] Telegram approval intake remains responsive during resumes: per-chat FIFO workers retain durable album/photo-batch sources until transactional, lock-scoped execution; nonblocking host-lock waits leave other chats/bots available, interrupted claims are surfaced without tool replay, and retirement errors cannot withhold an executed turn's result.

- [x] Telegram: `ergo_telegram_file` sends saved session images, videos and documents with multipart uploads.
- [x] Telegram: sends report their message id and `ergo_telegram_delete` deletes the bot's own messages (by id, or its latest).

- [x] Telegram passes on delegated replies that land in a root chat, and approvals a delegated request is waiting on (`notify_delegations`).

- [x] Changes section on the managing bot's page: open PRs with diff, Merge (squash, then pull) and Close; the unpublished draft with Discard. Admins only.

- [x] Delegation status in the UI: a chat lists what it's waiting on / working for; sidebar dots show threads busy with delegated work.

- [x] Bot repo pulls run as the `ergonaut.pull_bot_repos` beat task (a thread only without a broker).

- [x] `test_telegram_webhook_mode`: webhook_view is csrf-exempt by attribute (Django 4.2's decorator made it sync).

- [x] Bots see images and PDFs: 📎/paste attaches files to a message (native image/document parts), `ergo_attachments_look` for files already in a session, image thumbnails in the transcript.

- [x] Bots see images directly: tools can return `ToolResult`/`ToolImage` (Claude: image blocks in the tool result; OpenAI: a user message after the tool messages), `ergo_attachments_look` returns the image itself, images are downscaled to 1024px (Pillow) and only the latest two are sent, older ones become `[image omitted: ...]`.

- [x] A bot's round of tool calls shows all its images: the latest-two window no longer drops images the bot just asked for (a batch of four `ergo_attachments_look` calls came back as two images and two `[image omitted]` placeholders); up to 8 images from the newest round are sent, older ones still drop out.
- [x] Bots watch videos: `ergo_attachments_look` sends a video to Gemini (Files API upload, optional start/end), `ergo_attachments_frames` takes low-memory ffmpeg stills the bot sees itself.

- [x] Kitchen bot on Ergonaut (Tandoor tools, meal-planning skill, KB), boundcorp root bot, nested bots, `ergo_bot_call`.
- [x] Official plugins in `django_ergo/plugins`: ergo_kb, bot_management (draft worktree + PR), telegram, orca, bash, kubectl, attachments.
- [x] bot_management follows its own PRs: `ergo_config_repo_pr_status` (checks, failed logs, reviews), `ergo_config_repo_checkout_pr` + publish pushes fixes to the same PR, `ergo_config_repo_close_pr`, and `watch_checks` wakes the chat when its PR's checks pass or fail.
- [x] Live bot reloads on file changes; auto-pull of the bot repo.
- [x] CTO bot (orca on devbox, bash, threads) proposed by boundcorp and merged (ergo-bots #1).
- [x] Costs page, bot pages, Memory page, Files panel; tool names prefixed `ergo_*`.
