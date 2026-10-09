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
  # with a providers.yaml: config: {model: openai/gpt-6.1-sol}
  # transport: cli                   # the logged-in Claude Code or Codex CLI, no key
root:                                # context budget for main and named chats
  recent: 15                         # legacy; ignored by bots
  budget_tokens: 8000
  granularity: conversation          # legacy; ignored by bots
orchestration: true                  # may the bot delegate at all (false: never)
timezone: America/Los_Angeles        # default for users without a timezone
current_time: true                   # current date and time in every turn
tool_results_in_context: 6           # large tool results each model call keeps in full
tool_results_tokens: 40000           # optional budget for older ones; default 20% of model window
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
  default_compaction: {mode: context_size, config: {compact_at_tokens: 150000, keep_tokens: 50000}} # optional; window-relative defaults
skills:
  folder: skills                     # default
  unload_after_turns: 30             # drop a loaded skill unused this many turns
  requires: {meal-planning: [tandoor]}
  include: [skillbuilder]            # skills from Ergo's library (docs/skills.md)
  exclude: [skillbuilder]            # leave out some of DJANGO_ERGO["DEFAULT_SKILLS"]
  defaults: true                     # false: none of the default skills
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
plus every image in the newest round of tool results (up to 8), so looking at
four files at once shows all four; images are downscaled to 1024px with Pillow
when it's installed; older ones show as
`[image omitted: name (id=...)]`. See [attachments.md](attachments.md).

Large tool results get the same treatment: each model call carries the
newest six (`tool_results_in_context` in bot.yaml, default
`DJANGO_ERGO["TOOL_RESULTS_IN_CONTEXT"]`) in full, plus older ones while the
kept results fit a token budget: `tool_results_tokens` in bot.yaml or
`DJANGO_ERGO["TOOL_RESULTS_TOKENS"]`, defaulting to 20% of the engine's
context window (`DJANGO_ERGO["TOOL_RESULTS_CHARS_IN_CONTEXT"]`, when set,
gives the budget in characters instead). Older ones over 500 characters go
as a stub naming the tool and its size, so a long turn that keeps reading a
big dump doesn't re-send every earlier copy; errors and images stay. The bot
should note the detail it needs when it first reads a result, and call the
tool again only if it still needs the detail. History keeps every result.
See [structured-calls.md](structured-calls.md).

A tool module can also define
`toolkits(ctx) -> list[Toolkit]` for class-based toolkits.

A tool module can also declare [page actions](#page-actions), functions a
`.jhtml` page calls as the viewer:

```python
from django_ergo.bots import page_action

@page_action(requires_approval=True)
def restock(ctx, item: str, qty: int = 1) -> dict:
    """Order more of an item."""
    ...
    return {"message": f"Ordered {qty} {item}"}
```

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
        stop_if_empty: true                  # returns nothing: skip the rest, quietly
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
- a plugin that adds tools (`kb`, `config_repo`, `orca`, `bash`, `browser`,
  `attachments`, ...) and each `toolkits:` factory;
- built-ins: `history` (always loaded), `workers`, `orchestration`, and
  `introspection` for bots loaded from a folder;
- Ergo's skill library (`skillbuilder`), when bot.yaml or a skill names one,
  or by default (`DJANGO_ERGO["DEFAULT_SKILLS"]`, unless bot.yaml excludes it).

`introspection` is read-only and needs no plugin, so every bot can see what
it's made of, whether or not it can change its repository (that's
`bot_management`): `ergo_self_overview` (folder, model, skills and their
sources, plugins with secret-looking config hidden, tables, schedules, chats,
sub-bots), `ergo_self_files` and `ergo_self_read` (files in the bot folder, by
line range; `ergo:` paths read Ergo's own source, e.g.
`ergo:plugins/attachments.py`). Hidden files such as `.env` are never listed
or read.

```markdown
---
name: meal-planning
description: Plan a week of dinners from the recipe library
requires: [tandoor]      # load these too
always_load: false       # load in every chat from the start
plugins: {bot_management: {mode: propose_pr}}   # plugins it needs, with settings
---
1. Review the last 60 days of the meal plan with view_meal_plan.
```

Every turn's context has a compact `Skills` section: each skill, whether it
is loaded, and its one-line description. `ergo_skills_list` remains available
when the model needs the fuller listing, including tool counts and hints.
`ergo_skill_load(name)` returns the skill's instructions and context and
offers its tools from the next model call on (the same turn); using a skill's
tool keeps it loaded, and one unused for `skills.unload_after_turns` turns
(default 30, counting every turn in the chat) is dropped again.
`ergo_skill_unload` drops one sooner. Loaded skills are kept per chat.
`history` and the skills a chat lists under `chats.<name>.skills` (or
`threads.skills`) are always loaded there.

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
max`, `group(...)`, `first` and `rows`, never a queryset. `blocks.*` (heading,
markdown, metric, form, table, button, chart, html) render common pieces;
`now`, `today`, `days_ago(n)`, `user`, `bot` and the `money`, `number`,
`percent` and `markdown` filters are there too, and `{% include %}` loads other files from
the bot folder. Rows give dates as ISO strings, so for date math use
`as_datetime` (back to a datetime), `seconds_until` (seconds from now,
negative once past) and `duration` (seconds as `2h 15m`):
`{{ row.resets_at | seconds_until | duration }}`. A page without an `<html>` tag gets a layout with Chart.js.

Pages come from two places:

- **The bot folder**, reviewed like the rest of the repo. Pin them in a chat
  with `chats.<name>.pins: [pages/dashboard.jhtml]`, or give a pin a title
  and an icon: `pins: [{path: pages/dashboard.jhtml, title: Dashboard, icon: "📊"}]`.
  Without them a pin shows its file name and an icon for its file type, in the
  chat's pin bar and under the chat in the sidebar. Ergonaut serves bot-folder
  pages and assets (`.html`, `.mjs`, `.js`, `.css`, images, JSON, CSV, never
  Python, YAML or dotfiles) at `/api/bots/<bot>/files/<path>`. Pages are
  sandboxed like chat pages (below); their relative `src`, `href`, `poster`
  and CSS `url()` references are rewritten to signed asset URLs, so a page
  loads its own scripts and styles.
  A bot proposing a page through `bot_management` can check it first with
  `ergo_config_repo_preview(path)`: it renders the draft's page in a separate
  process, inside a transaction that's rolled back, after applying the
  draft's migrations and adding sample rows to empty tables (one filled in,
  others with some or all optional fields empty). The same check is
  `python -m django ergo_bot_preview <bot folder> <page>`.
- **Files the bot writes** into a chat with the `pages` plugin. They're
  served from the file's own URL and sandboxed (`Content-Security-Policy:
  sandbox`): scripts run, but without the app's cookies or API.
  A page the bot writes can call page actions too, if its bot declares them.

Any chat file can be pinned (`metadata.pinned`, from the Files panel or
`ergo_page_pin`). Pinned files show as tabs at the top of the chat and open in
place of the transcript.

Both kinds are sandboxed: scripts run with an opaque origin, without the
app's cookies, and can't call the API. A folder page's relative asset
references (`script`, `link`, `img`, `source`, `video`, `audio`, `poster`, and
`url()` in `<style>` blocks and `style` attributes) are rewritten when the
page renders to `/api/bots/<bot>/assets/<token>/<path>`. The token is a signed
path segment, valid for an hour for that user and bot folder; it never serves
`.jhtml` files. Imports inside an asset (`import "./util.mjs"`, `url(font.woff)`)
resolve under the same token. What a script builds at run time, like
`fetch("data.json")`, isn't rewritten: read data from tables, not files.

### The `ergo` bridge

Every page gets a small script defining `window.ergo`:

- `ergo.call(name, args)` calls a [page action](#page-actions) and returns a
  Promise of its result object; it rejects with an `Error` carrying the
  message when the action fails or the viewer says no.
- `ergo.on("table:Pantry", handler)` handles changes to a table yourself (see
  [Live refresh](#live-refresh)).
- `ergo.reload()` re-renders the page.
- `ergo.tables` lists the tables the page read.

The page never makes the request: it posts to the viewer, which calls the API
as the logged-in user for the bot and chat the page was opened from. A page
opened outside Ergonaut's viewer gets an `ergo.call` that rejects with "Open
this page in Ergonaut to use its buttons".

### Page actions

A page action is a function a page may call as the viewer. Declare it next to
the bot's tools:

```python
from django_ergo.bots import page_action

@page_action(requires_approval=True, approval_preview=lambda ctx, item, qty: f"Order {qty} × {item}")
def restock(ctx, item: str, qty: int = 1) -> dict:
    """Order more of an item."""
    ctx.table("Pantry").objects.filter(name=item).update(on_order=qty)
    ctx.table("Pantry").touch()
    return {"message": f"Ordered {qty} {item}"}
```

```html
<button onclick="ergo.call('restock', {item: 'flour', qty: 2}).catch(e => alert(e.message))">Restock</button>
```

- The first parameter is the `ToolContext`: `ctx.user` is the viewer,
  `ctx.session` the chat the page was opened from (or `None`), `ctx.page` the
  page (a bot-folder path or a chat file's id). Other parameters come from
  type hints like `@bot_tool`'s, or pass `parameters=` and `required=`. `name=`
  and `description=` override the function's.
- A function may carry both `@bot_tool` and `@page_action`; they're independent,
  and plain tools are never callable from pages. Action names are unique per
  bot, including those in skill folders' `tools.py`.
- Arguments are checked first: a missing required one, an unknown one or the
  wrong JSON type answers 400 with the reason.
- Return a JSON-serializable dict. `message` shows as a toast, `reload: true`
  re-renders the page at once, `open` is a URL for the viewer to open; the
  rest goes to the page as the result. Other return values arrive as
  `{"value": ...}`.
- `requires_approval=True` makes the viewer show a confirm dialog first, with
  `approval_preview(ctx, **args)` (or a default) as its text. The server
  answers the first call with `needs_approval`, a preview and a token signed
  over the user, bot, action and a hash of the arguments, valid 5 minutes; the
  viewer repeats the call with it on Yes. The token never reaches the page, and
  doesn't work for other arguments. Use it for anything that spends money,
  messages people or is hard to undo.
- A `ValueError` or Django `ValidationError` answers 400 with its text, which
  the page sees as the rejection's message. Any other exception answers 500
  with "The action failed" and is logged.
- An action has 30 seconds (`page_actions.TIMEOUT_SECONDS`, 504 after). Start
  longer work with `ctx.tasks` and return at once; the task writes rows as it
  goes and [live refresh](#live-refresh) shows them.

Ergonaut records each call (`PageActionCall`: who, action, args, result or
error, approved, duration; in Django admin). When the call came from a chat,
the bot sees the last 20 on its next turn in the context section "Page
actions since your last reply". Without Ergonaut the host runs
`call_page_action(bot, name, args, ...)` in `django_ergo.bots.page_actions`.

### Page forms and asks

Pages can write a table without declaring Python actions. `blocks.form` adds a
validated add form; `blocks.table(edit=true, delete=true)` adds per-row edit
and approved delete controls; and `blocks.button` invokes a page action:

```html
{{ blocks.form(table="Pantry", fields=["name", "quantity"], submit="Add item") }}
{{ blocks.table(table="Pantry", columns=["name", "quantity"], edit=true, delete=true) }}
{{ blocks.button(label="Review the list", ask="What should I buy next?", args={"chat": "main"}) }}
```

The built-in actions are `ergo.table.add`, `ergo.table.update` and
`ergo.table.delete`. They use the same `full_clean` validation as the table
tools, show field errors next to a form input, and delete only after the
viewer's approval. Set `page_writes = False` on a `BotTable` model to hide
row controls and reject page writes.

`ergo.ask(text, {chat})` sends `From the <page> page: <text>` through the
normal chat message path. `chat` is `"main"` (the default), a named chat, or
`"new"` for a new thread under main. The viewer toasts the destination with an
Open link; its reply and approvals remain in that chat.

### Live refresh

A page re-renders when a table it reads changes (`ergo.tables` lists them,
including tables read through `blocks`). Row saves and deletes announce
themselves; after bulk writes call `ctx.table("X").touch()` (see
[tables](tables.md#live-refresh)).

Ergonaut's page viewer opens one stream per page,
`GET /api/bots/<bot>/tables/events?tables=A,B`, shows a green "Live" dot while
it's connected, and on a change:

- re-renders the page 1 second after the last change, waiting while a form
  field has focus so typing isn't lost; scroll position is kept;
- for tables the page handles itself with `ergo.on("table:Name", fn)`, calls
  `fn({type: "changed", table})` instead and doesn't re-render. The event has
  no rows (the page has no API access): call `ergo.reload()` or update the DOM
  yourself;
- re-renders at once after an action returns `reload: true`.

A viewer that was disconnected catches up with one event when it reconnects.
So pages don't need to poll or reload on a timer.

Pins open in a new tab from the viewer at `/pages/view`, which hosts the same
viewer, so the bridge works there too.

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
- Functions are the bot's `@bot_task`s (`task:<name>`), plugin worker
  functions (`<plugin>:<name>`, from `BotPlugin.worker_functions()`) and
  agent watchers (`agent:<manager>`, see [Agents](#agents)).
- Every chat has the `workers` skill: `ergo_worker_list`, `ergo_worker_cancel`
  (which also stops a coding agent the worker watches), and `ergo_worker_start`
  for the bot's `@bot_task`s.
- `DJANGO_ERGO["WORKER_RUNNER"]` runs the steps (default: a thread);
  `SESSION_NOTIFIER` wakes live views when a worker changes.

The Orca plugin's agent manager starts a supervised Orca worker (with
approval) in the worktree the brief names, and its watcher polls the dispatch
every `worker_poll_seconds` (default 120), passes the agent's questions to the
chat (`ergo_agent_reply` answers them with `orchestration reply`), and returns
its `worker_done` report. `orca_start_worker(spec, worktree, agent)` is the
same as `ergo_agent_start` with Orca's parameter names; workers started before
agents existed (`orca:watch`) keep working. Each check also
reads the agent's latest output (`orchestration worker-read`: its transcript,
or its terminal) into the worker's activity (`WorkerContext.activity`), with
the time it last did something: the newest transcript time or heartbeat, or
the check that first saw new output. A running worker quiet for
`stall_minutes` (default 10) shows as stalled, unless Orca says it's waiting
on a person. `BotPlugin.worker_log` reads a worker's full recent output on
demand (Orca: up to 50 transcript messages or 400 screen lines). `model` and `effort` go to Orca's
`--model`/`--effort`, except for `agent: omp`, which Orca can't give a model
at launch: the plugin writes the worktree's `.omp/config.yml`
(`modelRoles.default: <model>:<effort>`, git-ignored by its own folder) and omp
picks it up. With `tier: <name>` instead (small, medium, large, xlarge, or a
custom name), the agent, model and effort come from the `agents` tiers in
providers.yaml, on whichever subscription has room (see
[Routing by tier](building-bots.md#routing-by-tier)). Each chat keeps one Orca mailbox terminal and Run for its
workers; if Orca no longer knows them (their worktree was removed, Orca was
reset), starting an agent makes new ones and tries once more. If a worker's agent
terminal exits or vanishes without a `worker_done` (Orca keeps such a dispatch
"dispatched"), the watcher fails the worker after five minutes and tells the
chat why.
  The watcher also scans its agent's session files on the worktree host every
  `usage_minutes` (default 10) and once after settlement. Claude Code, Codex,
  and omp tokens appear per worker under **Agent sessions** on Costs. Set
  `files_host` to the SSH host that holds the worktrees, or `""` to scan files
  locally. It is best effort: an unreadable session file preserves the last
  recorded counts and does not fail the worker. Counts are attributed by
  worktree and the worker time window, so overlapping workers in one worktree
  can each include the same agent requests.

## Agents

A coding agent (Codex, Claude Code, omp) works on a brief for minutes to
hours, on a subscription and never an API key. An **agent manager** is
whatever can run one: the Orca plugin today; a host over ssh and a local
shell are next. A plugin adds managers from `agent_managers()`:

```python
from django_ergo.bots.agents import AgentCheck, AgentManager, AgentQuestion

class BoxAgents(AgentManager):
    agents = ("codex", "claude")     # empty: any
    requires_approval = True         # start, reply and stop ask first
    poll_seconds = 120
    where = "the build box"

    def start(self, ctx, spec): ...  # AgentSpec(brief, workspace, agent, model,
                                     # effort, title, tier); return a JSON handle
    def check(self, ctx, handle):    # one look, from the watcher
        return AgentCheck("running", progress="editing", questions=[
            AgentQuestion(id="q1", body="Blue or red?")])
        # or AgentCheck("done", report=...), AgentCheck("failed", error=...)
    def reply(self, ctx, handle, question_id, text): ...
    def stop(self, handle): ...      # optional
    def log(self, worker, handle): ...  # optional: full recent output

class BoxPlugin(BotPlugin):
    name = "box"
    def on_load(self): self.manager = BoxAgents()
    def agent_managers(self): return {"box": self.manager}
```

A bot with a manager has the `agents` skill:

- `ergo_agent_start(brief, workspace, title, tier | agent/model/effort,
  manager)` asks the manager to start the agent and starts a polling Worker,
  `agent:<manager>`, with the handle as its argument. With `tier`, the agent,
  model and effort come from providers.yaml's `agents` tiers. Built-in
  small/medium/large/xlarge lists are derived from listed subscription catalog models
  even without YAML tier declarations; `agents.<name>` replaces that
  name's list, and custom names are supported. Chat `tiers.<name>` overrides
  are independent. Restart Ergonaut and its bot workers after editing
  providers.yaml (see [Routing by tier](building-bots.md#routing-by-tier)).
  The manager must run the picked agent. `manager` is needed only when the
  chat has more than one.
- Each check (`AgentManager.check`) passes new questions to the chat once,
  as a message naming the worker and question; `ergo_agent_reply(worker_id,
  question_id, answer)` answers. `done` finishes the worker with the report
  as its result, which comes back as a message; `failed` fails it.
- `ergo_agent_stop(worker_id)`, or cancelling the worker, calls the
  manager's `stop` and cancels the worker.
- The worker card's log (`workers.log`) reads `AgentManager.log`. A check
  may also keep activity and state on the worker (`ctx.activity`,
  `ctx.state`), as Orca's does.

## Chats

Every user has a **main** chat with each bot (formerly the root session),
plus one chat for each named chat in `chats:`, created when first opened.
Main and named chats keep native history with token compaction and history tools over every session
the bot has with the user. A named chat adds its own `instructions` to
agents.md and loads its own skills. Threads are child sessions of any chat;
their history tools reach the same sessions, so a thread can read the
person's other chats with the bot.
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
- `status` (optional, both types): one line for thread lists, saying what
  the bot needs from the user or what it is doing now. Ergonaut shows it
  under the thread's title.

History stores each reply as readable text, with its suggestions, so later
turns and the history tools see the conversation as the user did. A tool
marked `requires_approval` pauses the turn (`result.approvals`), and
`bot.resume(session, decisions)` continues it. `ChatReply` and
`chat_reply_spec` live in `django_ergo.conversation.chat_reply` and work for
any chat session, not only bots.

The **main chat** and each named chat keep full native history, compacted
by tokens (see [compaction.md](compaction.md)), and have history tools over
every session this bot has with the user. `root.recent` and
`root.granularity` still parse but do nothing for bots;
`root.budget_tokens` sizes the per-turn context block. That block is
prepended to the current turn's user message, preserving a stable system
prompt for caching. The `orchestration` skill (loaded in main by default) has:

| Tool | What it does |
| --- | --- |
| `ergo_bot_list` | The bots it can message, with their `description`s (also in the context block below) |
| `ergo_thread_list` | A bot's main chat, named chats and threads with the user (default: this bot) |
| `ergo_thread_send` | Message a bot's `main` chat, a named chat, a thread id, or a `new` thread; returns at once. A busy thread queues the message (in order, as its next turn) instead of refusing it; optional `interrupt` replaces this chat's own running request there |
| `ergo_thread_forward` | Hand the user's own message (the one this turn answers) to a chat or thread, word for word with its author, time and files, plus an optional `note`. The recipient treats it as the user speaking and answers there; nothing comes back. Refused when the turn isn't answering the user |
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

### Message identity

Forwarded and cross-chat messages store the words separately from their
identity. `SessionMessage.role` remains the provider role (`user` for incoming
messages, including bot messages); it is not the person's identity.
`author` is an extensible JSON snapshot with `kind` (`django_user`, `bot`,
`telegram_user` or `system`), a string `ref` scoped by that kind, and
`display_name`. `provenance` records the message kind (`forwarded`, `message`,
`report` or `reply`), an `origin` snapshot (session id/label and timestamp,
plus original message id, sequence and structured-call id when available),
and, for a forward, `forwarded_by` (the sending bot's identity and chat).
Optional notes and shared files stay separate from the author's words.
Re-forwarding preserves the original author, origin and files; only the
latest forwarding chat and its note change.

Claude, OpenAI and history context generate attribution and routing
instructions from those fields at read time. The HTTP and SSE APIs instead
return the unprefixed body alongside `author` and `provenance`; the web app
shows the original author, forwarding bot, linked origin chat, original
time, note and files. `Bot.ask(..., author={...})` lets a trusted channel
identify its actual sender. Identity is attribution, not authorization:
session ownership and tool permissions are unchanged.

Migration `0034_sessionmessage_identity` adds two JSON fields with empty
defaults; it does not rewrite history or guess identities from old textual
preambles. Existing rows keep their prior rendering, while queued legacy
forwards use their existing structured `ThreadMessage` metadata when
delivered. This is groundwork, not shared-chat membership: participant
rosters, access control, invitations, identity linking and avatars are
not implemented.


Every turn of a chat with the `orchestration` skill loaded also gets a
**Bots and threads** context block (`django_ergo.bots.overview`), so it can
answer "what's going on?" without asking anyone:

- this bot and each bot it can message, with its description;
- under each, its main chat, named chats and open threads with this user:
  working, waiting for approval or idle, when it last moved, who started it,
  requests open in and out, and running workers
  (`orchestrator.thread_status(session)`, which UIs can use too);
- the latest five messages of each chat as short snippets (the newest gets
  more room; the current chat's are in its native history);
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
  the user, a worker, a reply or an approval, or has an open PR. A bot may
  resolve (or stop) its own threads and threads a chat of its own started on
  another bot (`orchestrator.can_resolve`); a parent can't close a sub-bot's
  self-started thread, since that could end work the sub-bot still tracks.
  The Bots and threads block marks quiet threads with nothing open as "ready
  to resolve" only where the viewing bot may resolve them; on other bots'
  threads it says "looks finished (devbox resolves it)". A refused call names
  who can resolve the thread. A thread resolves itself after its final report.
  A resolved thread keeps `resolved_by` and `resolved_summary` in its metadata
  until it reopens.
- **Reports upward get no reply.** A thread's message to its own main chat,
  or a main chat's to its parent's, is a one-way report: the recipient's turn
  starts with `[Report from …]` and its reply isn't sent back, so a status
  update doesn't cost the sender another turn for an acknowledgement. Pass
  `ask: true` (to `ergo_thread_send` or `ergo_message_up`) when an answer is
  needed.
- **Follow-ups are fine; status pings aren't.** A chat may send a thread
  several messages while its earlier request there is still open: they queue
  in order (see below), each is its own turn there, and each reply comes back
  separately, so `waiting_on` counts every open request. Nothing is refused
  because an earlier request is open. The one short-message rule left: in the
  turn that handles a chat's reply, a bot can't send that chat a short
  follow-up ("please continue", thanks): under 400 characters is refused
  unless the reply asked a question. A complete new request still goes
  through. The tool description, the reply header and the orchestration
  instructions tell bots to send follow-ups with something new in them, never
  a status ping.
- **A busy thread queues, never refuses.** A message to a thread that is
  mid-turn, waiting for approval or has earlier messages waiting is stored
  and goes out as its next turn, after the current one and in the order it
  was sent (one turn per message, so each keeps its own reply routing). The
  tool returns `status: "queued"` and the `queue_position` (1 = next); an
  idle thread returns `status: "sent"`. A queued message stays `queued` (so
  `ergo_thread_stop` cancels it) until its turn starts, and queues are sent
  on whenever the thread's turn ends, whether it completed, failed or was
  stopped. `ergo_thread_send` never interrupts the thread's turn unless
  `interrupt: true`, and that only replaces a request *this chat* sent that
  the thread is working on right now: a turn that answers the user (their
  message, a forward of it, a schedule or a worker) or another chat is never
  stopped by a bot, and the message queues behind it (the result says
  `interrupted: false` and why). It also needs `TURN_STOPPER`. Messages the
  user types in Ergonaut keep their own path: they steer a running turn, or
  `interrupt` it, whoever started it.
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
made the calls. They are seeded once per session, and again on the first
turn after compaction folds the prior seeded call (window chats from
`conversation.window`, whose model calls carry only the current turn, seed
every turn). `FunctionToolkit(tools, ctx, seed=["tool_name"])` seeds
zero-argument tools. Skills are listed in the per-turn `Skills` context
section; the bots a chat can reach are in the "Bots and threads" context
block instead.

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
    def worker_functions(self): return {}             # "<plugin>:<name>" workers
    def agent_managers(self): return {}               # where coding agents run (Agents)
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
  root_only: false     # true: top-level chats only, not threads
  run:                 # optional: ergo_config_repo_run
    approve: true      # each run waits for approval (default)
    timeout: 300
    commands:
      toolkit-test: {argv: [npm, test], cwd: ficsit/toolkit}
      node: {argv: [node], cwd: ficsit/toolkit, args: true}
```

Lets the bot maintain the git repository its folder lives in, so it can
change its own instructions, config and tools, or add new bots.
`ergo_config_repo_status`, `ergo_config_repo_list`, `ergo_config_repo_read`,
`ergo_config_repo_grep` and `ergo_config_repo_diff` inspect the files.
Use `ergo_config_repo_edit` for an exact, targeted replacement in an existing
file; `ergo_config_repo_write` creates or replaces a whole file. Ranged reads
return numbered lines, and all paths can't leave the repo or touch `.git`.
`ergo_config_repo_discard` throws unpublished edits away.

In `merge_main` mode the bot edits the checkout it runs from, and
`ergo_config_repo_publish` commits, rebases on main and pushes. In `propose_pr` mode it
edits a draft instead: a separate git worktree of main kept inside `.git`,
so the running bots don't change until you merge. `ergo_config_repo_publish` then rebases
the changes onto the latest main, pushes a `bot/<name>/<time>-<title>` branch and opens a
pull request with `gh`; if the changes conflict with main it refuses and names the files.
A draft with no changes follows main, so work after a merge starts from the merged
version, and `ergo_config_repo_status` says when main has moved past a draft with changes.
Publishing needs approval unless `approve_publish: false`. `ergo_config_repo_pull`
fast-forwards main and `ergo_config_repo_prs` lists open pull requests. Changes take
effect when the bot is loaded again (Ergonaut reloads changed bot files on
its own, and with `ERGONAUT_BOTS_PULL_SECONDS` pulls merged changes too).
For review screens the plugin also has `draft_diff()`, `pull_requests()`,
`pull_request_diff(n)`, `merge_pull_request(n)` (squash-merge, then pull) and
`close_pull_request(n)`; Ergonaut's bot page uses them for its Changes section.

With `run.commands`, `ergo_config_repo_run(command, args)` runs one of those
commands in the draft, so the bot can test code it wrote before publishing
it. Commands are argv lists (no shell) with a `cwd` in the repo; only one with
`args: true` takes the bot's arguments. They come from the live bot.yaml, not
the draft. A run gets a bare environment (`PATH`, `HOME`, `LANG`, `CI=1`, none
of the server's secrets), stdin closed, the timeout and trimmed output with the
exit code. It still runs draft code as the user Ergonaut runs as, so each run
waits for approval unless `approve: false`.

### orca

```yaml
- name: orca
  environment: devhost        # every call is pinned to this Orca environment
  executable: orca-ide       # default: orca-ide if installed, else orca
  files_host: devhost        # SSH host holding worktrees; "" means local
  usage_minutes: 10          # minimum minutes between agent session scans
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
It is also the `orca` agent manager (see [Agents](#agents)): `agents`
(default `[codex, claude, omp]`) lists what `ergo_agent_start` may start, and
loading the `orca` skill loads `agents` with it.

### kubectl

```yaml
- name: kubectl
  clusters:
    cluster-name:
      kubeconfig: /mounted/kubeconfig
      namespace: default       # optional default for calls without -n/--namespace
  approve: true                # required; every kubectl_run call waits for approval
  timeout: 120
  root_only: true
```

Runs the `kubectl` binary only against named clusters configured in the bot
file. `kubectl_read(cluster, args)` needs no approval, but permits only
`get`, `describe`, `logs`, `top`, `events`, `explain`, `api-resources`,
`version`, and `auth can-i`; aliases and packed shell-style arguments are not
accepted. Every `kubectl_run(cluster, args)` call needs individual approval;
`approve: false` is rejected. Before approval, `apply`, `patch`, `delete`,
`scale`, `rollout`, `label`, `annotate`, and `create` first run against the
selected cluster with `--dry-run=server`; `apply` and `patch` also run
`kubectl diff`. The approval request shows bounded, redacted output. `exec`
and `rollout restart`, `undo`, or `status` explicitly state that their preview
is skipped because Kubernetes has no safe cluster-state preview for them.
Arguments are always an argv list. The plugin pins the selected cluster's
kubeconfig and rejects flags that could change the kubeconfig, context, server,
or identity. It redacts structured `data`, `stringData`, and credential-shaped
output and refuses `get secret` custom formats, so Secret values are never
returned.

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

### browser

```yaml
- name: browser
  cdp_url: http://127.0.0.1:9222   # Chrome's --remote-debugging-port, as ssh_host sees it
  ssh_host: rigel            # optional: reach cdp_url through ssh -L to this host
  takeover: the Chrome window on rigel  # where a person signs in for the bot
  approve_actions: true      # clicks, typing and key presses wait for approval
  root_only: true
  timeout: 30                # seconds per browser call
  max_snapshot_chars: 20000
```

Attaches to a running Chrome over CDP with Playwright (the `browser`
extra) and gives the bot `ergo_browser_tabs`, `ergo_browser_open`,
`ergo_browser_snapshot` and `ergo_browser_screenshot` with no approval, and
`ergo_browser_click`, `ergo_browser_type` and `ergo_browser_press`, which
wait for approval unless `approve_actions: false`. Snapshots are
accessibility trees whose refs (`e12`) the actions take; a Playwright
selector works too. The bot is told to hand sign-ins, two-factor codes and
captchas to the user in `takeover`. With `ssh_host`, each call opens
`ssh -N -L` to that host for its duration. Setting up Chrome and SSH:
[Browser control](browser.md).

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
Only the latest two images stay in what's sent to the model (plus those the
bot's newest round of tool calls just returned); older ones
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

The incoming message's `author` records the actual Telegram sender id and
name when Telegram provides them, independently of the Django account used
to route the chat. Forwarding preserves this `telegram_user` identity.

### Model context windows

In `providers.yaml`, a model may set `context_window` directly or inside
`config`. For example:

```yaml
providers:
  subscription:
    type: claude
    transport: cli
    models:
      - name: claude-opus-5-5[1m]
        context_window: 1000000
```

When unset, `[1m]` names use 1,000,000 tokens and all other names use 200,000.
The CLI receives the model name unchanged; display labels and price lookup
strip the suffix. Native compaction defaults to 75% of that window and
keeps 25% verbatim. `max_context_tokens` remains a threshold alias;
`rolling`/`stream` map to `context_size` and ignore message-count keys.
