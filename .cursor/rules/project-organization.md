# Django Ergo Project Organization Rules

## Documentation Strategy

### Living Documents Approach

This project uses a set of living documents that evolve throughout development:

- **ARCHITECTURE_ANALYSIS.md**: Analysis of existing prototype approach and architectural decisions
- **ROADMAP.md**: High-level roadmap with phases, milestones, and success metrics
- **TODO.md**: Detailed task lists organized by priority and area
- **QUESTIONS.md**: Open questions requiring clarification or research
- **docs/product/**: Product specifications for example applications

### Document Maintenance Rules

#### When to Update Documents

- **Add to TODO.md**: When discovering new tasks or requirements during development
- **Update QUESTIONS.md**: When encountering design decisions that need stakeholder input
- **Modify ROADMAP.md**: When scope changes significantly or milestones are reached
- **Archive completed items**: Move finished tasks to DONE.md to maintain clean TODO lists

#### Documentation Standards

- Use clear, actionable language in TODO items
- Include context and rationale in ROADMAP decisions
- Cross-reference related items between documents using links
- Date stamp major changes and decisions
- Maintain consistent formatting and structure

## Task Management Workflow

### Priority Classification

```
High Priority: Critical for current development phase
Medium Priority: Important for upcoming phases
Lower Priority: Future enhancements and technical debt
Research: Investigation needed before implementation can begin
```

### Task Lifecycle

1. **Discovery**: New tasks added to TODO.md with priority and context
2. **Planning**: Break large tasks into smaller, actionable items
3. **Implementation**: Move active tasks to "In Progress" section
4. **Completion**: Move finished tasks to DONE.md with completion notes
5. **Review**: Regular review of priorities and dependencies

### Dependencies and Blocking

- Mark tasks that depend on other work or external decisions
- Use QUESTIONS.md to track blockers requiring stakeholder input
- Document assumptions made when proceeding despite uncertainties
- Plan parallel work streams to minimize blocking

## Development Practices

### Code Organization

- Follow Django best practices for app structure and naming
- Place Django Ergo core functionality in `src/django_ergo/`
- Keep example applications separate from core library code
- Use consistent naming conventions across models, views, and APIs

### Documentation Integration

- Include docstrings for all public APIs and complex functions
- Link code comments to relevant sections in planning documents
- Update documentation when making architectural changes
- Maintain examples that demonstrate current capabilities

### Testing Strategy

- Write tests for all new functionality
- Include integration tests for complex workflows
- Test example applications as part of CI pipeline
- Document testing approach and coverage goals

## Stakeholder Communication

### Regular Updates

- Provide progress updates referencing specific ROADMAP milestones
- Highlight completed TODO items and newly discovered requirements
- Surface QUESTIONS.md items that need stakeholder input
- Share architectural decisions and their rationale

### Decision Documentation

- Record major architectural decisions in ARCHITECTURE_ANALYSIS.md
- Document trade-offs and alternatives considered
- Update ROADMAP.md when scope or priorities change
- Use QUESTIONS.md to track open decisions and their resolution

### Change Management

- Use version control for all documentation changes
- Tag major milestone completions and architectural changes
- Maintain change history for important decisions
- Create pull requests for significant documentation updates

## Future Agent Guidelines

### Onboarding New Contributors

1. Read ARCHITECTURE_ANALYSIS.md to understand the current approach
2. Review ROADMAP.md to understand project direction and current phase
3. Check TODO.md for available tasks matching your expertise
4. Add any unclear items to QUESTIONS.md for clarification

### Working with Living Documents

- Update documents as you work, not just at project end
- Be specific and actionable in TODO items you add
- Include sufficient context for future developers
- Link related items across documents when relevant

### Code Contribution Standards

- Follow existing patterns and architectural decisions
- Add tests for new functionality
- Update documentation to reflect changes
- Consider impact on example applications

### Communication Patterns

- Use QUESTIONS.md for anything requiring clarification
- Update TODO.md when you discover new requirements
- Document significant decisions and their rationale
- Keep stakeholders informed of progress and blockers

## Tool Integration

### Development Environment

- Use Django management commands for common operations
- Integrate with Django admin for content management
- Support both development and production configurations
- Document setup requirements and dependencies

### Continuous Integration

- Run tests on all supported Python and Django versions
- Include documentation quality checks
- Validate example applications build and run correctly
- Check for security vulnerabilities in dependencies

### Deployment Considerations

- Support multiple deployment scenarios (development, staging, production)
- Document infrastructure requirements and recommendations
- Provide Docker configurations for containerized deployment
- Include monitoring and logging guidance

## Quality Standards

### Code Quality

- Maintain high test coverage (target: 90%+)
- Use consistent coding style and formatting
- Include comprehensive error handling
- Optimize for readability and maintainability

### Documentation Quality

- Keep documentation current with code changes
- Use clear, actionable language
- Provide complete examples for complex features
- Include troubleshooting guidance for common issues

### User Experience

- Design APIs that are intuitive and consistent
- Provide helpful error messages and debugging information
- Include sensible defaults while allowing customization
- Consider both developer and end-user experience

This project organization approach ensures that Django Ergo develops systematically while maintaining flexibility to adapt based on user feedback and changing requirements.
