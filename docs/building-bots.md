# Building bots

A bot is a folder. This guide covers how to put one together; the
[bot reference](bots.md) lists every key and plugin option.

```
boundcorp/                   # a bot folder, here also the repo root
  bot.yaml
  agents.md
  tools/github.py
  skills/
    weekly-review.md
    triage/SKILL.md
    triage/tools.py
  kb/
    index.md                 # always in context
    people.md
  tables.py
  migrations/                # generated for tables.py
  pages/dashboard.jhtml
  kitchen/                   # a sub-bot, with its own bot.yaml
    bot.yaml
    agents.md
```

Only Python files that bot.yaml names (`tools:`, `tables:`) or that live in
a skill folder are imported, so loading a folder never runs code it didn't
ask for.

## bot.yaml essentials

```yaml
name: boundcorp
description: Lee's chief of staff; delegates to the other bots
instructions: agents.md          # default
engine:
  type: openai                   # the default; or claude
  config: {model: gpt-6-luna}
  api_key_env: BOUNDCORP_OPENAI_KEY      # default: the SDK's OPENAI_API_KEY
timezone: America/Los_Angeles
max_turns: 50                    # model calls one reply may use, tool calls included
tools: [tools/github.py]
permissions:
  users: [lee]                   # who may use it (default: everyone)
```

`description` matters: other bots see it when deciding whom to message, and
the web app shows it. The API key is read from the environment on every
turn and never stored.

## Models and providers

A `providers.yaml` at the top of a bot path (the first one found among the
`ERGONAUT_BOTS` paths) lists the engines and models a deployment allows:

```yaml
default: openai/gpt-6-luna   # for bots without an engine, and chats with no pick
providers:
  openai:
    type: openai
    api_key_env: OPENAI_API_KEY
    config: {reasoning_effort: medium}   # shared by its models
    models:
      - gpt-6-luna
      - {name: gpt-6-sol, label: Sol, config: {reasoning_effort: high}}
  anthropic:                   # optional: only if you have an Anthropic key
    type: claude
    api_key_env: ANTHROPIC_API_KEY
    models: [claude-sonnet-5-5]
```

OpenAI is the default engine; Anthropic (Claude) is optional and only
needed if you list it here or set `engine.type: claude`.

Models are named `provider/model`. A bot picks one with
`engine: {config: {model: openai/gpt-6-sol}}`; a bot with no `engine` uses
`default`. In Ergonaut, the chat header and the New thread page have a
model picker listing every model whose provider's key is set. A new thread
can start on any of them; an existing chat can only switch to another model
on the same engine, because its history is stored in that engine's format.
For the same reason, when a bot's model moves to another engine (its bot.yaml
or `default` changes), existing chats stay on their engine: they use
`default` if it's on that engine, else the first available model there.
The file reloads like the bot folders. If an edit breaks it, the last good
version stays in use and admins see the error.

### Claude on your subscription

`transport: cli` runs Claude models through the Claude Code CLI, on the
Claude Pro or Max plan it's logged in with, instead of an API key:

```yaml
providers:
  claude:
    type: claude
    transport: cli
    config: {effort: medium}   # optional: command, config_dir, effort, timeout
    models: [claude-sonnet-5-5, claude-opus-5-5]
```

Install the CLI (`npm install -g @anthropic-ai/claude-code`) where turns
run, and log it in with `claude auth login`, or set `CLAUDE_CODE_OAUTH_TOKEN`
to a token from `claude setup-token`. The provider shows as available when
the `claude` command is found. Without a `providers.yaml`, a bot can ask for
it directly with `engine: {type: claude, transport: cli}`.

Each model call runs `claude -p` once with the chat history. Ergo still runs
the tools, approvals and compaction; the CLI's own tools, skills and settings
are off. The CLI ignores any `ANTHROPIC_API_KEY` or gateway in the
environment, so usage always comes from the subscription, at the rate
Anthropic meters `claude -p` and the Agent SDK. Token costs shown in
Ergonaut are API list prices, not what the plan charges.

Anthropic allows this for your own login on the unmodified CLI. It doesn't
allow serving other people's requests on your login: in a deployment other
people use, each of them needs their own login, so keep a CLI provider to
bots only you use.

## agents.md

The system prompt, rebuilt every turn, so edits reach existing chats at
once. Ergo adds the rest around it: the skill list, the current time, the
KB's index article, context functions and the chat's recent history. Keep
`agents.md` about the bot's job and voice; put procedures in skills, which
only load when needed, and facts in `kb/`.

## Chats and threads

Every person gets a **main** chat with each bot. It is a window chat: each
turn the model sees a fixed window of recent messages (`root.recent`, 15 by
default) in a context block, plus the current turn, and reads further back
with history tools. A main chat never needs resetting.

Named chats are more of the same, one per person, each with its own
instructions and skills:

```yaml
chats:
  main:
    skills: [orchestration, github]     # loaded from the start
    pins: [pages/dashboard.jhtml]       # tabs at the top of the chat
  reports:
    description: Weekly reports
    instructions: Keep each report under 200 words.
    skills: [analytics]
```

**Threads** are child sessions for a piece of work: a delegated task, a
scheduled report, a conversation you start from the web app. They keep
their whole history and fold older messages into summaries with rolling
compaction (the `rolling` mode, formerly called `stream`; both names are
accepted):

```yaml
threads:
  skills: []
  allow_create: true          # may chats (this bot's or other bots') start threads of it?
  archive_after_days: 7       # idle threads are archived (messaging one reopens it)
  default_compaction: {mode: rolling, config: {keep_recent: 15}}
```

See [compaction](compaction.md) for the modes.

Every reply is a structured `ChatReply`: a message, or a question with
suggested answers that channels show as buttons.

## Bots working together

With `orchestration: true` (the default), a chat has the `orchestration`
skill: `ergo_bot_list`, `ergo_thread_list`, `ergo_thread_send` and
`ergo_thread_archive`. Messages are asynchronous. `ergo_thread_send` returns
at once; the recipient answers in a turn of its own, and its reply comes
back to the sender as a new message.

A bot may message:

- its sub-bots (bot folders nested inside its folder),
- bots listed in `permissions.call_bots`,
- its own threads (`new` starts one).

A typical setup is one orchestrating bot at the root of a repo with
specialists nested under it, each with `orchestration: false`.

## Knowledge

A `kb/` folder is the bot's knowledge base, added automatically: Markdown
articles it can search and read. `kb/index.md` is in context on every turn,
so it holds what the bot should always know. To let the bot keep its own
notes there:

```yaml
plugins:
  - name: ergo_kb
    path: kb
    write: true          # ergo_kb_write, committed and pushed at once
```

For long-term reference material, `ergo_kb` can also point at Ergo
knowledge bases in the database (`knowledgebases: [...]`) or any toolkit
factory. See [Memory and knowledge bases](memory.md).

## Tables and pages

Declare real Django models for data the bot owns:

```yaml
tables: [tables.py]
```

```python
from django.db import models
from django_ergo.bots import BotTable

class AdStat(BotTable):
    """Daily ad spend per campaign."""
    date = models.DateField()
    campaign = models.CharField(max_length=200)
    spend = models.DecimalField(max_digits=10, decimal_places=2)
```

Write migrations into the folder with
`ergonaut manage ergo_bot_makemigrations <bot folder>`; Ergonaut applies
them at start and after pulling the repo. The bot gets the `tables` skill
to query and edit rows, and your tool code uses `ctx.table("AdStat")`.

Pages are Jinja templates (`.jhtml`) rendered live over the tables each time
they're opened:

```html
<h1>Ad spend</h1>
{{ blocks.metric(label="This week", table="AdStat", aggregate="sum", field="spend") }}
{{ blocks.chart(table="AdStat", x="date", y="spend", group="campaign") }}
```

Pin them in a chat with `chats.<name>.pins`, or let the bot write its own
with the `pages` plugin. See [Data tables](tables.md).

## People

Ergonaut makes a Django user for each person listed under `people:` (in a
bot.yaml or `ergonaut.yaml`) and links their Telegram id:

```yaml
people:
  lee: {telegram: 123456789, timezone: America/Los_Angeles, email: lee@example.com}
```

These users have no password; set one with
`ergonaut manage changepassword lee` to sign in on the web.

## Bots that change themselves

Keep bot folders in their own git repo. Add `bot_management` to the root
bot and it can read and edit the repo, then publish:

```yaml
plugins:
  - name: bot_management
    mode: propose_pr     # edits a draft worktree and opens a PR; or merge_main
    approve_publish: true
```

In `propose_pr` mode nothing running changes until you merge. With
`ERGONAUT_BOTS_PULL_SECONDS` set, Ergonaut pulls merged changes and
reloads. The bot's page in the web app shows open proposals with their
diffs, and Merge and Close buttons. A bot can preview a draft page with
`ergo_config_repo_preview` before proposing it.

## Checking a bot

```bash
ergonaut check                 # loads every bot; lists skills, tools, plugins, missing secrets
ergonaut chat kitchen --user lee
ergonaut manage ergo_bot_preview <bot folder> pages/dashboard.jhtml
```

A bot folder that fails to load is skipped (its last good version stays
loaded on reload), and admins see the error in the web app's sidebar.

## Running a bot from Python

Ergonaut is one host; any Django project with `django_ergo` installed can
load bots:

```python
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot

bot = Bot.load("bots/kitchen")              # or BotRegistry.discover("bots/")
main = await bot.main_session(user)
result = await bot.ask(main, "What's for dinner?")
result.text, result.suggestions, result.approvals
await bot.resume(main, True)                # approve what the turn is waiting on
```

Background work, schedules, workers and thread messages each have a runner
setting in `DJANGO_ERGO` (`BOT_TASK_RUNNER`, `SCHEDULE_RUNNER`,
`WORKER_RUNNER`, `THREAD_MESSAGE_RUNNER`). Without one they run in the same
process (a thread, or inline for schedules); Ergonaut points them at Celery.
Schedules also need something to call `django_ergo.bots.schedules.run_due`
every minute.
