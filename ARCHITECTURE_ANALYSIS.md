# Django Ergo - Architecture Analysis

## Overview

This document analyzes the existing prototype architecture found in the `old-code-inspiration` folder to understand the approach and inform the development of Django Ergo v1.0.

## Current Architecture (Prototype Analysis)

### Core Models

#### 1. User Chat System

- **UserChat**: Individual chat sessions owned by users, associated with specific workflows
- **ChatMessage**: Typed messages supporting multiple roles (user, assistant, system, tool)
- **MessageType**: Enumeration for message types (user_input, assistant_message, tool_request, tool_response, system_message, error)
- **MessageRole**: User, Assistant, System, Tool roles

#### 2. Knowledge Management

- **Knowledgebase**: Hierarchical collections of articles with owner_id support for multi-tenancy
- **Article**: Documents with:
  - Hierarchical organization using hexadecimal codes (e.g., "C3" = 12th chapter, 3rd sub-section)
  - Semantic embeddings (1536 dimensions) for vector search
  - Auto-generated summaries
  - Full-text and vector hybrid search capabilities
  - Custom `SummarizedVectorField` for automatic embedding generation

#### 3. Workflow Engine

- **Workflow**: Defines AI logic, tools, and knowledgebase access
- **WorkflowEngine**: Self-contained system without external agent library dependencies
- **Tool System**: Extensible framework for AI agent capabilities
- **Multi-Agent Support**: Research agents (readonly) and Actor agents (with side effects)

### Key Features

#### 1. Hybrid Search

- Combines PostgreSQL full-text search with pgvector similarity search
- Optimized for both semantic and keyword-based queries
- Hierarchical browsing with efficient index retrieval

#### 2. Multi-Agent Workflows

- **ResearchAgent**: Readonly operations for information gathering
- **ActorAgent**: Performs actions with side effects (requires user approval)
- Agent handoff capabilities between different specialized agents
- State management across agent transitions

#### 3. Document Ingestion

- **IngestKnowledgeBase**: Automated document processing workflow
- **FactExtractionAgent**: Structured information extraction
- Automatic article creation with hierarchy assignment
- Integration with external knowledgebases for context

#### 4. Tool Architecture

- **ToolRegistryBase**: Base class for organizing tools and resources
- **ModelToolset**: Tool collections with user context binding
- Separation between "tools" (side effects) and "resources" (readonly)
- Function decorators for easy tool registration

#### 5. LLM Integration

- OpenAI integration with fallback behavior
- Support for multiple LLM providers
- Async/await support throughout the system
- Template-based responses when LLM is unavailable

### Technical Architecture

#### Database Design

- PostgreSQL with pgvector extension for embeddings
- Django ORM with custom fields (VectorField, SummarizedVectorField)
- Timestamp mixins for audit trails
- JSON fields for flexible metadata storage

#### Async Support

- Native async/await throughout the workflow engine
- ASGI-compatible design
- Sync-to-async adapters for Django ORM operations
- Background task support

#### Dependency Injection

- Protocol-based design for testability
- Pluggable components (AgentRunner, ContentProcessor, KnowledgebaseRepository)
- Configurable tool loading and agent creation

### Strengths of Current Approach

1. **Self-Contained**: No hard dependencies on external agent frameworks
2. **Testable**: Extensive use of protocols and dependency injection
3. **Scalable**: Multi-tenant design with owner-based knowledgebases
4. **Flexible**: Extensible tool system and workflow definitions
5. **Efficient**: Hybrid search and hierarchical organization
6. **Django Native**: Deep integration with Django ORM and patterns

### Areas for Improvement in v1.0

1. **API Design**: Current prototype lacks clear public APIs
2. **Documentation**: Limited developer documentation and examples
3. **Configuration**: Tool and workflow configuration could be more declarative
4. **Performance**: Embedding generation and search optimization needed
5. **Monitoring**: Logging and metrics for workflow execution
6. **Security**: Tool execution permissions and sandboxing
7. **Migration Path**: Clear upgrade path from prototype to v1.0

## Recommended v1.0 Direction

Based on this analysis and stakeholder input, Django Ergo v1.0 should:

1. **Preserve Core Architecture**: Keep the workflow engine, tool system, and knowledge management approach
2. **MCP Integration**: Provide reusable tools for Django apps to build their own MCP servers with utilities like search_user_kb, search_garden_kb
3. **Pluggable Embeddings**: Support multiple embedding providers with settings-based switching, custom embeddings, and both on-demand/background generation
4. **Workflow Context Management**: Implement OpenAI agent context serialization for pause/resume capabilities with tool approval system
5. **Knowledge Base Tools**: Easy flatfile export/import for agentic processing with create/update/diff/build utilities
6. **App-Level Permissions**: Framework provides tools, apps handle permissions and access control
7. **Performance Optimization**: Efficient embedding generation and caching

The prototype demonstrates a solid architectural foundation that can support diverse AI-powered applications while providing the flexibility needed for various use cases.
