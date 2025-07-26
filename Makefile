# -----------------------------------------------------------------------------
# Generate help output when running just `make`
# -----------------------------------------------------------------------------
.DEFAULT_GOAL := help

define PRINT_HELP_PYSCRIPT
import re, sys

for line in sys.stdin:
	match = re.match(r'^([a-zA-Z_-]+):.*?## (.*)$$', line)
	if match:
		target, help = match.groups()
		print("%-20s %s" % (target, help))
endef
export PRINT_HELP_PYSCRIPT

help:
	@python3 -c "$$PRINT_HELP_PYSCRIPT" < $(MAKEFILE_LIST)

# -----------------------------------------------------------------------------

python_version=3.9.11
venv=django-ergo_env

# START - Generic commands
# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------

env:  ## Create virtual environment
	uv venv && make pip_install

env_remove:  ## Remove virtual environment
	pyenv uninstall -f ${venv}

sqllite_remove:	## Remove sqlite database
	rm -f db.sqlite3

env_recreate: sqllite_remove env_remove env pip_install migrations migrate superuser serve ## Recreate environment from scratch

pyenv_rehash:	## Rehash pyenv
	pyenv rehash

# -----------------------------------------------------------------------------
# Pip
# -----------------------------------------------------------------------------

pip_install:  ## Install requirements
	uv pip install --upgrade pip
	@for file in $$(ls requirements/*.txt); do \
			uv pip install -r $$file; \
	done
	pre-commit install

pip_install_editable:  ## Install in editable mode
	python3 -m pip install -e .

pip_list:  ## Run pip list
	python3 -m pip list

pip_freeze:  ## Run pipfreezer freeze
	pipfreezer freeze  --verbose

pip_upgrade:  ## Run pipfreezer upgrade
	pipfreezer upgrade  --verbose

# -----------------------------------------------------------------------------
# Django
# -----------------------------------------------------------------------------

manage:	## Run django manage.py (eg - make manage cmd="shell")
	python3 manage.py ${cmd}

superuser:  ## Create superuser
	python3 manage.py createsuperuser

migrations:  ## Create migrations (eg - make migrations app="core")
	python3 manage.py makemigrations ${app}

migrate:  ## Apply migrations
	python3 manage.py migrate

serve:  ## Run server
	python3 manage.py runserver 127.0.0.1:8000

show_urls:  ## Show urls
	python3 manage.py show_urls

shell:  ## Run shell
	python3 manage.py shell_plus

flush:  ## Flush database
	python3 manage.py flush

# -----------------------------------------------------------------------------
# Testing
# -----------------------------------------------------------------------------

pytest:  ## Run tests
	pytest -vx

pytest_verbose:  ## Run tests in verbose mode
	pytest -vs

coverage:  ## Run tests with coverage
	coverage run -m pytest && coverage html

coverage_verbose:  ## Run tests with coverage in verbose mode
	coverage run -m pytest -vs && coverage html

coverage_skip:  ## Run tests with coverage and skip covered
	coverage run -m pytest -vs && coverage html --skip-covered

open_coverage:  ## Open coverage report
	open htmlcov/index.html

build_devcontainer:
	docker build -t django-ergo-test -f .cursor/Dockerfile .

test_coverage_in_devcontainer: ## Build container and run tests with coverage using start.sh
	docker run --rm -v $(shell pwd):/workspace -w /workspace django-ergo-test /bin/bash -c 'bash .cursor/start.sh && export PATH="/home/ubuntu/.local/bin:$$PATH" && export VENV_ROOT="/home/ubuntu/.venv" && export PATH="$$VENV_ROOT/bin:$$PATH" && export COVERAGE_FILE="/tmp/.coverage" && pytest -v --ds=tests.example_app.settings --cov=src/django_ergo --cov-report=html:/tmp/htmlcov && echo "✅ Tests completed! Copying coverage report..." && cp -r /tmp/htmlcov /workspace/ 2>/dev/null || true'

# -----------------------------------------------------------------------------
# Ruff
# -----------------------------------------------------------------------------

ruff_format: ## Run ruff format
	ruff format src/django_ergo

ruff_check: ## Run ruff check
	ruff check src/django_ergo

ruff_clean: ## Run ruff clean
	ruff clean

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------

clean_build: ## Remove build artifacts
	rm -fr build/ dist/ .eggs/
	find . -name '*.egg-info' -o -name '*.egg' -exec rm -fr {} +

clean_pyc: ## Remove python file artifacts
	find . \( -name '*.pyc' -o -name '*.pyo' -o -name '*~' -o -name '__pycache__' \) -exec rm -fr {} +

clean: clean_build clean_pyc ## Remove all build and python artifacts

clean_pytest_cache:  ## Clear pytest cache
	rm -rf .pytest_cache

clean_ruff_cache:  ## Clear ruff cache
	rm -rf .ruff_cache

clean_tox_cache:  ## Clear tox cache
	rm -rf .tox

clean_coverage:  ## Clear coverage cache
	rm .coverage
	rm -rf htmlcov

clean_tests: clean_pytest_cache clean_ruff_cache clean_tox_cache clean_coverage  ## Clear pytest, ruff, tox, and coverage caches

# -----------------------------------------------------------------------------
# Miscellaneous
# -----------------------------------------------------------------------------

tree:  ## Show directory tree
	tree -I 'build|dist|htmlcov|node_modules|migrations|contrib|__pycache__|*.egg-info|staticfiles|media'

# -----------------------------------------------------------------------------
# Deploy
# -----------------------------------------------------------------------------

dist: clean ## Builds source and wheel package
	python3 -m build

twine_upload_test: dist ## Upload package to pypi test
	twine upload dist/* -r pypitest

twine_upload: dist ## Package and upload a release
	twine upload dist/*

twine_check: dist ## Twine check
	twine check dist/*

# END - Generic commands
# -----------------------------------------------------------------------------
# Project Specific - OpenAI Testing
# -----------------------------------------------------------------------------

# Two-tier OpenAI testing system
tests_openai_real:  ## Run tests against real OpenAI API (costs credits) - generates fixtures
	@echo "🚨 WARNING: This will make real OpenAI API calls and cost credits!"
	@echo "Press Ctrl+C within 5 seconds to cancel..."
	@sleep 5
	TEST_OPENAI=true pytest -v -m "openai_real" --ds=tests.example_app.settings

tests_openai_mocked:  ## Run OpenAI tests using saved fixtures (fast, no API costs)
	pytest -v -m "openai_mocked" --ds=tests.example_app.settings

tests_openai_all:  ## Run all OpenAI tests (both real API and mocked)
	pytest -v -m "openai" --ds=tests.example_app.settings

# Development container testing with OpenAI
tests_with_costs_in_devcontainer:  ## Build container and run costly OpenAI tests to generate fixtures
	@echo "🚨 WARNING: This will make real OpenAI API calls and cost credits!"
	@echo "Make sure OPENAI_API_KEY is set in your environment"
	@echo "Press Ctrl+C within 5 seconds to cancel..."
	@sleep 5
	docker run --rm -v $(shell pwd):/workspace -w /workspace \
		-e OPENAI_API_KEY="$(OPENAI_API_KEY)" \
		-e TEST_OPENAI=true \
		django-ergo-test /bin/bash -c '\
		bash .cursor/start.sh && \
		export PATH="/home/ubuntu/.local/bin:$$PATH" && \
		export VENV_ROOT="/home/ubuntu/.venv" && \
		export PATH="$$VENV_ROOT/bin:$$PATH" && \
		pytest -v -m "openai_real" --ds=tests.example_app.settings && \
		echo "✅ Costly tests completed! Fixtures generated." && \
		echo "Now run: make tests_openai_mocked for fast testing"'

tests_fast_in_devcontainer:  ## Build container and run fast OpenAI tests using fixtures
	docker run --rm -v $(shell pwd):/workspace -w /workspace django-ergo-test /bin/bash -c '\
		bash .cursor/start.sh && \
		export PATH="/home/ubuntu/.local/bin:$$PATH" && \
		export VENV_ROOT="/home/ubuntu/.venv" && \
		export PATH="$$VENV_ROOT/bin:$$PATH" && \
		pytest -v -m "openai_mocked" --ds=tests.example_app.settings'

# Fixture management
clean_openai_fixtures:  ## Remove all OpenAI test fixtures
	rm -rf tests/fixtures/openai/
	@echo "🧹 OpenAI fixtures cleaned. Run tests_openai_real to regenerate."

list_openai_fixtures:  ## List all saved OpenAI fixtures
	@echo "📁 OpenAI test fixtures:"
	@find tests/fixtures/openai -name "*.json" 2>/dev/null | sort || echo "No fixtures found"

# Project Specific - Other
# Add other project specific targets here
