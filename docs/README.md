# Django Ergo Documentation

## Overview

This documentation provides a comprehensive guide for Django Ergo v1.0 development, including architectural decisions, current status, development tasks, and product specifications.

## 📋 Core Project Documents

### Current Status & Planning
- **[STATUS.md](../STATUS.md)** - 🚨 **SINGLE SOURCE OF TRUTH** for current project state
- **[TODO.md](../TODO.md)** - Streamlined development tasks organized by priority
- **[HISTORY.md](../HISTORY.md)** - Project evolution with actual git history and milestones
- **[QUESTIONS.md](../QUESTIONS.md)** - Active questions needing decisions (cleaned up)

### Architecture & Decisions
- **[ARCHITECTURE_ANALYSIS.md](../ARCHITECTURE_ANALYSIS.md)** - Analysis of prototype and architectural foundation
- **[ARCHITECTURE_DECISIONS.md](../ARCHITECTURE_DECISIONS.md)** - Key architectural decisions made for v1.0
- **[ROADMAP.md](../ROADMAP.md)** - 24-week strategic development roadmap

### Product Specifications
- **[Personal Goals App](product/personal-goals-app.md)** - AI-powered personal productivity system
- **[Garden Management App](product/garden-management-app.md)** - Multi-tier garden knowledge system

## 🎯 Quick Start Guide

### For New Contributors
1. **Read [STATUS.md](../STATUS.md)** - Understand current project state
2. **Check [TODO.md](../TODO.md)** - Find tasks matching your expertise  
3. **Review [ARCHITECTURE_ANALYSIS.md](../ARCHITECTURE_ANALYSIS.md)** - Understand the foundation
4. **Ask questions** - Add to [QUESTIONS.md](../QUESTIONS.md) if anything is unclear

### For Development
```bash
# Get started immediately
python manage.py runserver
# Visit http://127.0.0.1:8000/admin/ (admin/admin123)

# Key commands
make pytest          # Run tests
make migrations      # Create migrations
make coverage        # Test coverage
```

## 📚 Recently Consolidated (January 2025)

### What Changed ✅
- **Removed redundancy**: Deleted `PROGRESS_SUMMARY.md` and `SUMMARY.md` 
- **Created [STATUS.md](../STATUS.md)**: New single source of truth for project state
- **Cleaned [QUESTIONS.md](../QUESTIONS.md)**: Moved resolved questions to archive, focused on active decisions
- **Streamlined [TODO.md](../TODO.md)**: Reduced from 299 lines to focused actionable tasks
- **Updated [HISTORY.md](../HISTORY.md)**: Added actual project evolution and git history

### Key Insights from Consolidation
The project had accumulated extensive documentation that wasn't being actively maintained or referenced. The consolidation focused on:
- **Reducing cognitive overhead** while maintaining useful context
- **Creating clear entry points** for new contributors
- **Focusing on actively used documents** rather than generating context for LLMs
- **Maintaining historical decisions** while emphasizing current priorities

## 🏗️ Architecture Highlights

### Core Strengths (Preserved from Prototype)
- **Self-contained workflow engine** without external agent library dependencies
- **Multi-tenant design** with owner-based knowledge bases for scalability
- **Hybrid search system** combining PostgreSQL full-text and vector similarity
- **Extensible tool system** with function decorators and automatic discovery
- **Django-native integration** using ORM patterns and conventions

### v1.0 Enhancements (Decided)
- **MCP Integration**: Reusable tools for Django apps to build MCP servers
- **Pluggable Embeddings**: Provider interface with OpenAI default, custom embeddings support
- **Workflow Context Management**: OpenAI agent serialization with pause/resume and tool approval
- **Knowledge Base Tools**: Flatfile export/import for agentic processing
- **App-level Permissions**: Framework provides tools, apps handle permissions
- **Database**: PostgreSQL only with pgvector for embeddings
- **LLM Provider**: OpenAI only for v1.0

## 📋 Current Development Phase

### Phase 3: Production Features (Current)
**Goal**: Making the system production-ready with real embeddings, OpenAI integration, and public APIs.

**Immediate Priorities**:
1. **PostgreSQL + pgvector** - Enable real semantic search
2. **OpenAI Integration** - Configure real API usage  
3. **REST APIs** - Design and implement public APIs

**Previous Completed Phases**:
- ✅ **Phase 1**: Prototype Analysis - Understanding existing architecture
- ✅ **Phase 2**: Foundation Building - Core models, tools, and admin interface

## 🧪 Testing & Quality Status

### Current Implementation
- **Dual-tier OpenAI testing**: Test mode (free) + production mode (real API)
- **6 working tools**: Knowledge base search and management tools
- **Sample data**: 2 knowledge bases, 6 articles, 1 workflow
- **Admin interface**: Full Django admin with custom views

### Quality Metrics
- **Models**: 5 core models implemented and tested
- **Tool Registry**: 6 tools discovered and working correctly
- **Test Coverage**: Basic functionality verified, expanding toward 90%
- **Development Speed**: ~2 hours for complete foundation implementation

## 🔧 Development Infrastructure

### Environment Setup
- **Container**: Complete Dockerfile with Ubuntu 24.04 + Python 3.9.11
- **Dependencies**: All requirements via UV package manager
- **Database**: SQLite (development) → PostgreSQL + pgvector (production)
- **Tools**: Ruff, pytest, coverage, pre-commit hooks configured

### AI Development Rules
- **Cursor Configuration**: Complete setup in `.cursor/` with development standards
- **Testing Strategy**: Dual-tier OpenAI testing to balance cost and coverage
- **Code Quality**: Automated formatting, linting, and testing pipeline

## 📈 Success Metrics & Targets

### Technical Goals
- **Performance**: Sub-100ms average response time for simple queries
- **Reliability**: 99.9% uptime for core services  
- **Scalability**: Support for 1M+ articles per knowledge base
- **Test Coverage**: 90%+ code coverage maintained

### Adoption Goals
- **Documentation**: Complete API documentation with practical examples
- **Examples**: At least 3 complete example applications demonstrating capabilities
- **Community**: 100+ GitHub stars, 10+ active contributors
- **Usage**: 50+ production deployments across different use cases

## 🚀 Future Vision

Django Ergo v1.0 will provide Django developers with powerful building blocks for creating intelligent, knowledge-aware applications. The system balances:

- **Flexibility** for diverse use cases vs **sensible defaults** for quick starts
- **Powerful AI capabilities** vs **predictable, reliable behavior**
- **Sophisticated features** vs **Django-native simplicity**
- **Framework approach** vs **complete platform solution**

The goal is to make it easy for Django developers to add AI-powered knowledge management to their applications without requiring deep AI expertise.

---

## 📞 Quick Reference

### Essential Links
- **[STATUS.md](../STATUS.md)** - Current state and next steps
- **[TODO.md](../TODO.md)** - Available tasks and priorities
- **Admin Interface**: http://127.0.0.1:8000/admin/ (admin/admin123)

### Key Commands
```bash
# Development
make serve                           # Start development server
make pytest                          # Run tests  
make coverage                        # Test coverage
python manage.py create_sample_data  # Generate sample data

# Quality
make ruff_check                      # Code linting
make ruff_format                     # Code formatting
make migrations                      # Create Django migrations
```

---

*This documentation is actively maintained and reflects the current state of Django Ergo development. Last updated: January 2025*