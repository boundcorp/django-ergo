# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django Ergo is a toolkit for building AI agents in Django, in three layers:

- `src/django_ergo/`: the library. Conversation sessions on Claude and OpenAI
  (`conversation/`), structured calls, compaction, history tools, the context
  builder, knowledge bases (`knowledge/` and the legacy Article models), and
  attachments.
- `src/django_ergo/bots/` and `src/django_ergo/plugins/`: bots defined as
  folders (`bot.yaml`, `agents.md`, tools, skills, schedules, tables, pages)
  and the official plugins.
- `ergonaut/`: the Django project that hosts bot folders (web app, API,
  Celery, the `ergonaut` command). It has its own `CLAUDE.md`.

User docs are in `README.md` and `docs/`. Start with `docs/README.md`.

## How we develop

The goal is that Ergo builds Ergo. Feature work should run through the
platform: delegate it to a bot with the `ergonaut-remote` client, watch the run, and
improve whichever layer fell short (the bot, the library or Ergonaut). The
`ergo-developer` skill describes the loop. Install it and the other agent
skills with `python3 src/django_ergo/bots/skill_library/install.py --bin ~/.local/bin`
(see `docs/agent-skills.md`).

## Commands

```bash
make env && make pip_install    # virtualenv with uv, dev dependencies
make pytest                     # tests (embedded PostgreSQL via pgserver unless DATABASE_URL is set)
make coverage                   # with coverage
make ruff_format && make ruff_check
pytest tests/test_bots.py::test_name -v

# OpenAI fixture tests: real calls cost credits and write fixtures
make tests_openai_real          # TEST_OPENAI=true
make tests_openai_mocked        # replay saved fixtures
```

Ergonaut: `cd ergonaut && make venv && make test`. Pre-commit runs ruff
and prettier (for `ergonaut/frontend`); every hook should pass.

## Where things are

- `bots/definition.py`: bot.yaml parsing (the docstring lists every key)
- `bots/runtime.py`: `Bot` (load, sessions, turns, skills as `SkillDef`s)
- `bots/skillset.py`, `bots/skills.py`: lazy skill loading, skill folders
- `bots/tools.py`: `@bot_tool`, `@bot_task`, `@bot_context`, `ToolContext`, `FunctionToolkit`
- `bots/messaging.py`, `bots/orchestrator.py`: async thread messages between bots
- `bots/schedules.py`, `bots/workers.py`, `bots/background.py`, `bots/archival.py`
- `bots/tables.py`, `bots/pages.py`: `BotTable` models and `.jhtml` pages
- `bots/skill_library/`: library skills (skillbuilder; the agent skills ergo-client, ergo-hosting, ergo-bot-development, ergo-developer, with `install.py` for Claude Code and Codex)
- `plugins/`: `ergo_kb` (`kb.py`), `bot_management`, `pages`, `attachments`, `telegram`, `orca`, `bash`
- `conversation/structured.py`: `StructuredCall` and `run_structured_call`; every bot turn is one (`chat_reply`)
- `conversation/compaction.py`, `conversation/context.py`, `conversation/window.py` (window chats), `conversation/history*.py`
- `settings.py`: `DJANGO_ERGO` defaults

## Database

The legacy `django_ergo` app needs PostgreSQL with pgvector. Tests and
Ergonaut use pgserver (embedded PostgreSQL) when `DATABASE_URL` is unset.
`django_ergo.knowledge` alone also runs on SQLite.

## Conventions

- Keep docs current: a change to bot.yaml keys, plugins, tools or Ergonaut
  behavior updates `docs/bots.md` and the relevant guide in the same PR.
- `TODO.md` tracks the bots and Ergonaut backlog; update it as items land.
- Bot configs live in their own repos (for Boundcorp, `boundcorp/ergo-bots`),
  not here. `examples/` holds public examples; `examples/proprietary/` is
  git-ignored.
- Tool names that Ergo provides start with `ergo_`.
- Install is from a reviewed Git commit, not PyPI (`docs/git-release.md`).
