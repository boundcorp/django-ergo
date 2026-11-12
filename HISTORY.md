# Django Ergo - Development History

This document tracks the major milestones and evolution of the Django Ergo project.

## Development Timeline

### Realtime voice dispatch recovery (November 2026)

- The isolated voice bridge pins the Redis broker transport and probes Celery
  enqueue/consume offline. A failed voice dispatch is recorded as not run
  and delivered through the voice result outbox, without an automatic retry.

### Opt-in hard budgets for a turn (November 2026)

- A thread message can carry a budget (model turns, input and output tokens,
  a deadline, allowed tools) that the turn runner enforces server-side: no
  retries, compaction, routing, approvals, worker or agent starts or thread
  sends, and a limit ends the turn as a truthful `failed` result. Turns
  without a budget are unchanged. The realtime voice prototype's delegation
  uses it in place of the unbounded queue.

### Direct messaging to parent chats (November 2026)

- Chats with orchestration enabled may send or forward directly to any
  same-user chat or thread of an authorized bot, including their parent.
  Bot access checks, disabled orchestration, and stop/resolve ownership remain.

### Opt-in passive Telegram group context (November 2026)

- Selected groups retain attributed, untrusted context and downloaded media
  without starting turns or transcribing passive audio. Sender-only authorization
  and explicit `tags` routing keep partner messages and untagged replies passive;
  existing defaults stay unchanged.
- Telegram can send saved session attachments back as photos, videos or documents.
- Active and passive message writes share a short database row lock for sequence
  allocation. Concurrent Telegram thread creation rechecks under an owner-row
  lock. The host's turn runner remains exclusive to active turns and approvals.
- Approval callbacks honor the configured sender allowlist. Process-local intake
  FIFO/deduplication still requires one polling consumer, not concurrent webhook
  workers, for the intended Storytime deployment.

### Model discovery seeds a baseline and fails closed for coding (November 2026)

- A first catalog check persists its baseline and returns a one-time
  `Baseline seeded, N models` summary instead of listing the existing catalog
  as newly released models. Subsequent checks report actual differences;
  existing baseline scopes and snapshots are preserved.
- Coding review flags require explicit chat-kind metadata. Non-chat and
  missing-kind models remain in discovery but cannot become coding candidates;
  task tokens such as `realtime` also reject upstream chat misclassifications.
  Pricing, private-pool and provider restrictions remain in force.

### Worker results reach the chat that asked (November 2026)

- A thread started by another chat reports what it made of a finished
  worker's result back to that chat. Before, its "I've started a worker"
  answer closed the request and the finished work stayed in the thread.

### Agent replies check the question id (November 2026)

- `ergo_agent_reply` refuses a question id the agent never asked and names
  the open one, instead of queuing an answer that fails later out of sight.
  The watcher saves a question as asked before telling the chat about it.
- A turn left waiting for approval no longer keeps the chat busy once the
  user has written again (it shows no card and can't be answered). Before,
  it held every queued thread message and worker report indefinitely.

### Local jobs survive upgrades and get kit fixes (November 2026)

- The restart gate (`wait_idle`, the upgrader) waits for `local_shell` jobs
  whose agent is running, even when it ignores other workers: those runs die
  with their pod.
- A job's unchanged template files are updated to the template's current
  version before each run (`.ergo/template.json`); edited files stay.

### Local job secrets and kit refresh (November 2026)

- `local_shell` reports listed secrets that aren't set where a run launches
  (in `ergo_agent_start`'s `warnings` and the reply result), and copies template
  files a job folder lacks before each run, without overwriting the job's own.

### Nonblocking agent replies (November 2026)

- Agent answers are queued for the polling worker instead of holding the bot's
  turn open. Duplicate answers are rejected and delivery errors are reported
  back to the chat.

### Telegram approval intake and recovery (November 2026)

- Callback acknowledgement follows durable acceptance rather than waiting
  for a running turn. Per-chat FIFO workers share a two-worker limit across
  Telegram plugins on each event loop, keeping executor capacity available
  for polling, acknowledgement and turn-lock release.
- Updates queued or waiting for the host's turn lock remain pending and
  recover on startup. Prompt-retirement failures after tools execute are
  logged without failing the completed turn or suppressing its reply.
- Authorization, atomic approval execution and prompt retirement remain
  unchanged; no migration or configuration change is required.


### GPT-Transcribe defaults (November 2026)

- Composer voice and attachment transcription now default to `gpt-transcribe`.
  Voice costs use its audio-duration price, estimating from the measured recording
  when provider usage is unavailable.

### Voice input in chat (November 2026)

- Existing chat composers can record audio for an editable draft, without
  sending it automatically. The optional session-scoped transcription API
  validates and limits uploads and uses the deployment's OpenAI credential.
  Audio is sent to OpenAI, including for private-pool chats; voice costs appear
  in the ledger. See [Ergonaut](docs/ergonaut.md#voice-input).

### System event messages (November 2026)

- PR-check notifications, worker updates/results, scheduled prompts and
  Ergonaut's Resume notifications have system source identities rather than
  impersonating the chat owner. `messaging.send_event` uses the existing durable
  queue and wakes the bot normally; bot-to-bot reports and real human messages
  retain their original attribution.
- Provider input explicitly labels events; chat cards and history reads/search
  show their event source. Existing author/provenance JSON fields hold the
  distinction, so no schema migration is required.

### Private inference pools (October 2026)

- `private_pools` in providers.yaml lists API-billed models only the bots in
  its `allowed_bots` may use, selected as `personal` or `personal/<alias>`.
  The first provider is Fireworks: a guarded engine with a fixed endpoint,
  `FIREWORKS_API_KEY` read at send time, SDK retries off, and a grant minted
  per turn in `Bot.make_engine` and checked before every HTTP attempt.
  Anything else fails closed with a `PolicyError` (`error_category`
  `policy`, never retried, no public fallback); public engines, embeddings
  and transcription refuse `fireworks.ai`, and coding agents refuse private
  models.
- Each attempt is an `InferenceRequest` row priced exactly by provider and
  model ID (unknown, never 0, without a price or usage). An optional
  `daily_usd_cap` reserves the worst case before sending. The Costs API has
  a `private` block and the models API a `private_models` list.
- `FIREWORKS_*` (and `DJANGO_ERGO["PROTECTED_ENV_PREFIXES"]`) are kept out
  of subprocesses, `ctx.secret()` and local shell secrets: defense in depth,
  not isolation. A credential-isolated service is the planned next phase.
- The OpenAI extra installs `httpx` and restricts the SDK to versions before 3,
  whose HTTP transport interface is used by the guarded Fireworks engine.

### Small, medium, large and xlarge tiers (October 2026)

- The built-in routing tiers are now `small` (Haiku 5.5, GPT-6 Luna),
  `medium` (Sonnet 5.5, GPT-6.1 Sol), `large` (Opus 5.5, GPT-6 Sol) and the
  new `xlarge` (Fable 5.1, GPT-6 Astra), each falling back to the pair below
  it. Effort is no longer tied to the tier: everything runs at medium unless
  a chat picks another level with the effort slider in its thread options
  (`POST /api/sessions/<id>/effort`). `low` and `high`
  stay as aliases of `small` and `large`, so existing `auto/low` chats and
  providers.yaml keys keep working. Haiku 5.5 pricing is added.

### Routing & capacity limits that stay current (October 2026)

- Subscription limits were only recorded as a side effect of a Claude or Codex
  CLI turn, and a partial event refreshed the row's timestamp while leaving
  old windows in place, so idle providers showed stale 7-day values and no
  Claude 5-hour window. `bots/usage_sync.py` now runs `omp usage --redact
  --json` every 5 minutes (and on **Refresh limits**), stores each window with
  its own `observed_at`, and records the sync's health in `UsageSync`.
- The Routing page is redesigned as Routing & capacity: sync health, a card per
  account with 5-hour and 7-day windows, rate ticks showing the elapsed share
  of each window, and explicit Unavailable/Stale states.

### Provider-driven routing (October 2026)

- Routing uses only reported subscription windows, preserving unknown utilization,
  reset times and status; Claude's Fable weekly allowance only gates Fable models.
- The Routing page separates pay-per-token providers from subscriptions and shows
  usage freshness instead of inventing 5-hour/weekly counters.
- Built-in low/medium/high tiers use configured current-catalog subscription
  models, with Fable/Astra high-capability defaults and medium-model fallbacks.
  `providers.yaml` supports custom names and independent per-tier overrides.

### Initial Setup (December 2024)

- **8283455** - Initial commit
- **dd0b5a8** - Cursor Dockerfile development environment setup
- **addf06c** - PostgreSQL configuration and test infrastructure
- **0aea1b9** - Cursor AI development standards and rules

### Foundation Phase (December 2024 - January 2025)

- **b5589a2** - Added `old-code-inspiration/` with prototype models and workflow engine
- **4ad3c4d** - Created comprehensive planning documentation:
  - Architecture analysis of prototype
  - 24-week development roadmap
  - Detailed TODO lists and questions
  - Product specifications for example apps

### Architecture & Planning (January 2025)

- **1a6d8f7** - Refined product documentation for garden management and personal goals apps
- **25cab62** - Finalized v1.0 architectural decisions:
  - MCP integration strategy (reusable tools for Django apps)
  - Pluggable embedding system with OpenAI default
  - Workflow engine with context serialization
  - App-level permissions model

### Core Development (January 2025)

- **b463f22** - Implemented core Django Ergo models and functionality:
  - Migrated models from prototype (Workflow, Knowledgebase, Article, UserChat, ChatMessage)
  - Built tool system with 6 knowledge base tools
  - Created workflow engine with state persistence
  - Enhanced Django admin interface
  - Sample data management command

### Infrastructure & Testing (January 2025)

- **caf4fa6** - Improved startup scripts with logging and debugging
- **940eee3** - Implemented dual-tier OpenAI testing with fixtures:
  - Test mode for development without API costs
  - Production mode with real OpenAI integration
  - Comprehensive test coverage for tool system

## Current Status (v0.1.0-dev)

### ✅ Completed

- **Core Models**: All prototype models migrated and enhanced
- **Tool System**: 6 knowledge base tools with registry and validation
- **Workflow Engine**: Basic processing with state persistence and approval system
- **Admin Interface**: Full Django admin integration
- **Development Environment**: Complete Cursor setup with all dependencies
- **Testing Infrastructure**: Comprehensive test suite with fixtures

### 🚧 In Progress

- **PostgreSQL + pgvector**: Production database setup
- **OpenAI Integration**: Real API integration (currently in test mode)
- **Public APIs**: REST/GraphQL API design and implementation

### 🎯 Next Milestones

- **v0.2.0**: Production-ready embeddings and search
- **v0.3.0**: Public APIs and authentication
- **v1.0.0**: Complete MCP integration and example applications

## Architecture Evolution

### Phase 1: Prototype Analysis (Complete)

Analyzed existing prototype to understand:

- Multi-tenant knowledge base architecture
- Hybrid search with PostgreSQL + pgvector
- Extensible tool system with function decorators
- Workflow engine with OpenAI integration

### Phase 2: Foundation Building (Complete)

Migrated and enhanced core components:

- Django models with proper migrations
- Tool registry with automatic discovery
- Admin interface with custom views
- Development and testing infrastructure

### Phase 3: Production Features (Current)

Building towards production readiness:

- Real embedding generation and vector search
- OpenAI API integration with proper error handling
- Public APIs for external access
- Performance optimization and caching

## Key Decisions Made

1. **MCP Strategy**: Focus on reusable tools for Django apps rather than monolithic platform
2. **Embedding System**: Pluggable providers with OpenAI default and custom embedding support
3. **Workflows**: Python-based with OpenAI context serialization for pause/resume
4. **Permissions**: App-level rather than framework-level for maximum flexibility
5. **Knowledge Management**: Flatfile export/import for agentic processing

## Documentation Evolution

### Initial Documentation Burst

Created comprehensive planning documents:

- Architecture analysis and decisions
- 24-week roadmap with phases
- Detailed TODO lists (299 items)
- 60+ questions for stakeholder input
- Product specifications for example apps

### Documentation Consolidation (Current)

Streamlining documentation for maintainability:

- Consolidated redundant summary documents
- Cleaned up resolved questions
- Updated history with actual project evolution
- Focused on actively used planning documents

## Contributors

- **leeward bound** - Project lead and primary developer
- **Cursor Background Agents** - Architecture, planning, and implementation assistance

## Version History

### v0.0.1 (Initial)

- Empty Django project structure

### v0.1.0-dev (Current)

- Complete core model implementation
- Tool system with 6 knowledge base tools
- Workflow engine with state persistence
- Django admin integration
- Comprehensive test suite
- Development environment setup

---

_This history is maintained as a living document and updated with each major milestone._
