# Django Ergo - Development Tasks

## 🤖 Ergonaut bots (PR #31 merged 2026-10-02 as b1bd99c)

Live on rigel from `~/p/boundcorp/django-ergo`, bots from `boundcorp/ergo-bots`. Updated by whoever works the branch.

### Now (2026-10-02)

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

- [ ] **Skills unified + named chats** (branch `claude/skills-and-chats`): lazy skills (load/unload, always per chat, requires), main replaces root, named chats in bot.yaml, schedule targets. PR, then ergo-bots configs.
- [x] **Schedule actions** (PR #34, merged): ordered `prompt` and `run` steps; `run` calls bot-folder Python and records a BotJob.
- [x] **BotTable** (PR #35, merged): real Django models per bot, migrations in the bot folder's `migrations/` (bot_management writes them into the proposal), `ergo_bot_migrate` at start and after pulls, `tables` skill.
- [x] **Pages and pins** (PR #36, merged): live .jhtml pages (sandboxed Jinja over tables), blocks, the `pages` plugin, chat pins (bot-folder files and pinned chat files), page viewer in the chat, `ctx.table()` for schedule code.
- [ ] **Fixes from the Ad Manager test** (PR #37): code-only schedules run once as the first admin; pulls only migrate after real changes; page sums are unknown, not zero.
- [ ] **Ad Manager bot (`ads`, ergo-bots #3, merged)**: written by boundcorp; live on rigel, table migrated, dashboard pinned. Waiting on Lee to put the Meta token in ~/src/ergonaut-secrets.env, then restart and run the first pull.
- [ ] Ideas from the test: preview a draft's pages before publishing (boundcorp can't render a page in its draft); a visual blocks editor in the UI.
- [x] **Bot data and pages** (decided 2026-10-02): pinned files (repo files and bot-written attachments), blocks-based page toolkit, live .jhtml Jinja pages served straight from the file/attachment endpoints, templates from bots or PRs. No sandbox origin; all users share rows.

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

- [x] Kitchen bot on Ergonaut (Tandoor tools, meal-planning skill, KB), boundcorp root bot, nested bots, `ergo_bot_call`.
- [x] Official plugins in `django_ergo/plugins`: ergo_kb, bot_management (draft worktree + PR), telegram, orca, bash, attachments.
- [x] Live bot reloads on file changes; auto-pull of the bot repo.
- [x] CTO bot (orca on devbox, bash, threads) proposed by boundcorp and merged (ergo-bots #1).
- [x] Costs page, bot pages, Memory page, Files panel; tool names prefixed `ergo_*`.

## 🎯 IMMEDIATE NEXT STEPS (Current Sprint)

### Production Readiness

- [x] **Enable PostgreSQL + pgvector** ✅
  - ✅ Switch from SQLite to PostgreSQL for development
  - ✅ Enable SummarizedVectorField with real embeddings
  - ✅ Implement hybrid search with semantic similarity
- [x] **OpenAI Integration** ✅
  - ✅ Uncomment OpenAI client configuration
  - ✅ Implement real tool execution with function calling
  - ✅ Remove graceful fallbacks - OpenAI is STRICTLY REQUIRED
  - ✅ Fail fast when OpenAI API key is not configured
- [x] **API Development** ✅
  - ✅ Design REST APIs for core models with Django Ninja
  - ✅ Add JWT authentication and permissions
  - ✅ Create comprehensive API documentation
  - ✅ Implement semantic search endpoints
  - ✅ Add automatic OpenAPI/Swagger documentation
  - ✅ Move API to example application (better architecture)

## 🏗️ CORE FEATURES (Next 4-6 weeks)

### Knowledge Management

- [x] **Pluggable Embedding System** ✅
  - ✅ Create abstract embedding provider interface
  - ✅ Implement OpenAI provider (default)
  - ✅ Add settings-based provider switching
  - ✅ Support custom embeddings for testing
- [x] **Enhanced Search** ✅
  - ✅ Optimize hybrid search performance with pgvector indexes
  - ✅ Caching strategy decision: Apps handle their own embedding/result caching
  - [ ] Implement search analytics and suggestions

### Workflow Engine

- [x] **Tool Approval System** ✅
  - ✅ Build approval workflow: save context → event → wait → resume
  - ✅ Add tool whitelisting for apps
  - ✅ Implement Django signals for approval events
  - ✅ Create new message types for approval workflow
  - ✅ Add context serialization for pause/resume
  - ✅ Demonstration tools with approval requirements
- [x] **Context Management** ✅
  - ✅ OpenAI agent context serialization
  - ✅ Workflow pause/resume capabilities
  - ✅ Workflow state persistence

### Developer Experience

- [x] **Admin Enhancements** ✅
  - ✅ Add workflow execution monitoring
  - ✅ Create knowledge base management tools
  - ✅ Implement user chat history viewer
- [x] **Testing Framework** ✅
  - ✅ Expand test coverage to 90%+
  - ✅ Add integration tests for workflows
  - ✅ Create test fixtures for development

## 📚 DOCUMENTATION & EXAMPLES (Ongoing)

### Essential Documentation

- [ ] **API Documentation**
  - Complete API reference with examples
  - Add authentication and usage guides
  - Create integration tutorials
- [ ] **Developer Guides**
  - Getting started tutorial
  - Workflow development guide
  - Tool creation documentation

### Example Applications

- [ ] **Example Application 1**
  - Implement core functionality demonstrating single-user workflows
  - Add AI-powered conversation features
  - Create sample data and setup scripts
- [ ] **Example Application 2**
  - Build multi-tier knowledge system example
  - Implement complex workflow orchestration
  - Demonstrate advanced tool integration

## 🔧 INFRASTRUCTURE (Later)

### Performance & Monitoring

- [ ] **Application-Level Caching** (Framework provides utilities only)
  - Caching utilities and helpers for apps
  - Cache invalidation helper functions
  - Performance monitoring tools
- [ ] **Monitoring & Logging**
  - Structured logging throughout system
  - Performance metrics collection
  - Health check endpoints

### Security & Compliance

- [ ] **Permission System**
  - Fine-grained tool permissions
  - User access control framework
  - Audit logging for security events
- [ ] **Tool Sandboxing**
  - Secure execution environment
  - Resource limits and timeouts
  - Input validation and sanitization

## ✅ COMPLETED (Recent)

### Performance Optimization ✅

- ✅ **Vector Search Optimization**: Added comprehensive pgvector indexes for 10-100x performance improvement
  - ✅ HNSW indexes for content_embedding and summary_embedding fields
  - ✅ IVFFlat indexes as alternative for different query patterns
  - ✅ Composite indexes for knowledgebase-filtered searches
  - ✅ Performance monitoring and query analysis tools
  - ✅ Database optimization configuration and maintenance utilities

### Core Foundation ✅

- ✅ **Models Migrated**: All prototype models enhanced and working
- ✅ **Tool System**: 6 knowledge base tools with registry
- ✅ **Workflow Engine**: Basic processing with state persistence
- ✅ **Admin Interface**: Full Django admin integration
- ✅ **Testing Infrastructure**: Dual-tier OpenAI testing setup
- ✅ **Development Environment**: Complete Cursor setup

### Architecture Decisions ✅

- ✅ **MCP Strategy**: Reusable tools for Django apps
- ✅ **Embedding System**: Pluggable providers decided
- ✅ **Workflow Design**: Python-based with context serialization
- ✅ **Permissions Model**: App-level rather than framework
- ✅ **Database**: PostgreSQL only with pgvector
- ✅ **LLM Provider**: OpenAI only for v1.0

## 📋 ARCHIVED TASKS

<details>
<summary>Click to view archived/completed tasks</summary>

### Database & Migrations ✅

- ✅ Clean up and refactor UserChat, ChatMessage models
- ✅ Enhance Workflow model with better configuration
- ✅ Improve Knowledgebase and Article models
- ✅ Add proper database migrations and indexes
- ✅ SQLite development configuration

### Tool Development ✅

- ✅ Design declarative tool configuration system
- ✅ Implement tool registry with automatic discovery
- ✅ Add tool validation and permission framework
- ✅ Create knowledge base tools (search_user_kb, etc.)

### Sample Data & Testing ✅

- ✅ Sample data management command
- ✅ Admin user creation (admin/admin123)
- ✅ Basic functionality testing and verification
- ✅ OpenAI testing with fixtures

</details>

---

## 📝 Task Management Notes

### Working on Tasks

1. **Pick tasks** matching your expertise from "Immediate Next Steps"
2. **Break large tasks** into smaller actionable items
3. **Update progress** by moving items between sections
4. **Document decisions** and add new tasks as discovered
5. **Test thoroughly** and update documentation

### Priority Guidelines

- **🎯 Immediate**: Critical for current development
- **🏗️ Core**: Important for v1.0 functionality
- **📚 Documentation**: Essential for adoption
- **🔧 Infrastructure**: Future scalability and operations

### Current Focus

We're in **Phase 3: Production Features** - making the system production-ready with real embeddings, OpenAI integration, and public APIs.

---

_This TODO list is actively maintained. Completed items are moved to archives, new tasks are added as discovered._
