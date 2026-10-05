---
name: ergo-bot-development
description: Build or change Ergo bot folders in a bot repo, writing bot.yaml, agents.md, tools, skills, tables, schedules and .jhtml pages, then test them with ergonaut and ergonaut-remote. Use it when writing a new bot or changing one from a checkout. A bot changing its own folder from inside Ergonaut uses skillbuilder instead.
install: [claude, codex]
---
# Developing Ergo bots

A bot is a folder in a git repo, usually its own repo of bots. Ergonaut
serves every folder under `ERGONAUT_BOTS` that has a `bot.yaml`. A bot
folder inside another one is its sub-bot. Changes reach a running server
when the repo is pulled. Ergonaut reloads a bot when its files change, so
prompts, bot.yaml, skills and tools apply from the next turn.

The full reference is in the django-ergo repo:

- `docs/building-bots.md`: guide
- `docs/bots.md`: every bot.yaml key and plugin option
- `docs/skills.md`, `docs/tools.md`, `docs/tables.md`, `docs/schedules.md`
  and `docs/plugins.md`

Read the matching section before you use a key you haven't used before.
The `skillbuilder` skill (`src/django_ergo/bots/skill_library/skillbuilder/SKILL.md`)
has worked examples of tool files, workers, tables, schedules and pages.
Follow it for those details.

**If you are an Ergo bot changing your own folder, load `skillbuilder`
instead.** It edits a draft worktree and proposes a PR through
`bot_management`.

## Before you write

1. Read the repo's existing bots: `bot.yaml`, `agents.md`, and the tool
   files closest to what you're adding. Match their style.
2. Pick the smallest shape that does the job:
   - instructions only: `skills/<name>.md`
   - instructions with tools: `skills/<name>/SKILL.md` and `tools.py`
   - data you will query: a table
   - work on a clock: a schedule
   - a dashboard: a `.jhtml` page pinned in a chat
3. Decide where it lives. Is it a new sub-bot with its own purpose and
   `description`, or a skill on an existing bot? Prefer a skill unless the
   work needs its own persona, permissions or people.

## The pieces

```yaml
# bot.yaml
name: receipts
description: Files receipts from the shared inbox     # other bots route work by this
icon: "🧾"
engine: {config: {model: openai/gpt-6-luna}}           # with providers.yaml; omit for its default
orchestration: false                                    # a specialist; the root bot routes to it
tools: [tools/inbox.py]
tables: [tables.py]
chats: {main: {skills: [inbox], pins: [{path: pages/receipts.jhtml, title: Receipts}]}}
schedules:
  - name: morning
    cron: "0 7 * * *"
    actions:
      - run: tools/inbox.py:pull_new
      - prompt: "New receipts: {result}. File them."
        to: {thread: "Receipts {date:%b %d}"}
```

- `agents.md` is the system prompt. Brief the bot the way you would brief a
  person: who it works for, what it owns, how it replies, and when to ask.
- Tools are `@bot_tool` functions. Type hints become the parameters and the
  docstring becomes the description. Use `requires_approval=True` for
  anything that spends, messages people, deletes or is hard to undo. Read
  secrets with `ctx.secret("NAME")`, never from the repo. Don't start tool
  names with `ergo_`; those are reserved for Ergo.
- Use a worker (`@bot_task` plus `ctx.workers.start`) for anything that
  takes minutes, and a `@bot_task` run for seconds.
- Ergo's library skills (`skills: {include: [...]}`) and the official
  plugins (`ergo_kb`, `pages`, `attachments`, `bot_management`, `orca`,
  `bash`, `kubectl`, `telegram`) cover a lot already. Check `docs/bots.md`
  before writing your own.

## Testing

On a dev server, from `django-ergo/ergonaut` with its virtualenv:

```bash
ERGONAUT_BOTS=~/bots ergonaut check                 # loads every bot: errors, skills, tools, missing secrets
ergonaut chat receipts --user <you>                 # a main chat in the terminal
ergonaut manage ergo_bot_makemigrations ~/bots/receipts   # after changing tables.py
ergonaut manage ergo_bot_preview ~/bots/receipts pages/receipts.jhtml
```

Against a running server with the `ergo-client` skill:

```bash
ergonaut-remote bot receipts                                   # did it load with the skills and tools you expect?
ergonaut-remote new receipts "File yesterday's receipts" --wait
ergonaut-remote show <session> --full                          # which tools it called, with what, and what came back
```

Run the turn and read the transcript. Look for tools the bot didn't find,
missing arguments, oversized results and instructions it ignored. Fix the
description, docstring or prompt, and run it again. Unit-test the plain
Python in tools like any other code.

## Shipping

- One PR per purpose in the bot repo. Name new secrets and schedules in
  the PR body so someone sets them.
- New tool files must be listed under `tools:`, and table files under
  `tables:`. Run `ergo_bot_makemigrations` for table changes and commit the
  migrations.
- Re-read the diff before pushing. Check for secrets, debugging leftovers,
  and docstrings that tell the model when to use each tool.
