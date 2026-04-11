# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django Ergo is an AI Knowledgebase Toolkit for Django that provides semantic search and knowledge management capabilities through innovative field types and AI integration. The project uses PostgreSQL with pgvector for vector operations and integrates deeply with Django patterns.

## Development Commands

### Environment Setup

```bash
# 1. Copy environment variables template and configure
cp .env.example .env
# Edit .env with your database credentials and API keys

# 2. Set up Python environment
make env               # Create virtual environment with uv
make pip_install       # Install all dependencies

# 3. Set up database
make migrations        # Create database migrations
make migrate          # Apply migrations
make superuser        # Create Django superuser

# 4. Run development server
make serve            # Run dev server at 127.0.0.1:8000
```

### Testing

```bash
make pytest                      # Run standard tests
make pytest_verbose             # Run tests with verbose output
make coverage                   # Run tests with coverage report
make open_coverage              # Open coverage HTML report

# OpenAI-specific tests (two-tier system)
make tests_openai_real          # Run real API tests (costs credits, generates fixtures)
make tests_openai_mocked        # Run tests using saved fixtures (fast, no API costs)
make tests_openai_all           # Run all OpenAI tests

# Single test execution
pytest tests/test_specific.py::TestClass::test_method -v
```

### Code Quality

```bash
make ruff_format        # Format code with ruff
make ruff_check        # Check code style with ruff
```

### Build & Release

```bash
make dist               # Build distribution packages
make twine_check       # Check package validity
make twine_upload_test  # Upload to PyPI test
make twine_upload      # Upload to PyPI
```

## Architecture

### Core Components

**SemanticTextField**: The revolutionary field type that automatically generates embeddings for text content. Each semantic field gets its own embedding field, enabling field-specific semantic search.

**Workflow Engine**: Python-based workflows with OpenAI agent context serialization, supporting pause/resume and tool approval systems.

**Tool System**: Declarative tool registry with approval workflows. Tools can be "approved" (run automatically) or "ask" (require user approval).

**Knowledge Base**: Hierarchical article storage with vector search capabilities, designed for agentic processing with export/import utilities.

### Key Models & Fields

- `Workflow`: Defines AI logic and available tools for processing messages
- `Knowledgebase`: Stores hierarchical articles with owner support for multi-tenancy
- `Article`: Individual KB entries with semantic content and embeddings
- `Chat`: Conversation container linking users to workflows
- `ChatMessage`: Message storage with role/type support and metadata
- `SemanticTextField`: Auto-embedding text field type with search capabilities

### Search Architecture

Multiple search levels provide flexibility:

1. High-level `semantic_search()` - auto-embeds queries
2. Low-level `vector_search()` - uses pre-computed vectors
3. Field helpers like `SemanticTextField.search_field()`
4. QuerySet methods for multi-field weighted search

### Embedding System

Fully pluggable with provider interface:

- Default: OpenAI `text-embedding-3-small`
- Configured via `DJANGO_ERGO['EMBEDDING_PROVIDER']` setting
- Custom providers implement `BaseEmbeddingProvider` interface
- Automatic regeneration on content changes

## Database Requirements

**PostgreSQL only** - The project requires PostgreSQL with the pgvector extension for vector operations. SQLite is not supported.

Database credentials are stored in `.env` file (copy from `.env.example`). The test database requires pgvector extension to be enabled.

## Key Design Decisions

1. **Framework, Not Platform**: Provides building blocks for Django developers to compose their own applications
2. **Agent-Friendly**: Designed with AI agents as first-class users with programmatic KB management
3. **Deep Django Integration**: Follows Django patterns, feels natural to Django developers
4. **Python Workflows**: No YAML or visual builders - workflows defined in Python for maximum flexibility
5. **App-Managed Permissions**: Applications decide permissions, not the framework

## Testing Strategy

**Two-tier OpenAI testing**:

- Real API tests generate fixtures (costs credits)
- Mocked tests use saved fixtures (fast, free)
- Set `TEST_OPENAI=true` environment variable for real API tests

**Coverage Target**: 97%+ coverage maintained

## Common Development Tasks

### Adding a New Semantic Field

1. Add `SemanticTextField` to model
2. Add corresponding `VectorField` with same name + `_embedding` suffix
3. Run migrations
4. Embeddings generate automatically on save

### Creating a Tool

1. Define tool function with proper signature
2. Register with `@tool_registry.register` decorator
3. Configure approval requirements in workflow tools_config

### Working with Knowledge Bases

- Use admin interface for manual management
- Use `kb_tools` module for programmatic access
- Export/import via flatfile utilities for agentic processing

## Important Files

- `src/django_ergo/fields.py`: SemanticTextField implementation
- `src/django_ergo/workflow_engine.py`: Workflow execution logic
- `src/django_ergo/tools.py`: Tool registry and base classes
- `src/django_ergo/embedding_providers.py`: Embedding provider interface
- `src/django_ergo/models.py`: Core Django models
- `tests/conftest.py`: Test fixtures and configuration
