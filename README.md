# Django Ergo

[![Tests](https://github.com/boundcorp/django-ergo/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/boundcorp/django-ergo/actions/workflows/test.yml)

Ergo is a toolkit for building AI agents in Django. It has three layers:

- **The library** (`django_ergo`): conversation sessions on OpenAI (the default) or Claude,
  structured calls, compaction, history tools, a context builder, knowledge
  bases with semantic search, and file attachments. Use the pieces in any
  Django project.
- **Bots** (`django_ergo.bots`): an agent defined as a folder of files. A
  `bot.yaml`, an `agents.md` prompt, Python tools, Markdown skills, a
  knowledge base, schedules, data tables and live pages. Bots keep long-running
  chats with each person, start threads, message each other, and can propose
  changes to their own folder.
- **Ergonaut** (`ergonaut/`): the host that runs bot folders. One command
  brings up the web app, workers, scheduler and channels like Telegram, with
  an embedded database when you don't supply one.

## Quick start: run a bot

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/boundcorp/django-ergo
cd django-ergo/ergonaut
make venv && source .venv/bin/activate
(cd frontend && npm install && npm run build)   # the web app; needs Node

export OPENAI_API_KEY=sk-...      # bots run on OpenAI by default
export ERGONAUT_BOTS=../examples/hello
ergonaut check                      # loads the bot, lists its tools and missing secrets
ergonaut up                         # web, worker, beat and bots; http://localhost:8000
ergonaut manage createsuperuser     # in a second terminal, once up has migrated
```

Or in one container (Postgres, Redis and S3 storage included). Build the
image from the repo root, then run it from any folder holding a `bot.yaml`:

```bash
docker build -f ergonaut/Dockerfile --target aio -t ergonaut .
docker run -v "$PWD:/bot" -v ergonaut-data:/data -p 8000:8000 \
  -e OPENAI_API_KEY --name ergonaut ergonaut
docker exec -it ergonaut ergonaut manage createsuperuser
```

[Getting started](docs/getting-started.md) walks through it, including your
first tool.

## A bot is a folder

```
kitchen/
  bot.yaml             # engine, chats, skills, plugins, schedules
  agents.md            # the system prompt
  tools/tandoor.py     # @bot_tool functions
  skills/meal-planning/SKILL.md
  kb/index.md          # knowledge base; index.md is always in context
  tables.py            # BotTable models (real Django tables)
  pages/dashboard.jhtml
```

```yaml
# bot.yaml
name: kitchen
description: Household kitchen manager
engine: {type: openai, config: {model: gpt-6-luna}}
tools: [tools/tandoor.py]
chats:
  main: {skills: [orchestration, tandoor]}
plugins:
  - name: telegram
    token_env: KITCHEN_TELEGRAM_TOKEN
schedules:
  - name: weekly-meal-plan
    cron: "0 17 * * sun"
    message: Propose next week's dinners with the meal-planning skill.
```

```python
# tools/tandoor.py
"""Recipes, meal plans and the shopping list in Tandoor."""
from django_ergo.bots import bot_tool

@bot_tool(requires_approval=True, takes_context=True)
def add_to_shopping_list(ctx, item: str) -> str:
    """Add an item to the shopping list."""
    return tandoor(ctx.secret("TANDOOR_API_KEY")).add(item)
```

What a bot gets:

- **Chats.** Every person has a *main* chat with each bot, plus any named
  chats the bot declares. Main chats are window chats: each turn sees a
  fixed-size window of recent messages and searches the rest with history
  tools, so they can run forever. Threads are child sessions for focused work.
- **Skills.** Tool files, Markdown skills and plugins are all skills. A chat
  sees a list of them and loads what it needs, so each model call carries a
  short tool list.
- **Orchestration.** Bots message each other's chats and threads
  asynchronously, and nested bot folders become sub-bots.
- **Schedules.** Cron entries that prompt a chat, start a thread, or run
  Python and pass the result to the bot.
- **Data and pages.** `BotTable` models with migrations in the bot folder,
  `.jhtml` pages rendered live over those tables, pinned as tabs in a chat.
- **Workers and tasks.** Slow work runs on Celery and reports back to the
  chat when it's done.
- **Self-management.** The `bot_management` plugin lets a bot edit its own
  repo and open a pull request for you to review.
- **Plugins.** Telegram, knowledge base, pages, attachments, bash, Orca, and
  your own, with lifecycle hooks and webhooks.

## Documentation

**Building bots**

- [Getting started](docs/getting-started.md): run Ergonaut and the hello bot, add a tool
- [Building bots](docs/building-bots.md): the folder, bot.yaml, chats, threads, sub-bots
- [Skills](docs/skills.md): skill folders, tool files, loading and unloading
- [Tools](docs/tools.md): `@bot_tool`, context, secrets, approvals, tasks, workers, toolkits
- [Schedules](docs/schedules.md): cron, targets and actions
- [Memory and knowledge bases](docs/memory.md): history, the `kb/` folder, bots that take notes
- [Data tables](docs/tables.md): `BotTable` models, migrations, pages over tables
- [Attachments](docs/attachments.md): files in chats, images, audio
- [Plugins](docs/plugins.md): the official plugins and writing your own
- [Bot reference](docs/bots.md): every bot.yaml key and official plugin
- [Running Ergonaut](docs/ergonaut.md): commands, settings, containers, production

**Library**

- [Structured calls](docs/structured-calls.md)
- [Compaction](docs/compaction.md)
- [Context builder and window chats](docs/context-builder.md)
- [Message history](docs/message-history.md)
- [Knowledge foundation](docs/knowledge-foundation.md), [knowledge paths](docs/knowledge-paths.md), [filesystem and vector integration](docs/fs-vector-integration.md)
- [Semantic fields and search](docs/semantic-search.md)

**Project**

- [Development](docs/development.md): tests, linting, conventions
- [Git releases](docs/git-release.md)
- [TODO.md](TODO.md): what's being worked on
- [docs/archive/](docs/archive/): older plans and design notes

## Using the library in your own project

Ergo is installed from a reviewed commit on `main` (see
[Git releases](docs/git-release.md)):

```bash
pip install 'django-ergo[bots,openai] @ git+https://github.com/boundcorp/django-ergo.git@FULL_COMMIT_SHA'
```

| Extra | For |
| --- | --- |
| `bots` | bot folders (YAML, Jinja pages, Markdown) |
| `openai` | the OpenAI engine and embeddings |
| `legacy` | the Article/Knowledgebase app with pgvector (PostgreSQL only) |
| `filesystem` | YAML-backed filesystem knowledge bases |
| `images` | downscaling images before they reach the model (Pillow) |
| `telemetry` | OpenTelemetry export |

```python
INSTALLED_APPS = [
    ...
    "django.contrib.postgres",   # for the legacy app's search fields
    "django_ergo",
]

DJANGO_ERGO = {
    "CONVERSATION_ENGINE_TYPE": "openai",            # the default; or "claude"
    "EMBEDDING_PROVIDER": "django_ergo.embedding_providers.OpenAIEmbeddingProvider",
}
```

A conversation, without bots:

```python
from django_ergo.conversation.manager import SessionManager
from django_ergo.conversation.runner import run_conversation_turn

manager = SessionManager()
session = await manager.create_session(user=user, workflow=None, engine_type="openai",
                                       transport_type="api", system_prompt="You are terse.")
engine = await manager.get_engine(session)
async for event in run_conversation_turn(engine, session, "Hello", extra_tools=[my_toolkit]):
    ...   # EngineResponse events, or PendingApproval when a tool needs approval
```

Or one-shot, with a typed result: `await generate_once("Name three herbs", response_model=Herbs)`.

Bots run inside any Django project too; Ergonaut is one host. See
[Building bots](docs/building-bots.md#running-a-bot-from-python).

## Development

```bash
make env && make pip_install
make pytest          # tests; PostgreSQL comes from pgserver, no setup needed
make ruff_check
```

Ergonaut has its own `make venv` and `make test` in `ergonaut/`. See
[Development](docs/development.md).

## License

MIT. See [LICENSE](LICENSE).
