# Cursor Background Agent Setup

This directory contains the configuration for Cursor background agents to work with the django-ergo project.

## Files

- `Dockerfile` - Container configuration with all development dependencies
- `setup.sh` - Post-clone setup script for the project
- `README.md` - This documentation

## What's Included

The Dockerfile provides a complete development environment with:

### System Dependencies

- Ubuntu 24.04 base
- Python 3.9.11 (via pyenv)
- UV package manager
- Git, curl, vim, nano, tree, htop
- All necessary build tools and libraries

### Python Development Tools

- pyenv for Python version management
- Virtual environment: `django-ergo_env`
- All project dependencies from requirements files:
  - Django 4.2.9+
  - pytest, pytest-django, pytest-mock
  - ruff (code formatting and linting)
  - coverage (test coverage)
  - pre-commit (git hooks)
  - factory-boy (test factories)
  - django-coverage-plugin
  - djlint (Django template linting)
  - twine, wheel, build (packaging tools)
  - pip-freezer (dependency management)

### User Configuration

- Non-root user: `ubuntu`
- Working directory: `/home/ubuntu`
- Proper PATH and environment setup

## Usage for Background Agents

1. **Build the container** using the Dockerfile
2. **Clone the repository** into the container
3. **Run the setup script**:
   ```bash
   ./cursor/setup.sh
   ```

## Available Commands

After setup, you can use all the Makefile commands:

- `make pytest` - Run tests
- `make serve` - Start Django development server
- `make ruff_check` - Run linting
- `make ruff_format` - Format code
- `make coverage` - Run tests with coverage
- `make migrations` - Create Django migrations
- `make migrate` - Apply migrations

## Development Workflow

1. The container comes with Python 3.9.11 and all dependencies pre-installed
2. The virtual environment is automatically activated
3. Pre-commit hooks are installed for code quality
4. A default superuser is created (admin/admin)
5. Tests are run to verify the setup

## Ports

The container exposes these ports:

- 8000 - Django development server
- 3000 - Frontend development server
- 5432 - PostgreSQL
- 6379 - Redis
