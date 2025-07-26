# Django Ergo - Active Questions & Decisions

## 🚨 ACTIVE QUESTIONS (Need Decisions)

### User Experience & Features  
- **Q**: Should we include a visual workflow builder, or is code-based configuration sufficient for v1.0?
- **Q**: How should users interact with knowledge bases - chat interfaces, traditional search, or both?
- **Q**: Should example apps be separate Django projects or integrated into main package?

### Deployment & Distribution
- **Q**: Should Django Ergo be distributed as PyPI package, Docker images, or both?
- **Q**: Do we need a hosted SaaS version, or focus on self-hosted deployments initially?
- **Q**: What deployment documentation is needed - Docker Compose, Kubernetes, cloud guides?

## 📋 RESOLVED DECISIONS ✅

### Architecture & Core Design ✅
- **DECIDED**: **MCP Integration** - Ergo provides reusable tools for Django apps to build their own MCP servers
- **DECIDED**: **Embeddings** - Pluggable system with OpenAI default, settings-based switching, custom embeddings support  
- **DECIDED**: **Workflows** - Python-based with OpenAI agent context serialization and tool approval system
- **DECIDED**: **Knowledge Bases** - Easy flatfile export/import for agentic processing, no versioning initially
- **DECIDED**: **Permissions** - Managed by apps, not framework - apps decide user access patterns

### Development Approach ✅
- **DECIDED**: **Framework Strategy** - Building blocks for Django developers, not complete platform
- **DECIDED**: **Tool System** - "Approved tools" vs "ask tools" with approval flow and app whitelisting
- **DECIDED**: **Agent Architecture** - Self-contained workflow engine without external dependencies
- **DECIDED**: **Multi-tenancy** - Owner-based knowledge bases for scalability

### Technical Implementation ✅
- **DECIDED**: **Database** - PostgreSQL only, require pgvector for embedding functionality
- **DECIDED**: **Embedding Storage** - Save embeddings in database fields (not external storage)
- **DECIDED**: **LLM Provider** - OpenAI only for v1.0 (can add others later)

## 🔬 RESEARCH NEEDED

### Performance & Scalability
- **Research**: What are realistic performance targets for different deployment sizes?
- **Research**: Should we implement horizontal scaling from start or optimize for single-server?
- **Research**: How to benchmark and maintain performance as codebase grows?

### Security Considerations
- **Research**: How to implement proper sandboxing for custom tools?
- **Research**: What data encryption requirements should we support?
- **Research**: How to handle compliance (GDPR, HIPAA, SOC 2)?

### Community & Ecosystem
- **Research**: Which Django packages should we prioritize integration with?
- **Research**: What governance model works best for this type of project?
- **Research**: How to build sustainable community around the project?

## 📈 FUTURE CONSIDERATIONS

### Advanced Features (Post v1.0)
- Multi-modal AI capabilities (vision, audio)
- Multiple AI agents working together
- Reinforcement learning from user feedback
- Advanced analytics and recommendations

### Enterprise Features
- Single Sign-On (SSO) integration
- Advanced user management and roles
- Compliance and audit features
- Multi-tenant architecture improvements

---

## Decision Process Guidelines

### For Active Questions
1. **Research** existing solutions and best practices
2. **Prototype** complex decisions with proof-of-concepts  
3. **Gather feedback** from potential users
4. **Document** decisions and rationale
5. **Plan for evolution** - avoid painting into corners

### Adding New Questions
- Check if it's already resolved in the archive above
- Be specific about what decision is needed
- Include context about why the decision matters
- Tag with priority: 🚨 Active, 🔬 Research, 📈 Future

### Regular Review
- Monthly review during development
- Archive resolved questions
- Update priorities based on current phase
- Keep stakeholders informed of major decisions

---

*This document focuses on questions that actively need decisions. Historical questions and extensive analysis have been moved to archive sections to maintain clarity.*