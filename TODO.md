# Django Ergo - Development Tasks

## 🤖 Ergonaut bots (PR #31, branch `claude/project-thread-epsqtb`)

Live on rigel from `~/p/boundcorp/django-ergo`, bots from `boundcorp/ergo-bots`. Updated by whoever works the branch.

### Now (2026-10-02)

- [x] **Toolkit pre-seeding**: toolkits declare tool calls run and written into the chat before the first completion; orchestration pre-seeds `ergo_bot_list` (names + YAML descriptions); skills use the same path; boundcorp's instructions stop listing bots.
- [ ] **Turns as Celery tasks**: web messages and approvals queue a turn task (inline without a broker); Redis pub/sub wakes the SSE stream; rigel runs `up web worker beat`.
- [ ] **Thread-to-thread messaging**: messages record a sender; replies go back to the sender's thread; `ergo_thread_list(bot)` and async `ergo_thread_send(bot, root|<id>|new, message)`; hop limit; replaces sync `threads_*` and `ergo_bot_call`.
- [ ] **Thread inactivity and archival**: beat archives threads idle `sessions.archive_after_days` (default 7); archived threads collapse in the sidebar; messaging one reopens it.
- [ ] **`@bot_task` for custom tools**: run a bot-folder function on a worker and wait for or await its result.

### Next

- [ ] Images and PDFs from the Files panel shown to the model, not just described.
- [ ] "Changes" tab on the bot page: open ergo-bots PRs with diff, Merge and Discard.
- [ ] Replace the 30s git-pull thread with a beat task.
- [ ] Reconcile the root pre-commit config (ruff, prettier) with the branch's code style.
- [ ] `test_telegram_webhook_mode` fails locally (async view under the sync test client); check CI.

### Done

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
