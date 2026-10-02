# Bots

A bot is a folder:

```
kitchen/
  agents.md        # instructions (the system prompt)
  bot.yaml         # configuration
  tools/tandoor.py # tool modules
```

Install the extra for YAML support: `pip install 'django-ergo[bots]'`.

## bot.yaml

```yaml
name: kitchen
description: Household kitchen manager
instructions: agents.md              # default
engine:
  type: claude                       # or openai; default is the settings engine
  config: {model: claude-sonnet-4-5}
  api_key_env: KITCHEN_ANTHROPIC_KEY # read at runtime, never stored
root:                                # the root (stream) session
  recent: 15                         # latest messages always in context
  budget_tokens: 8000
  granularity: conversation          # or reasoning / full
orchestration: true                  # thread tools on the root; false = none
timezone: America/Los_Angeles        # default for users without a timezone
current_time: true                   # current date and time in every turn
sessions:
  allow_create: true                 # may the root start threads?
  archive_after_days: 7              # archive threads idle this long (0 = never)
  default_compaction: {mode: context_size, config: {keep_recent: 6}}
tools: [tools/tandoor.py]
toolkits: ["myapp.toolkits:make_toolkit"]   # factory(ctx) -> Toolkit or list
plugins:
  - name: ergo_kb
    knowledgebase: Kitchen
  - name: myapp.plugins:AuditPlugin
permissions:
  call_bots: [sysadmin]              # other bots this bot may message
```

Only files listed under `tools` are imported, and only from inside the bot
folder, so loading a definition never runs code it didn't name.

## Tools

```python
from django_ergo.bots import bot_tool

@bot_tool(description="Find recipes")
def find_recipe(query: str, limit: int = 5) -> list[dict]:
    ...

@bot_tool(requires_approval=True, takes_context=True)
def add_to_list(ctx, item: str) -> str:
    """Add an item to the shopping list."""
    ...   # ctx.bot, ctx.session, ctx.user, ctx.is_root
```

Parameters come from type hints (`str`, `int`, `float`, `bool`, `list`,
`dict`), or pass `parameters=` as JSON Schema properties. Non-string results
are sent back as JSON. A tool module can also define
`toolkits(ctx) -> list[Toolkit]` for class-based toolkits.

Tool code reads credentials with `ctx.secret("TANDOOR_API_KEY")`. It
returns the environment variable `TANDOOR_API_KEY__<USERNAME>` (the user's
name upper-cased, other characters as `_`) when set, else
`TANDOOR_API_KEY`, so each person can bring their own key. `ctx.now()` is
the current time in the user's `timezone` attribute, the bot's `timezone`,
or Django's `TIME_ZONE`, in that order.

## Background tasks

Slow work in a tool file can run off the chat turn. Mark a function with
`@bot_task` and start it from a tool with `ctx.tasks`:

```python
from django_ergo.bots import bot_task, bot_tool

@bot_task
def import_receipts(month: str) -> dict:
    ...

@bot_tool(takes_context=True)
def receipts(ctx, month: str) -> dict:
    job = ctx.tasks.start(import_receipts, month)   # returns at once
    return job.wait(timeout=300)                    # or `await job`
```

`ctx.tasks.run(fn, *args, timeout=...)` starts and waits in one call; tasks
can be async functions too. Arguments and results must be JSON-serializable.
`DJANGO_ERGO["BOT_TASK_RUNNER"]` decides where tasks run: Ergonaut sends them
to its Celery workers; without it they run in a thread pool in the same
process. A worker only runs tasks the bot's own tool files declared.

## Skills

A `skills/` folder (or the folder named by `skills:` in bot.yaml) holds
instructions the bot loads only when it needs them. Each skill is
`skills/<name>.md` or `skills/<name>/SKILL.md`, with optional front matter:

```markdown
---
name: meal-planning
description: Plan a week of dinners from the recipe library
---
1. Review the last 60 days of the meal plan with view_meal_plan.
2. ...
```

A bot with skills gets `list_skills` and `load_skill` tools, and its
sessions start with a `list_skills` result already in history, naming every
skill and tool. Root sessions only carry the current turn natively, so they
get that result on every turn.

## Knowledge base folder

A `kb/` folder in the bot folder is the bot's knowledge base: Markdown
articles it can search and read (the `ergo_kb` plugin, added automatically).
The root article, `kb/index.md`, is in context on every turn, so it is the
place for what the bot should always know.

## Context functions

A tool module can also put live data into every turn's context:

```python
from django_ergo.bots import bot_context

@bot_context(title="Shopping list", weight=1.0)
def shopping_list(ctx, message: str) -> str:
    return render(tandoor(ctx).shopping_list())
```

Each function's text becomes a section of the turn's context, trimmed to
its share of `root.budget_tokens`. An empty string adds nothing, and an
exception is logged and adds nothing, so an outage in one source never
fails the turn. The current date and time is added the same way unless
`current_time: false`.

Tools with `requires_approval=True` are not run. The turn ends with a
`PendingApproval` instead, and `bot.resume(session, {tool_use_id: True})`
continues it. Declined calls go back to the model as errors.

## Running a bot

```python
from django_ergo.bots.runtime import Bot

bot = Bot.load("bots/kitchen")
root = await bot.root_session(user)          # one per (bot, user)
result = await bot.ask(root, "What's for dinner?")
result.reply          # ChatReply
result.text, result.suggestions, result.approvals
await bot.resume(root, True)                 # approve what the turn is waiting on
```

Every turn is a **chat reply**: a [structured call](structured-calls.md) of
kind `chat_reply` against the session. The bot uses its tools, then answers
by calling `send_reply` with a `ChatReply`:

- `type: message`: what to tell the user, optionally with suggested
  follow-ups.
- `type: question`: something the bot needs from the user, with 2 to 4
  suggested answers. Channels show suggestions as buttons, and the user can
  still type anything.

History stores each reply as readable text, with its suggestions, so later
turns and the history tools see the conversation as the user did. A tool
marked `requires_approval` pauses the turn (`result.approvals`), and
`bot.resume(session, decisions)` continues it. `ChatReply` and
`chat_reply_spec` live in `django_ergo.conversation.chat_reply` and work for
any chat session, not only bots.

The **root session** is a stream chat (see
[context-builder.md](context-builder.md)): each turn it sees the latest
`recent` messages through a context block, sends only the current turn
natively, and has history tools over every session this bot has with the
user. It also gets the orchestrator tools:

| Tool | What it does |
| --- | --- |
| `ergo_bot_list` | The bots it can message, with their `description`s (pre-seeded) |
| `ergo_thread_list` | A bot's root chat and threads with the user (default: this bot) |
| `ergo_thread_send` | Message a bot's `root` chat, a thread id, or a `new` thread; returns at once |
| `ergo_thread_archive` | Archive one of this bot's threads; its history stays readable |

Messages between sessions are asynchronous, like thread-to-thread
delegation in Codex (`django_ergo.bots.messaging`). `ergo_thread_send`
stores a `ThreadMessage` and returns. The recipient answers it in a turn of
its own, which starts with a `[Message from <bot> · <thread> (thread <id>)]`
header; when that turn finishes, its reply goes back to the sender as a new
message (`[Reply from ...]`) and starts a turn there. Replies are never
answered back, chains of delegation stop after six hops, a recipient that is
mid-turn finishes first, and a turn that stops for approval replies once the
user decides. A message from a person routes nothing back, so a bot's root
chat can take delegated work without its replies reaching Telegram.
Delivery goes through `DJANGO_ERGO["THREAD_MESSAGE_RUNNER"]` (Ergonaut
queues a Celery task) or a background thread. Starting a thread of the bot's
own needs `sessions.allow_create`; set `orchestration: false` for a bot that
only answers in its root session and gets none of these tools (it still
answers messages sent to it).

Threads idle longer than `sessions.archive_after_days` (default 7) are
archived by `django_ergo.bots.archival.archive_idle_threads`, which
Ergonaut's Celery beat runs hourly; threads with a turn in progress, a
pending approval or an unanswered thread message are left alone. Root chats
are never archived. A message to an archived thread reopens it.

`BotRegistry.discover("bots/")` loads every folder at or under `bots/` that
has a `bot.yaml`, and lets bots find each other by name. Bot folders can
nest: a bot folder inside another bot's folder is its sub-bot, and the
parent may message its sub-bots with `ergo_thread_send` (as well as
any bot in `permissions.call_bots`). Each session starts with an
`ergo_bot_list` result already in its history, built from each bot's
`description` in bot.yaml, so instructions don't need to list the bots.

### Pre-seeded tool calls

A toolkit's `pre_seeds()` names tool calls that run before a session's first
model call; their results are written into the history as if the model had
made the calls (each turn for stream sessions, whose model calls carry only
the current turn). `FunctionToolkit(tools, ctx, seed=["tool_name"])` seeds
zero-argument tools. The orchestrator seeds `ergo_bot_list` and skills seed
`list_skills`.

```
boundcorp/
  bot.yaml          # orchestration on
  kitchen/
    bot.yaml        # a sub-bot of boundcorp, orchestration: false
    skills/
    kb/
```

Engines are built per turn from the definition. The API key is read from
`engine.api_key_env` each time and never written to the session.

## Plugins

A plugin adds tools and context and hooks into the bot lifecycle:

```python
from django_ergo.bots.plugins import BotPlugin

class AuditPlugin(BotPlugin):
    name = "audit"

    def on_load(self): ...                            # bot constructed
    def toolkits(self, ctx): return []                # tools per session
    def context_sources(self, ctx, message): return []  # context per turn
    async def on_session_created(self, session): ...
    async def before_turn(self, session, message): ...
    async def after_turn(self, session, message, result): ...
    async def on_session_closed(self, session): ...
    async def serve(self): ...                        # long-running, e.g. a chat channel
```

Hooks may be sync or async. Every key in a plugin's `bot.yaml` entry other
than `name` is passed as `self.config`. Plugins are named by dotted path
(`module:Class`), by a short name registered in
`DJANGO_ERGO["BOT_PLUGINS"]`, or by a built-in name. `bot.serve()` runs
every plugin's `serve()` together.

## Webhooks

A plugin can serve webhooks by returning handlers from `webhooks()`:

```python
class GitHubPlugin(BotPlugin):
    name = "github"

    def webhooks(self):
        return {"push": self.on_push}

    async def on_push(self, request):
        ...                      # check the signature, then act
        return {"ok": True}     # JSON, an HttpResponse, or None for 200
```

The project mounts every plugin's webhooks with one URL pattern and tells
Ergo which bots this process serves:

```python
# urls.py
path("hooks/", include("django_ergo.bots.urls")),

# startup, e.g. AppConfig.ready()
from django_ergo.bots import webhooks
webhooks.set_registry(BotRegistry.discover("bots/"))
```

A request to `hooks/<bot>/<plugin>/<name>/` reaches that handler; anything
else is a 404. Set `DJANGO_ERGO["BOT_WEBHOOK_BASE_URL"]` to the public URL
of that mount (`https://bots.example.com/hooks`) and
`plugin.webhook_url(name)` gives the full URL to register with the outside
service. Handlers check their own secrets.

## Official plugins

Ergo's own plugins live in `django_ergo.plugins`, one module each, and bot.yaml
names them by short name. Tools that belong to one bot (like a kitchen bot's
Tandoor tools) go in that bot folder's `tools/` instead.

### ergo_kb

```yaml
- name: ergo_kb
  path: kb                           # Markdown folder, relative to the bot folder
  # or knowledgebases: [Kitchen], or toolkit: "myapp.kb:make_toolkit"
  prefetch: new_session              # new_session | every_turn | off
  search_tool: ergo_kb_search
  top_k: 5
```

Adds the KB tools to every session. Prefetch calls the search tool with the
user's message and puts the results in that turn's context block: on a
session's first turn by default, or every turn (useful for a root session,
which rarely starts over). Results are not stored, and a failed search never
blocks the turn. `toolkit:` accepts any factory `(ctx) -> Toolkit`, for
example one returning a `CorpusToolkit`; set `search_tool` to its search tool.

With `path`, the knowledge base is a folder of Markdown files (it may sit
next to the bot folder, as `../kb`, in the same repo). Each file is an
article titled by its first `# Heading`. The bot gets `ergo_kb_search` (keyword
search), `ergo_kb_read` and `ergo_kb_list`, and prefetch adds matching articles to the
context only when something matches. Add the `bot_management` plugin and the
bot can edit articles with `ergo_config_repo_write` and propose them with
`ergo_config_repo_publish`, which is how it keeps notes such as household preferences up
to date under your review.

### bot_management

```yaml
- name: bot_management
  mode: propose_pr      # or merge_main
  main_branch: main
  approve_publish: true
  root_only: true
```

Lets the bot maintain the git repository its folder lives in, so it can
change its own instructions, config and tools, or add new bots.
`ergo_config_repo_status`, `ergo_config_repo_list`, `ergo_config_repo_read`, `ergo_config_repo_diff` and `ergo_config_repo_write` look
at and edit the files (paths can't leave the repo or touch `.git`), and
`ergo_config_repo_discard` throws unpublished edits away.

In `merge_main` mode the bot edits the checkout it runs from, and
`ergo_config_repo_publish` commits, rebases on main and pushes. In `propose_pr` mode it
edits a draft instead: a separate git worktree of main kept inside `.git`,
so the running bots don't change until you merge. `ergo_config_repo_publish` then pushes
a `bot/<name>/<time>-<title>` branch and opens a pull request with `gh`.
Publishing needs approval unless `approve_publish: false`. `ergo_config_repo_pull`
fast-forwards main and `ergo_config_repo_prs` lists open pull requests. Changes take
effect when the bot is loaded again (Ergonaut reloads changed bot files on
its own, and with `ERGONAUT_BOTS_PULL_SECONDS` pulls merged changes too).
For review screens the plugin also has `draft_diff()`, `pull_requests()`,
`pull_request_diff(n)`, `merge_pull_request(n)` (squash-merge, then pull) and
`close_pull_request(n)`; Ergonaut's bot page uses them for its Changes section.

### orca

```yaml
- name: orca
  environment: devbox        # every call is pinned to this Orca environment
  executable: orca-ide       # default: orca-ide if installed, else orca
  approve_changes: true
  root_only: true
```

Lets the bot run the Orca CLI on its host to manage worktrees, terminals and
supervised workers. `orca_read` runs inventory commands with no approval
(`status`, `worktree ps|list|show`, `terminal list|read|show`, `repo
list|show`, `orchestration run-list|worker-list|worker-read|worker-show`,
`search`, `skills get`, anything with `--help`). `orca_run` runs everything
else, such as `worktree create`, `terminal send` or `orchestration
worker-start`, and waits for approval unless `approve_changes: false`.
Arguments are an argv list, never a shell string, and `--json` is added for
the bot. With `environment` set, the bot can't point a call elsewhere.

### bash

```yaml
- name: bash
  cwd: ~               # working directory
  approve: true        # every command waits for approval
  timeout: 120
  root_only: true
```

Gives the bot `ergo_bash_run(command, cwd?)`, which runs `bash -lc` on the
host as the user Ergonaut runs as. Output is stdout and stderr with the exit
code, trimmed to its start and end when long. It is the whole machine, so
keep `approve: true` unless you trust the bot with it.

### attachments

```yaml
- name: attachments
  max_bytes: 5000000     # largest file the bot may write
  other_sessions: true   # may read files in the user's other sessions
```

Files in a chat session are `ConversationAttachment` rows: sent with a
message, uploaded to the session (Ergonaut's Files panel), or written by the
bot (`source` says which; session files have no `message_sequence`). The bot
gets `ergo_attachments_list`, `ergo_attachments_read`,
`ergo_attachments_create` and `ergo_attachments_update`. It writes text
files only in its own session, reads files in the same user's other
sessions, and sees the session's file list in every turn's context.
`ergo_attachments_look(attachment_id, question)` lets it see an image or PDF:
the file goes to the bot's own model as an attachment in a separate call
(kind `attachment_look`), which works with every engine. Files sent with a
message (Ergonaut's 📎 button or a pasted image) reach the model natively.

### telegram

```yaml
- name: telegram
  token_env: KITCHEN_TELEGRAM_TOKEN
  users: {123456789: lee}    # Telegram user or chat id -> Django username
  album_wait: 1.5            # seconds to collect an album's photos
  mode: auto                 # webhook, polling, or auto
  secret_env: KITCHEN_TELEGRAM_SECRET   # optional webhook secret
  notify_delegations: true   # pass delegated replies in the root chat on to Telegram
```

In `auto` mode Telegram uses a webhook when `BOT_WEBHOOK_BASE_URL` is set:
`bot.serve()` registers it and returns, and updates arrive at
`hooks/<bot>/telegram/update/` with Telegram's secret-token header. Without
a public URL it long-polls.

`bot.serve()` long-polls the Bot API. Each message's sender is looked up in
`users`, then its chat, so in a shared group chat each person talks to the
bot as themselves (in their own root session) and the reply goes back to
the group. Messages from anyone else are ignored. The photos of an album
arrive as one turn. Photos, voice notes, audio
and documents become attachments. A turn that stops for approval replies
with Approve and Deny buttons that resume it. `plugin.notify(user, text)`
sends a message from other code.
