# Cursor Rules for django-ergo

This directory contains Cursor AI rules that help maintain code quality, consistency, and best practices for the django-ergo AI Knowledgebase Toolkit project.

## Rules Overview

### 1. `django-development.mdc`

**Type**: Auto-attached (globs-based)
**Applies to**: Django-specific Python files
**Purpose**: Django framework best practices, model design, view patterns, and Django-specific conventions.

### 2. `python-standards.mdc`

**Type**: Auto-attached (globs-based)
**Applies to**: All Python files
**Purpose**: Modern Python coding standards, type hints, error handling, and code organization.

### 3. `ai-development.mdc`

**Type**: Auto-attached (globs-based)
**Applies to**: AI/ML related files
**Purpose**: AI/ML development best practices, knowledge management, embeddings, and performance optimization.

### 4. `testing-standards.mdc`

**Type**: Auto-attached (globs-based)
**Applies to**: Test files
**Purpose**: Comprehensive testing standards to maintain 97% code coverage, pytest best practices, and Django testing patterns.

### 5. `code-quality.mdc`

**Type**: Always applied
**Applies to**: All contexts
**Purpose**: Code quality tools configuration, pre-commit hooks, linting, formatting, and development workflow standards.

### 6. `project-structure.mdc`

**Type**: Agent-requested
**Applies to**: When making structural decisions
**Purpose**: Project organization, file naming conventions, directory structure, and architectural guidance.

## Rule Types Explained

Based on the latest Cursor rules format, these rules use different activation patterns:

- **Auto-attached (globs)**: Automatically applied when working with files matching the glob patterns
- **Always applied**: Applied to every chat and command context
- **Agent-requested**: Applied when the AI determines the rule is relevant based on the description

## Frontmatter Format

Each rule follows the modern `.mdc` format with YAML frontmatter:

```yaml
---
description: When and why this rule should be applied
globs: file,patterns,to,match
alwaysApply: true/false
---
```

## Usage Tips

1. **File Matching**: Rules with `globs` automatically activate when you work with matching files
2. **Context Awareness**: The AI will consider rule descriptions to determine relevance
3. **Comprehensive Coverage**: Rules are designed to work together without conflicts
4. **Project-Specific**: All rules are tailored to django-ergo's specific architecture and requirements

## Maintenance

- Rules are version-controlled with the project
- Update rules as the project evolves
- Consider rule effectiveness and adjust descriptions for better AI understanding
- Remove or consolidate rules that become redundant

## Coverage

These rules cover:

- ✅ Django framework best practices
- ✅ Modern Python development standards
- ✅ AI/ML development patterns
- ✅ Comprehensive testing strategies
- ✅ Code quality and tooling
- ✅ Project structure and organization
- ✅ Type safety and error handling
- ✅ Performance optimization
- ✅ Security best practices

The rules are designed to maintain the project's high standards while providing clear guidance for consistent development practices.
