# Django Ergo v1.0 - Key Architectural Decisions

## Overview

This document captures the key architectural decisions made for Django Ergo v1.0 based on stakeholder input and analysis of the prototype. These decisions resolve many of the open questions and provide clear direction for development.

## Core Architecture Decisions

### 1. MCP Integration Strategy ✅ DECIDED

**Decision**: Ergo provides reusable tools for Django app authors to build their own MCP servers

**Details**:

- Focus on utilities like `search_user_kb`, `search_grower_kb`, `search_garden_kb`
- Export capabilities as REST endpoints
- Apps handle authentication and permissions
- Framework provides MCP server building blocks, apps compose their own servers

**Rationale**: This approach gives maximum flexibility to Django developers while providing powerful, reusable components. Apps can decide which knowledgebases to expose and how to secure them.

### 2. Pluggable Embedding System ✅ DECIDED

**Decision**: Embeddings are fully pluggable with multiple provider support

**Details**:

- Abstract embedding provider interface
- OpenAI embeddings as default provider
- Settings-based provider switching (`ERGO_EMBEDDING_PROVIDER = 'openai'`)
- Support for custom embeddings (like loading test fixtures)
- Both on-demand and background task embedding generation

**Rationale**: Different use cases have different needs - some want local models for privacy, others want hosted solutions for convenience. This system accommodates all approaches.

### 3. Workflow Engine Architecture ✅ DECIDED

**Decision**: Python-based workflows with OpenAI agent context serialization and tool approval system

**Details**:

- Workflows defined in Python code (not YAML or visual builders)
- Serialize OpenAI agent context to ChatMessage for pause/resume capability
- Tool approval system: "approved tools" vs "ask tools" (default: ask)
- Approval flow: save context → fire generic event → wait for user approval → resume
- Apps can whitelist specific tools to skip approval

**Rationale**: Python workflows provide maximum flexibility. Context serialization enables sophisticated pause/resume workflows. Tool approval system provides safety while allowing apps to optimize for their use cases.

### 4. Knowledge Base Management ✅ DECIDED

**Decision**: Easy-to-manage KBs designed for agentic processing

**Details**:

- Flatfile export/import capabilities for agentic processing
- Utilities for create/update/diff/build operations
- No versioning system initially (avoid complexity)
- Focus on tools that agents can use to manage and reorganize KBs

**Rationale**: The primary use case involves AI agents managing knowledge bases. Making them easy to export, diff, and rebuild programmatically enables sophisticated agentic workflows.

### 5. Permissions Model ✅ DECIDED

**Decision**: Permissions managed by applications, not the framework

**Details**:

- App builders decide which users can use which knowledgebases
- App builders decide which users can update/ingest into knowledgebases
- Framework provides the tools, apps implement permission logic
- No built-in permission system at the Ergo level

**Rationale**: Different applications have vastly different permission needs. A single-user productivity app has different requirements than a multi-tenant collaborative system. Apps are better positioned to implement appropriate permissions.

## Implementation Priorities

### Phase 1: Core Foundation

1. **OpenAI Context Serialization**: Enable pause/resume workflows
2. **Tool Approval System**: Safe tool execution with approval mechanisms
3. **Pluggable Embeddings**: Provider interface with OpenAI default

### Phase 2: Knowledge Management

1. **Flatfile Export/Import**: KB management for agentic processing
2. **MCP Tool Utilities**: Reusable search tools for Django apps
3. **Performance Optimization**: Caching and efficient operations

### Phase 3: Developer Experience

1. **Documentation**: Comprehensive guides and examples
2. **Testing Framework**: Test utilities and fixtures
3. **Admin Interface**: Enhanced Django admin integration

## Design Principles

### 1. Framework, Not Platform

Ergo provides powerful building blocks that Django developers compose into their own applications. It's not a complete platform but a toolkit.

### 2. Agent-Friendly Architecture

The system is designed with AI agents as first-class users. Knowledge bases can be managed programmatically, workflows can be paused and resumed, and tools can be composed dynamically.

### 3. Django Integration

Deep integration with Django patterns, ORM, and ecosystem. Feels natural to Django developers rather than like a foreign system.

### 4. Flexibility Over Convention

While providing sensible defaults, the system prioritizes flexibility. Apps can override, extend, or replace most behaviors.

### 5. Production Ready

Built for real applications with performance, reliability, and maintainability in mind from the start.

## Open Questions Remaining

While major architectural decisions are resolved, some implementation details remain:

1. **Hierarchy Organization**: Should we keep hexadecimal codes or use alternative structure for KB organization?
2. **Database Support**: PostgreSQL-only initially or support multiple databases?
3. **LLM Provider Priority**: Which providers beyond OpenAI should be prioritized?
4. **Deployment Packaging**: PyPI package, Docker images, or both?

These questions can be resolved during development as we gain more implementation experience.

## Next Steps

1. Update TODO.md with specific implementation tasks based on these decisions
2. Begin Phase 1 development with workflow context serialization
3. Create proof-of-concept for tool approval system
4. Design and implement pluggable embedding interface
5. Build first MCP tool utilities for knowledge base search

These architectural decisions provide a clear foundation for Django Ergo v1.0 development while maintaining the flexibility needed for diverse use cases.
