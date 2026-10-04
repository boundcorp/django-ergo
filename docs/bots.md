# Bot reference

The full list of bot.yaml keys, tool APIs and official plugin options.
For a guided introduction, start with [Building bots](building-bots.md).

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
icon: "🍳"                            # shown before the name in Ergonaut (default: its first letter)
color: amber                         # a palette name or hex like "#f59e0b" (default: picked from the name)
instructions: agents.md              # default
engine:
  type: openai                       # or claude (optional); default is the settings engine (openai)
  config: {model: gpt-6-luna}
  api_key_env: KITCHEN_OPENAI_KEY    # read at runtime, never stored
  # with a providers.yaml: config: {model: openai/gpt-6-sol}
root:                                # window settings for main and named chats
  recent: 15                         # latest messages always in context
  budget_tokens: 8000
  granularity: conversation          # or reasoning / full
orchestration: true                  # may the bot delegate at all (false: never)
timezone: America/Los_Angeles        # default for users without a timezone
current_time: true                   # current date and time in every turn
tool_results_in_context: 3           # large tool results each model call keeps in full
chats:
  main:                              # every user's main chat (always there)
    skills: [orchestration, tandoor] # loaded from the start (default: [orchestration])
  reports:                           # a named chat: one per user, its own purpose
    description: Weekly reports
    instructions: Keep each report short.   # added to agents.md in this chat
    skills: [analytics]
threads:                             # child threads (`sessions:` also works)
  skills: []
  allow_create: true                 # may chats (this bot's or other bots') start threads of it?
  archive_after_days: 7              # archive threads idle this long (0 = never)
  default_compaction: {mode: rolling, config: {keep_recent: 15}}   # the default; `stream` also works
skills:
  folder: skills                     # default
  unload_after_turns: 30             # drop a loaded skill unused this many turns
  requires: {meal-planning: [tandoor]}
tools: [tools/tandoor.py]
toolkits: ["myapp.toolkits:make_toolkit"]   # factory(ctx) -> Toolkit or list
plugins:
  - name: ergo_kb
    knowledgebase: Kitchen
  - name: myapp.plugins:AuditPlugin
pull_requests: [boundcorp/ergo-bots] # repos whose open PRs orchestrating chats see
permissions:
  call_bots: [sysadmin]              # other bots this bot may message
```

Only files listed under `tools` are imported, and only from inside the bot
folder, so loading a definition never runs code it didn't name.

`color` takes a hex color or one of `slate`, `red`, `orange`, `amber`,
`yellow`, `lime`, `green`, `emerald`, `teal`, `cyan`, `sky`, `blue`,
`indigo`, `violet`, `purple`, `fuchsia`, `pink` or `rose`. Ergonaut shows the
icon in that color before the bot's name in the sidebar and on its page.

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
are sent back as JSON. A tool can also return images, which the model sees
directly:

```python
from django_ergo.bots import ToolImage, ToolResult, bot_tool

@bot_tool
def sales_chart(days: int = 7) -> ToolResult:
    """Chart of sales."""
    return ToolResult(f"Sales, last {days} days", [ToolImage(png_bytes, name="sales.png")])
```

The image bytes are saved as a file in the chat (`ToolImage.from_attachment(row)`
points at a file the chat already has) and history keeps a reference. Only the
latest two images go to the model on each call (`DJANGO_ERGO["IMAGES_IN_CONTEXT"]`),
downscaled to 1024px with Pillow when it's installed; older ones show as
`[image omitted: name (id=...)]`. See [attachments.md](attachments.md).

Large tool results get the same treatment: each model call carries the
newest three (`tool_results_in_context` in bot.yaml, default
`DJANGO_ERGO["TOOL_RESULTS_IN_CONTEXT"]`) in full, and older ones over 500
characters go as a stub naming the tool and its size, so a long turn that
keeps reading a big dump doesn't re-send every earlier copy. History keeps
every result; the bot calls the tool again if it needs an old one. See
[structured-calls.md](structured-calls.md).

A tool module can also define
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

## Schedules

A bot can message its chats on a schedule, set in its bot.yaml:

```yaml
schedules:
  - name: weekly-meal-plan
    cron: "0 17 * * sun"      # minute hour day-of-month month day-of-week
    message: Propose next week's dinners with the meal-planning skill.
    to: main                  # main (default), a named chat, or a new thread:
    # to: {thread: "Reports {n}", in: main}   # {n} run number, {date:%b %d} and strftime codes
  - name: weekly-stats        # or an ordered list of actions
    cron: "0 8 * * mon"
    actions:
      - run: tools/analytics.py:pull_stats   # a function in a .py file in the bot folder
        args: {days: 7}                      # it may take ctx (a ToolContext) first
      - prompt: "Summarize last week: {result}"   # {result}: the last run step's value
        to: {thread: "Stats {date:%b %d}"}
    users: [lee]              # default: permissions.users, else everyone with a chat
                              # (a schedule of only run steps: once, as the first admin)
    enabled: true
```

Cron fields take `*`, numbers, ranges, `*/n` steps, lists and day/month
names, read in each person's timezone (theirs, else the bot's, else
`TIME_ZONE`). `django_ergo.bots.schedules.run_due(bots)` sends due messages
(Ergonaut's beat runs it every minute, and a Celery task carries out each run);
a `ScheduleRun` row keeps each from running twice. Actions run in order: a
`run` step calls the function and is recorded as a `BotJob` (status, result,
error, traceback; the bot page lists recent ones), and a failing step stops
the rest. A `prompt` step is sent to its chat like a message. The bot answers in a turn of its own under a `[Scheduled
message: <name>]` header, the reply stays in that chat, and the telegram
plugin passes it on for a main chat.

## Skills

Everything a bot can do beyond answering is a **skill**, and every skill
loads the same way (`django_ergo.bots.skillset`):

- a skill folder: `skills/<name>.md` or `skills/<name>/SKILL.md`, optionally
  with `skills/<name>/tools.py`;
- a tool file from `tools:` (`tools/tandoor.py` is the `tandoor` skill; its
  module docstring is the description);
- a plugin that adds tools (`kb`, `config_repo`, `orca`, `bash`,
  `attachments`, ...) and each `toolkits:` factory;
- built-ins: `history` (always loaded) and `orchestration`.

```markdown
---
name: meal-planning
description: Plan a week of dinners from the recipe library
requires: [tandoor]      # load these too
always_load: false       # load in every chat from the start
---
1. Review the last 60 days of the meal plan with view_meal_plan.
```

Every chat starts with an `ergo_skills_list` result already in its history:
each skill, whether it's loaded, how many tools it has, and a hint for some
unloaded plugins ("3 files in this chat"). `ergo_skill_load(name)` returns
the skill's instructions and context and offers its tools from the next
model call on (the same turn); using a skill's tool keeps it loaded, and one
unused for `skills.unload_after_turns` turns (default 30, counting every turn
in the chat) is dropped again. `ergo_skill_unload` drops one sooner. Loaded
skills are kept per chat. `history` and the skills a chat lists under
`chats.<name>.skills` (or `threads.skills`) are always loaded there.

A plugin's `context_sources` are part of its skill (in context while it's
loaded); `always_context_sources` stay on regardless (the KB's root article
and prefetch).

## Tables

A bot can keep its own data in real Django models. List the files that
declare them under `tables:`:

```yaml
tables: [tables.py]
```

```python
from django.db import models
from django_ergo.bots import BotTable

class House(BotTable):
    """Houses we've looked at for the property search."""

    address = models.CharField(max_length=200)
    price = models.IntegerField(null=True, blank=True)
```

Each bot is a Django app (label `ergo_bot_<name>`, tables
`ergo_bot_<name>_<model>`), so the ORM and admin work on its tables.
Everyone shares the rows. Every row gets `created_at` and `updated_at`.

Schema changes are ordinary Django migrations kept in the bot folder's
`migrations/`, so they're reviewed with the model change and data migrations
have a place to live:

- `python -m django ergo_bot_makemigrations <bot folder>` writes them
  (`--check` fails when one is missing). The bot_management plugin runs it
  before showing a diff and before publishing, so a proposal that changes a
  table carries its migration in the same commit.
- `python -m django ergo_bot_migrate <paths>` applies what's committed.
  Ergonaut runs it at start (`ergonaut up`) and after pulling a bot repo.

A bot with tables gets the `tables` skill: `ergo_table_query` (Django field
lookups, ordering, up to 200 rows or a count), `ergo_table_add`,
`ergo_table_update` (both validated with `full_clean`) and
`ergo_table_delete` (waits for approval). Loading the skill describes each
table's fields; a model's docstring is its description.

## Pages and pins

A `.jhtml` file is a live page: a Jinja template rendered over the bot's
tables each time it's opened (`django_ergo.bots.pages`).

```html
<h1>Ad spend</h1>
<p>{{ table("AdStat").filter(date__gte=days_ago(7)).sum("spend") | money }} this week</p>
{{ blocks.metric(label="Installs", table="AdStat", aggregate="sum", field="installs") }}
{{ blocks.chart(table="AdStat", x="date", y="spend", group="campaign") }}
{% for row in table("AdStat").order_by("-date").limit(10) %}{{ row.campaign }} {{ row.spend | money }}<br>{% endfor %}
```

Pages run in Jinja's sandbox and can only read: `table(name)` is a view with
`filter`, `exclude`, `order_by`, `limit`, `count`, `sum`, `avg`, `min`,
`max`, `group(...)`, `first` and `rows`, never a queryset. `blocks.*` (heading,
markdown, metric, table, chart, html) render common pieces; `now`, `today`,
`days_ago(n)`, `user`, `bot` and the `money`, `number`, `percent` and
`markdown` filters are there too, and `{% include %}` loads other files from
the bot folder. A page without an `<html>` tag gets a layout with Chart.js.

Pages come from two places:

- **The bot folder**, reviewed like the rest of the repo. Pin them in a chat
  with `chats.<name>.pins: [pages/dashboard.jhtml]`, or give a pin a title
  and an icon: `pins: [{path: pages/dashboard.jhtml, title: Dashboard, icon: "📊"}]`.
  Without them a pin shows its file name and an icon for its file type, in the
  chat's pin bar and under the chat in the sidebar. Ergonaut serves bot-folder
  pages and assets (`.html`, `.mjs`, `.js`, `.css`, images, JSON, CSV, never
  Python, YAML or dotfiles) at `/api/bots/<bot>/files/<path>`, in the app's
  origin, so a page can load its own scripts.
  A bot proposing a page through `bot_management` can check it first with
  `ergo_config_repo_preview(path)`: it renders the draft's page in a separate
  process, inside a transaction that's rolled back, after applying the
  draft's migrations and adding sample rows to empty tables (one filled in,
  others with some or all optional fields empty). The same check is
  `python -m django ergo_bot_preview <bot folder> <page>`.
- **Files the bot writes** into a chat with the `pages` plugin. They're
  served from the file's own URL and sandboxed (`Content-Security-Policy:
  sandbox`): scripts run, but without the app's cookies or API.

Any chat file can be pinned (`metadata.pinned`, from the Files panel or
`ergo_page_pin`). Pinned files show as tabs at the top of the chat and open in
place of the transcript.

## Workers

A **Worker** is long-running work a chat or thread started: a build, a data
pull, a coding agent in Orca. The tool that starts it returns at once, the
chat shows as busy (a spinner in the sidebar, a strip above the transcript
with the worker's latest progress), and when the worker finishes its result
comes back to the chat as a message, so the bot follows up with a reply
(`django_ergo.bots.workers`, model `Worker`).

```python
@bot_task
def watch_deploy(ctx, release: str):       # ctx is a WorkerContext
    status = check(release)
    if status != "done":
        return ctx.again(60, progress=f"deploy {status}")   # run again in a minute
    return {"release": release, "status": status}

@bot_tool(takes_context=True)
def deploy(ctx, release: str) -> str:
    ctx.workers.start("watch_deploy", title=f"Deploy {release}", release=release)
    return "Deploying; I'll tell you when it's live."
```

- A worker function runs once, or **polls** by returning `ctx.again(seconds)`.
  Each poll is a fresh task (Ergonaut: a Celery task on the `bot_tasks` queue),
  so nothing holds a process while it waits and a restart loses nothing (beat
  restarts overdue workers). `ctx.state` is a dict kept between polls,
  `ctx.progress(text)` updates the status line, `ctx.tell(text)` sends the chat
  a message while it keeps running (e.g. a question), and `ctx.stopping` turns
  true when it's cancelled.
- Functions are the bot's `@bot_task`s (`task:<name>`) and plugin worker
  functions (`<plugin>:<name>`, from `BotPlugin.worker_functions()`).
- Every chat has the `workers` skill: `ergo_worker_list`, `ergo_worker_cancel`,
  and `ergo_worker_start` for the bot's `@bot_task`s.
- `DJANGO_ERGO["WORKER_RUNNER"]` runs the steps (default: a thread);
  `SESSION_NOTIFIER` wakes live views when a worker changes.

The Orca plugin's `orca_start_worker(spec, worktree, agent)` starts a
supervised Orca worker (with approval) and a Worker (`orca:watch`) that polls
its dispatch every minute, passes the agent's questions to the chat, and
returns its `worker_done` report. `model` and `effort` go to Orca's
`--model`/`--effort`, except for `agent: omp`, which Orca can't give a model
at launch: the plugin writes the worktree's `.omp/config.yml`
(`modelRoles.default: <model>:<effort>`, git-ignored by its own folder) and omp
picks it up. Each chat keeps one Orca mailbox terminal and Run for its
workers; if Orca no longer knows them (their worktree was removed, Orca was
reset), `orca_start_worker` makes new ones and tries once more.

## Chats

Every user has a **main** chat with each bot (formerly the root session),
plus one chat for each named chat in `chats:`, created when first opened.
Main and named chats are window chats with history tools over every session
the bot has with the user. A named chat adds its own `instructions` to
agents.md and loads its own skills. Threads are child sessions of any chat.
Instructions are rebuilt every turn, so edits to agents.md and bot.yaml reach
existing chats. `bot.main_session(user)` and `bot.chat_session(user, name)`
open them (`root_session` still works).

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
main = await bot.main_session(user)          # one per (bot, user)
result = await bot.ask(main, "What's for dinner?")
result.reply          # ChatReply
result.text, result.suggestions, result.approvals
await bot.resume(main, True)                 # approve what the turn is waiting on
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

The **main chat** (and each named chat) is a window chat (see
[context-builder.md](context-builder.md)): each turn it sees the latest
`recent` messages through a context block, sends only the current turn
natively, and has history tools over every session this bot has with the
user. The `orchestration` skill (loaded in main by default) has:

| Tool | What it does |
| --- | --- |
| `ergo_bot_list` | The bots it can message, with their `description`s (also in the context block below) |
| `ergo_thread_list` | A bot's main chat, named chats and threads with the user (default: this bot) |
| `ergo_thread_send` | Message a bot's `main` chat, a named chat, a thread id, or a `new` thread; returns at once |
| `ergo_thread_stop` | Stop the running turn of a thread this bot started (or one of its own) and cancel what it queued there |
| `ergo_thread_resolve` | Resolve a finished thread (this one, one of this bot's, or another bot's it started), with a one-line `summary`; refused while workers run, a request is open, an approval is pending, or its last reply asks the user something. History stays readable; a new message reopens it. `ergo_thread_archive` is a deprecated alias |

Messages between sessions are asynchronous, like thread-to-thread
delegation in Codex (`django_ergo.bots.messaging`). `ergo_thread_send`
stores a `ThreadMessage` and returns. The recipient answers it in a turn of
its own, which starts with a `[Message from <bot> · <thread> (thread <id>)]`
header; when that turn finishes, its reply goes back to the sender as a new
message (`[Reply from ...]`) and starts a turn there. Replies are never
answered back, chains of delegation stop after six hops, a recipient that is
mid-turn finishes first, and a turn that stops for approval replies once the
user decides. A message from a person routes nothing back, so a bot's main
chat can take delegated work without its replies reaching Telegram.
Delivery goes through `DJANGO_ERGO["THREAD_MESSAGE_RUNNER"]` (Ergonaut
queues a Celery task) or a background thread. Set `orchestration: false` for
a bot that never delegates; it has no `orchestration` skill (it still
answers messages sent to it, and can message upward, below).

Every turn of a chat with the `orchestration` skill loaded also gets a
**Bots and threads** context block (`django_ergo.bots.overview`), so it can
answer "what's going on?" without asking anyone:

- this bot and each bot it can message, with its description;
- under each, its main chat, named chats and open threads with this user:
  working, waiting for approval or idle, when it last moved, who started it,
  requests open in and out, and running workers
  (`orchestrator.thread_status(session)`, which UIs can use too);
- the latest five messages of each chat as short snippets (the newest gets
  more room; the current chat's are already in its window);
- open pull requests of the repos in bot.yaml's `pull_requests`, from the gh
  CLI (cached five minutes), or from `DJANGO_ERGO["OPEN_PRS"]`
  (`callable(bot) -> [{repo, number, title, draft}]`) when an app keeps its
  own record of them.

It's capped at about 3k tokens: the least recently active chats lose their
snippets first, then drop out, and the block says how many it left out
(`ergo_thread_list` and the history tools still reach them). With seven bots
and nine open chats it is about 2.4k tokens.

Every bot chat also gets a short **This chat** block saying which chat it is:
"You are devbox · Main, the main chat", or for a thread "You are devbox ·
Deploy, a thread started by boundcorp · Main. Do the work here; … your final
reply goes back to it automatically" (`orchestrator.chat_identity`). Shared
agents.md instructions can then say what main does and what a thread does.

Who may message whom:

- **Upward, always.** Any chat may message its own bot's main chat, and a
  bot's main chat may message its parent bot's main chat (not its threads or
  named chats). Bots with `orchestration: false` get one always-loaded tool
  for this, `ergo_message_up` (`to: main` or `to: parent`); with
  orchestration on, `ergo_thread_send` does it and `ergo_bot_list` marks the
  parent.
- **Downward** to sub-bots needs `orchestration` (on by default).
- **Sideways** to any other bot needs `permissions.call_bots`, which also
  lifts the main-chat-only rule for a parent listed there.
- **New threads are the target's call.** `thread: "new"` needs the target
  bot's `threads.allow_create`, whether the sender is the bot itself or
  another bot; otherwise the sender messages its main chat. A thread another
  bot started records `started_by` (the sending chat), `started_by_bot` and
  `started_by_label` in its metadata; Ergonaut links back to that chat from
  the thread's header, and `ergo_thread_list` shows it.
- **Resolving.** The orchestration skill's instructions tell every
  orchestrator when to resolve: when the work is finished (PR merged or
  closed, answer delivered, the user wrapped it up), never while it waits on
  the user, a worker, a reply or an approval, or has an open PR. The Bots and
  threads block marks quiet threads with nothing open as "ready to resolve",
  and a thread resolves itself after its final report. A resolved thread keeps
  `resolved_by` and `resolved_summary` in its metadata until it reopens.
- **Reports upward get no reply.** A thread's message to its own main chat,
  or a main chat's to its parent's, is a one-way report: the recipient's turn
  starts with `[Report from …]` and its reply isn't sent back, so a status
  update doesn't cost the sender another turn for an acknowledgement. Pass
  `ask: true` (to `ergo_thread_send` or `ergo_message_up`) when an answer is
  needed.
- **No nudges.** A chat can't send a second request to a chat while its
  earlier one there is still open, and in the turn that handles a chat's
  reply it can't send that chat a short follow-up ("please continue"):
  under 400 characters is refused unless the reply asked a question. A
  complete new request still goes through.
- **Managing what it started.** `ergo_thread_stop` and `ergo_thread_resolve`
  (with `bot`) work on threads of other bots that a chat of this bot
  started. Stopping a running turn goes through
  `DJANGO_ERGO["TURN_STOPPER"]` (`callable(session_id) -> bool`; Ergonaut
  sets its stop flag, which the turn checks at its next step). Without it,
  `ergo_thread_stop` only cancels this bot's queued messages there.

Threads idle longer than `sessions.archive_after_days` (default 7) are
archived by `django_ergo.bots.archival.archive_idle_threads`, which
Ergonaut's Celery beat runs hourly; threads with a turn in progress, a
pending approval or an unanswered thread message are left alone. Main and named
chats are never archived. A message to an archived thread reopens it.

`BotRegistry.discover("bots/")` loads every folder at or under `bots/` that
has a `bot.yaml`, and lets bots find each other by name. Bot folders can
nest: a bot folder inside another bot's folder is its sub-bot, and the
parent may message its sub-bots with `ergo_thread_send` (as well as
any bot in `permissions.call_bots`), and each sub-bot's main chat may
message the parent's main chat. Each session starts with an
`ergo_bot_list` result already in its history, built from each bot's
`description` in bot.yaml, so instructions don't need to list the bots.

### Pre-seeded tool calls

A toolkit's `pre_seeds()` names tool calls that run before a session's first
model call; their results are written into the history as if the model had
made the calls (each turn for window chats, whose model calls carry only
the current turn). `FunctionToolkit(tools, ctx, seed=["tool_name"])` seeds
zero-argument tools. Skills seed `list_skills`; the bots a chat can reach
are in the "Bots and threads" context block instead.

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
  write: false                       # path KBs: the bot may save articles (ergo_kb_write)
  commit: true                       # ...committed and pushed when the KB is in a git checkout
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
With `write: true` the bot also gets `ergo_kb_write(path, content)` and keeps
its own notes: only `.md` files inside the KB folder, each write committed
and pushed to the checkout's branch at once (`commit: false` leaves it
uncommitted). Use it for things a bot should learn, like preferences; use
`bot_management` for changes you want to review.

### pages

```yaml
- name: pages
  max_bytes: 500000
```

Lets the bot write live pages into a chat. `ergo_page_write(filename, title,
blocks | source, pin=true)` writes or rewrites a `.jhtml` page from blocks
(`heading`, `markdown`, `metric`, `table`, `chart`, `html`) or Jinja source,
and returns a text preview of the render or the error, so the bot can fix it
in the same turn. `ergo_page_get` returns a page's blocks and source,
`ergo_page_preview` renders a chat page, a bot-folder page or some source, and
`ergo_page_pin` pins or unpins any file in the chat, with an optional `title`
and `icon` (an emoji) for the pin. Loading the skill loads
`tables` too.

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
`ergo_attachments_look(attachment_id, question)` lets it see an image or PDF.
An image comes back in the tool result, so the bot looks at it itself. A PDF
or other file goes to the bot's own model as an attachment in a separate call
(kind `attachment_look`) that answers the question. Files sent with a
message (Ergonaut's 📎 button or a pasted image) reach the model natively.
Only the latest two images stay in what's sent to the model; older ones
become `[image omitted: name (id=...)]`, and the bot can look again by id.
`ergo_attachments_archive` clears old files out of the bot's working set
(by id, or `all_files` with optional `older_than_days` / `keep_latest`);
`ergo_attachments_unarchive` brings them back. See
[attachments.md](attachments.md#archiving-session-files).

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
