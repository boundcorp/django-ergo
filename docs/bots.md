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
icon: "🍳" # shown before the name in Ergonaut (default: its first letter)
color: amber # a palette name or hex like "#f59e0b" (default: picked from the name)
instructions: agents.md # default
engine:
  type: openai # or claude (optional); default is the settings engine (openai)
  config: { model: gpt-6-luna }
  api_key_env: KITCHEN_OPENAI_KEY # read at runtime, never stored
  # with a providers.yaml: config: {model: openai/gpt-6.1-sol}
  # transport: cli                   # the logged-in Claude Code or Codex CLI, no key
  # a bot in a private pool's allowed_bots: config: {model: personal/heavy} (see Private inference)
root: # context budget for main and named chats
  recent: 15 # legacy; ignored by bots
  budget_tokens: 8000
  granularity: conversation # legacy; ignored by bots
orchestration: true # may the bot delegate at all (false: never)
private: true # no other bot may message it or read its chats and files (default false)
timezone: America/Los_Angeles # default for users without a timezone
current_time: true # current time in every turn, send time on each user message
todos: true # the todo list tools in every chat and thread (false: none)
tool_results_in_context: 6 # large tool results each model call keeps in full
tool_results_tokens: 40000 # optional budget for older ones; default 20% of model window
chats:
  main: # every user's main chat (always there)
    skills: [orchestration, tandoor] # loaded from the start (default: [orchestration])
    compaction: { compact_at_tokens: 80000, keep_tokens: 25000 } # optional; default 75% / 25% of the window
  reports: # a named chat: one per user, its own purpose
    description: Weekly reports
    instructions: Keep each report short. # added to agents.md in this chat
    skills: [analytics]
    retired: false # true hides it from navigation and disallows schedule targets
threads: # child threads (`sessions:` also works)
  skills: []
  allow_create: true # may chats (this bot's or other bots') start threads of it?
  archive_after_days: 7 # archive threads idle this long (0 = never)
  default_compaction: {
      mode: context_size,
      config: { compact_at_tokens: 150000, keep_tokens: 50000 },
    } # optional; window-relative defaults
skills:
  folder: skills # default
  unload_after_turns: 30 # drop a loaded skill unused this many turns
  requires: { meal-planning: [tandoor] }
  include: [skillbuilder] # skills from Ergo's library (docs/skills.md)
  exclude: [skillbuilder] # leave out some of DJANGO_ERGO["DEFAULT_SKILLS"]
  defaults: true # false: none of the default skills
tools: [tools/tandoor.py]
toolkits: ["myapp.toolkits:make_toolkit"] # factory(ctx) -> Toolkit or list
plugins:
  - name: ergo_kb
    knowledgebase: Kitchen
  - name: myapp.plugins:AuditPlugin
pull_requests: [boundcorp/ergo-bots] # repos whose open PRs orchestrating chats see
permissions:
  call_bots: [sysadmin] # other bots this bot may message
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
    cron: "0 17 * * sun" # minute hour day-of-month month day-of-week
    message: Propose next week's dinners with the meal-planning skill.
    to: main # main (default), a named chat, or a new thread:
    # to: {thread: "Reports {n}", in: main}   # {n} run number, {date:%b %d} and strftime codes
  - name: weekly-stats # or an ordered list of actions
    cron: "0 8 * * mon"
    actions:
      - run: tools/analytics.py:pull_stats # a function in a .py file in the bot folder
        args: { days: 7 } # it may take ctx (a ToolContext) first
        stop_if_empty: true # returns nothing: skip the rest, quietly
      - prompt: "Summarize last week: {result}" # {result}: the last run step's value
        to: { thread: "Stats {date:%b %d}" }
    users:
      [lee] # default: permissions.users, else everyone with a chat
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
the rest. A `prompt` step is queued as an automated event from
`Schedule: <name>`, not as a message authored by the person who owns the chat.
The bot answers in a turn of its own under an `[Event from Schedule: <name>:
automated notification, not a message from the user. …]` header. The reply
stays in that chat, and the Telegram plugin passes it on for a main chat.

Use `workflow:` instead of `actions:` for context-aware Python checks and
safe conditional prompts:

```yaml
schedules:
  - name: weekly-model-discovery
    cron: "0 9 * * sun"
    timezone: UTC
    workflow:
      - python: ergo.models.check_available
        save_as: models
      - if: models.notify
      - prompt: "Review {models}; propose catalog updates as a PR, never auto-enable."
        to: { thread: "New models available {date:%Y-%m-%d}" }
```

`python` selects the registered core check or a trusted bot file callable
(`tools/check.py:check`), receiving the context dictionary and optional `args`.
`save_as` exposes its result to later conditions and `{name}` prompt templates.
False `if` ends silently without a chat message or LLM turn. Conditions read
JSON data only; no function calls or arbitrary Python evaluation.
`llm: false` on a prompt posts without invoking a model. `timezone` pins the
cron and date templates to an IANA timezone instead of the person's timezone.
Runs retain status, step results, timestamps, and sanitized errors; failures
notify main without an LLM turn. Model checks use only catalogs/list endpoints,
persist an idempotent diff, and never change routing. Their first successful
run seeds the catalog for that bot folder/name, user, and schedule, returning
`baseline_seeded: true`, `model_count`, and a summary such as
`"Baseline seeded, 234 models"` instead of dumping every model as new.
It sets `notify: true` once with empty diffs/flags; subsequent unchanged runs
are silent and later real changes produce ordinary diffs. A separate bot table
refresh does not establish this baseline; existing core baselines are preserved.
Private models and quarantined providers are not coding/shared-routing
candidates. Coding flags also require explicit `kind: chat` metadata; non-chat
and missing-kind records fail closed, even when priced. Task tokens in model
IDs (for example `realtime` or `tts`) also exclude coding when an upstream cache
mislabels them as chat. Discovery flags and a
deployment's `ModelsAvailable` table are not core worker execution allowlists:
explicit worker models and configured agent tiers do not consult them.
See [Schedules](schedules.md#workflows) for the complete format and safety rules.

## Skills

Everything a bot can do beyond answering is a **skill**, and every skill
loads the same way (`django_ergo.bots.skillset`):

- a skill folder: `skills/<name>.md` or `skills/<name>/SKILL.md`, optionally
  with `skills/<name>/tools.py`;
- a tool file from `tools:` (`tools/tandoor.py` is the `tandoor` skill; its
  module docstring is the description);
- a plugin that adds tools (`kb`, `config_repo`, `orca`, `bash`, `browser`,
  `attachments`, ...) and each `toolkits:` factory;
- built-ins: `history` (always loaded), `workers`, `todos` (always loaded,
  see [Todo lists](#todo-lists)), `orchestration`, `models` (see
  [Model choice for tasks](#model-choice-for-tasks)), and `introspection` for
  bots loaded from a folder;
- Ergo's skill library (`skillbuilder`), when bot.yaml or a skill names one,
  or by default (`DJANGO_ERGO["DEFAULT_SKILLS"]`, unless bot.yaml excludes it).

`introspection` is read-only and needs no plugin, so every bot can see what
it's made of, whether or not it can change its repository (that's
`bot_management`): `ergo_self_overview` (folder, model, skills and their
sources, plugins with secret-looking config hidden, tables, schedules, chats,
sub-bots), `ergo_self_files` and `ergo_self_read` (files in the bot folder, by
line range; `ergo:` paths read Ergo's own source, e.g.
`ergo:plugins/attachments.py`). Paths cannot escape their respective folders;
for other files in the bot's config repository, use the `config_repo` skill's
`ergo_config_repo_list` and `ergo_config_repo_read` tools if available. Hidden
files such as `.env` are never listed or read.

```markdown
---
name: meal-planning
description: Plan a week of dinners from the recipe library
requires: [tandoor] # load these too
always_load: false # load in every chat from the start
plugins: { bot_management: { mode: propose_pr } } # plugins it needs, with settings
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

A bot with tables gets the `tables` skill: `ergo_table_schema` describes
writable fields and unique keys; `ergo_table_query` and `ergo_table_aggregate`
read bounded results; `ergo_table_add`, `ergo_table_update` and
`ergo_table_batch` validate writes. `ergo_table_delete` waits for approval.
Loading the skill describes each table's fields; a model's docstring is its
description. See [Data tables](tables.md#what-the-bot-can-do).

## Pages and pins

A `.jhtml` file is a live page: a Jinja template rendered over the bot's
tables each time it's opened (`django_ergo.bots.pages`).

```html
<h1>Ad spend</h1>
<p>
  {{ table("AdStat").filter(date__gte=days_ago(7)).sum("spend") | money }} this
  week
</p>
{{ blocks.metric(label="Installs", table="AdStat", aggregate="sum",
field="installs") }} {{ blocks.chart(table="AdStat", x="date", y="spend",
group="campaign") }} {% for row in table("AdStat").order_by("-date").limit(10)
%}{{ row.campaign }} {{ row.spend | money }}<br />{% endfor %}
```

Pages run in Jinja's sandbox and can only read: `table(name)` is a view with
`filter`, `exclude`, `order_by`, `limit`, `count`, `sum`, `avg`, `min`,
max`, `group(...)`, `first`and`rows`, never a queryset. `blocks.*`(heading,
markdown, metric, form, table, button, chart, html) render common pieces;`now`, `today`, `days_ago(n)`, `user`, `bot`and the`money`, `number`,
`percent`and`markdown`filters are there too, and`{% include %}`loads other files from
the bot folder. Rows give dates as ISO strings, so for date math use`as_datetime`(back to a datetime),`seconds_until`(seconds from now,
negative once past) and`duration`(seconds as`2h 15m`):
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
<button
  onclick="ergo.call('restock', {item: 'flour', qty: 2}).catch(e => alert(e.message))"
>
  Restock
</button>
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
  viewer repeats the call with it on Yes. The token never reaches the page,
  doesn't work for other arguments, and works once (its nonce is kept in
  Django's cache). Use it for anything that spends money,
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
{{ blocks.form(table="Pantry", fields=["name", "quantity"], submit="Add item")
}} {{ blocks.table(table="Pantry", columns=["name", "quantity"], edit=true,
delete=true) }} {{ blocks.button(label="Review the list", ask="What should I buy
next?", args={"chat": "main"}) }}
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

In a chat, the viewer also reloads an open file when a new message arrives,
except audio and video, so a recording or clip keeps playing while the bot works.

A viewer that was disconnected catches up with one event when it reconnects.
So pages don't need to poll or reload on a timer.

Pins open in a new tab from the viewer at `/pages/view`, which hosts the same
viewer, so the bridge works there too.

## Workers

A **Worker** is long-running work a chat or thread started: a build, a data
pull, a coding agent in Orca. The tool that starts it returns at once, the
chat shows as busy (a spinner in the sidebar, a strip above the transcript
with the worker's latest progress), and when the worker finishes its result
comes back as an automated event from `Worker: <title>`, so the bot follows
up with a reply (`django_ergo.bots.workers`, model `Worker`). The worker's
updates, questions, completion and failure notices are not human-authored
messages, even though they wake a turn in a person's chat.

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
  restarts overdue workers). A step holds its worker while it runs, so a late
  or duplicate runner call skips it; a step whose process died is taken over
  after 30 minutes. `ctx.state` is a dict kept between polls,
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
(`modelRoles`, git-ignored by its own folder) and omp picks it up. Every
chat role (`default`, `smol`, `task` subagents and the rest) is set to that
model, with the effort on the roles that do the agent's work, so no role
falls back to the host's omp config and spends another subscription. With `tier: <name>` instead (small, medium, large, xlarge, or a
custom name), the agent, model and effort come from the `agents` tiers in
providers.yaml, by the best safe subscription pace score, prioritizing unused
weekly quota near reset while respecting shorter windows (see
[Routing by tier](building-bots.md#routing-by-tier)). Each chat keeps one Orca mailbox terminal and Run for its
workers; if Orca no longer knows them (their worktree was removed, Orca was
reset), starting an agent makes new ones and tries once more. If a worker's agent
terminal exits or vanishes without a `worker_done` (Orca keeps such a dispatch
"dispatched"), the watcher fails the worker after five minutes and tells the
chat why. When a worker settles, the watcher closes its agent terminal
(`orchestration worker-release`, or `worker-stop` if Orca hasn't settled the
dispatch yet), and `ergo_agent_stop` runs `worker-stop`, so finished workers
don't leave idle agent CLIs holding memory on the Orca host.

Follow-up Orca guidance uses the `orca` skill's `ergo_orca_instruct` and
`ergo_orca_instruction_status`, not raw mail or terminal input (see
[instruction checkpoints](plugins.md#orca-instruction-checkpoints)).
Each instruction has a durable id/version and explicit supersession. Enqueue
persists an immediate watcher wake in `next_poll_at`, including during a running
step, so a later normal poll does not overwrite it. This preserves the request,
not a wall-clock delivery deadline or an interruption of an active step.
The watcher sends instructions before question replies, rechecks Orca lifecycle
under the local worker lock, and closes queued mail before terminal release.
Completion, cancellation and watcher failure classify unattempted queued mail
as `unsent`; interrupted sends remain `unknown` and are not replayed.
This fences locally observed completion, not a remote completion racing the
separate preflight/send calls. A status acknowledgement's sender/pane fields
are not attested by the current Orca protocol: even matching claims remain
`acknowledgement_claim` with `verification: "unverified"`, authenticated
`acknowledgement` stays `null`, and model `acceptance` stays `unknown`.
Transport consumption and claimed next actions never prove model acceptance.

The watcher also scans its agent's session files on the worktree host every
`usage_minutes` (default 10) and once after settlement. Claude Code, Codex,
and omp tokens appear per worker on Costs, grouped by project: the scan also
runs git in the worktree and saves the repository (name, canonical remote,
branch and the same `project_key` as agent history) in the worker's
`state["project"]`, so every worktree of one remote counts as one project.
`ergonaut manage agent_projects` looks it up for older workers (a worktree
that's gone is recorded as unresolved, named after its folder). Set
`files_host` to the SSH host that holds the worktrees, or `""` to scan files
locally. It is best effort: an unreadable session file preserves the last
recorded counts and does not fail the worker. Counts are attributed by
worktree and the worker time window, so overlapping workers in one worktree
can each include the same agent requests.

## Todo lists

Every chat and thread keeps its own todo list, like Claude Code's TodoWrite
(`django_ergo.bots.todos`). The built-in `todos` skill is always loaded, so
the model can plan multi-step work and check it off as it goes:

- `ergo_todo_write` replaces the whole list with `[{content, status}]` items
  (`status` is `pending`, `in_progress` or `completed`); an empty list clears it.
- `ergo_todo_update` changes one item by its number (from 1): its status, its
  wording, or both.

The list is kept in the session's metadata (`todos`), is in the model's
context on every turn while it has items, and Ergonaut shows it in the thread
view behind a "Todo X/Y" button beside Illustrate. `todos: false`
in bot.yaml leaves the skill out.

## Self-wakes

A bot can schedule one later turn of its own in a chat or thread
(`django_ergo.bots.wakes`), for work it can't finish now and can't hand to
anyone: waiting out a rate limit ("try again in a few minutes") or a long
external job. The built-in `wake` skill is loaded in every chat and thread:

- `ergo_wake_later(note, delay_minutes | at)` schedules it 1 minute to 24 hours
  ahead (`at` is ISO 8601; without an offset it is in the user's timezone).
  The note is for the bot's future self: what it was doing and what to retry.
- `ergo_wake_cancel()` cancels the chat's pending wake (and one that fired
  but is still queued behind a busy turn).

When it is due, the chat gets an automated event from "Self-wake" (not the
user) with the note and whether the user wrote since it was scheduled, and the
bot carries on. It is not for waiting on a person, reminders or a worker's
result (that comes back by itself).

Each wake is a `Wake` row (in the admin), with limits so it can't loop:

- It fires at most once: `fire_due` claims the row (pending to fired) in the
  same transaction that queues its message, so a duplicate task, a double beat
  tick or a restart finds it already fired. The message is a thread message,
  which waits for a busy chat and is itself delivered once.
- A failure is final: a send that raises, a delivery that fails or a turn that
  ends without an answer marks the wake `failed`, and nothing retries it.
- One pending wake per chat (a database constraint).
- At most 3 wakes in a row: a wake scheduled after one fired counts as one more
  unless the user wrote (or a bot.yaml schedule prompt arrived) in between.
  Replies from other chats and worker results don't reset it, since the bot can
  cause those itself. Past the cap the tool refuses and the bot tells the user.
- The wake's message keeps the delegation depth of the message the scheduling
  turn answered, so a wake never resets the cap on hops between chats: threads
  that wake and start more threads stop at the usual limit.
- A human message doesn't cancel a pending wake.

The pending wake is in the model's context each turn. Ergonaut fires due wakes
from beat every 30 seconds (`ergonaut.fire_wakes`) and shows the pending one
above the composer and in the phone session bar. Without Ergonaut, call
`django_ergo.bots.wakes.fire_due()` every 30-60 seconds.

### Recurring checks

For an explicit user request such as “check this chat every 5 minutes for the
next 10 hours”, the same built-in skill provides `ergo_wake_recurring`,
`ergo_wake_recurring_list` and `ergo_wake_recurring_control`. A finite expiry or
maximum occurrence count is mandatory; minimum interval and per-thread/global
quotas are configurable. Pause/resume/cancel and next run/expiry/count/status
are visible in the chat. Busy and approval-waiting threads coalesce checks;
final failures block rather than retry automatically. Unchanged updates stay
quiet unless periodic check-ins were requested. These are ordinary turns, not
new permissions, approval grants or financial authority. Worker/task results
continue to arrive naturally. See [recurring thread wakes](recurring-thread-wakes.md)
for settings, timezone and cancellation semantics, durability and acceptance.


## Agents

A coding agent (Codex, Claude Code, omp) works on a brief for minutes to
hours, on a subscription and never an API key. An **agent manager** is
whatever can run one: the Orca plugin and the `local_shell` plugin (job
folders on this host) today; a host over ssh is next. A plugin adds managers from `agent_managers()`:

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

The optional `checkpoint(ctx, handle)` hook runs before question replies, so
control-plane mail can be sent before a slow reply. Keep it bounded. A pending
or uncertain reply does not suppress the following `check`; it is not evidence
that the agent has accepted the answer.

A bot with a manager has the `agents` skill, which loads the `models` skill
with it (see [Model choice for tasks](#model-choice-for-tasks)):

- `ergo_agent_start(brief, workspace, title, tier | agent/model/effort,
manager)` asks the manager to start the agent and starts a polling Worker,
  `agent:<manager>`, with the handle as its argument. With `tier`, the agent,
  model and effort come from providers.yaml's `agents` tiers. Built-in
  small/medium/large/xlarge lists are derived from listed subscription catalog models
  even without YAML tier declarations; `agents.<name>` replaces that
  name's list, and custom names are supported. Chat `tiers.<name>` overrides
  are independent. Restart Ergonaut and its bot workers after editing
  providers.yaml (see [Routing by tier](building-bots.md#routing-by-tier)).
  Routing picks only among the tier's candidates the manager runs (list
  `claude` and `codex` candidates for `local_shell`, `omp` for Orca), so a
  used-up subscription falls back to another one. `manager` is needed only when the
  chat has more than one.
- Each check (`AgentManager.check`) passes new questions to the chat once,
  as a message naming the worker and question. `ergo_agent_reply(worker_id,
question_id, answer)` queues the answer for the worker and returns without
  waiting for the manager; repeated answers to the same question are rejected,
  and so is an id the agent never asked (the error names its open question).
  The worker reports delivery errors and continues polling for the next question
  or report. `done` finishes the worker with the report as its result, which
  comes back as a message; `failed` fails it.
  If delivery is interrupted, its outcome may be unknown: the worker reports
  this and keeps waiting rather than risk delivering the same answer twice.
  An operator must check the agent before intervening.
  While an earlier reply remains uncertain, checks still observe status but defer
  surfacing and marking later questions, preserving the original question context.
  A terminal result closes queued replies as unsent rather than scheduling another
  delivery after completion; interrupted replies retain their uncertain outcome.
  Orca workers retain their chat-owned mailbox and run in the dispatch handle:
  a newer dispatch replacing the chat's current mailbox does not change an older
  worker's sender. Orca still verifies the run's current consumer; a fenced or
  missing mailbox requires durable-question reconciliation, not an automatic
  resend or rebinding. Legacy handles use the chat mailbox only for a matching
  run. If delivery times out, reconcile before any manual retry. A worker
  terminal is never used as the sender.
  A successful Orca reply reports only that the answer was queued: neither a
  transport read flag nor `delivered_at` proves that the model accepted guidance.
  For follow-up changes of direction, use `ergo_orca_instruct` and inspect
  `ergo_orca_instruction_status` from the `orca` skill. They maintain versioned
  instructions, explicit supersession, and separate worker acknowledgements
  naming the instruction and next action. See [instruction checkpoints](plugins.md#orca-instruction-checkpoints)
  for delivery limits and the cooperative urgent-stop protocol.
- Agent worker descriptions include a `dispatch_receipt`: requested and selected
  agent/model/effort, and only quota windows applicable to the selected model
  when tier routing provided them. Window names are restricted to known public
  names (`other` otherwise); arbitrary provider labels are not copied. Stale
  observations remain marked stale, and custom-router reasons stay unknown.
  Launch-effective model and billing mode remain unknown unless independently
  reported. Worker step claims
  count watcher steps, not model calls. Usage rows link observed models by worker;
  list-price estimates are not billed amounts, and unpriced rows are excluded.
  An exited question requiring `needs_new_run` is shown as “Needs new run” in
  both the collapsed transcript disclosure and expanded worker card, not as a
  successful task completion.
- `ergo_agent_stop(worker_id)`, or cancelling the worker, calls the
  manager's `stop` and cancels the worker.
- The worker card's log (`workers.log`) reads `AgentManager.log`. A check
  may also keep activity and state on the worker (`ctx.activity`,
  `ctx.state`), as Orca's does.


## Model choice for tasks

Every bot has the built-in `models` skill, loaded with `agents` and with the
browser plugin. Its `ergo_models_list(kind)` lists what this bot may hand a
sub-agent or task it starts, and what routing would pick for each tier now:

- `agent_tiers`: the `agents` tiers of providers.yaml, for `tier` on
  `ergo_agent_start` and `orca_start_worker` (local_shell jobs too).
- `model_tiers` and `models`: `auto/<tier>` and the public `provider/model`s,
  for a task that runs an Ergo model, like `ergo_browser_task(model=...)`.
- For a bot in a private pool, only that pool's members and no agent tiers.

`bots.model_choice.check(bot, model, kind)` is the one rule for a model
argument, with `kind` `agent` (a coding agent CLI) or `model` (an Ergo
engine call). It refuses a private model for any other bot, an unknown
tier or model for a `model` task, and for a bot in a private pool anything
outside its pool (and every coding agent). A bot's own tools that start
tasks call it the same way and list `models` in their skill's `requires`:

```python
from django_ergo.bots import model_choice

model = model_choice.check(ctx.bot, model, model_choice.MODEL) or "auto/small"
```

## Chats

Every user has a persistent, top-level **main** chat with each bot, plus
one non-completed chat per named entry in `chats:`, created when first opened.
Named chats add their own `instructions` to agents.md and have their own
skills and pinned files. Task-scoped threads are children of a chat, have no
chat-specific instructions, and may be auto-resolved when idle. Both keep
history, and history tools can reach the user's other sessions with the bot.
Instructions are rebuilt every turn, so edits to agents.md and bot.yaml reach
existing chats. `bot.main_session(user)` and `bot.chat_session(user, name)`
open them (`root_session` still works).

Set `retired: true` on a named chat to hide it from navigation and prevent
new schedules from targeting it, without deleting its session, files or
threads. Existing links and history still work, and
`bot.chat_session(user, name)` still opens it for integrations. Removing a
chat from bot.yaml also preserves existing sessions and their history, but
removes their chat-specific configuration; prefer retirement while code
still refers to the chat by name.

To turn an existing thread into a named chat without changing its ID, see
the [adoption and rollback commands](building-bots.md#chats-and-threads).

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
`current_time: false`, and each user message then goes to the model with
a `[Sent: ...]` line in the same timezone, so the bot can tell how long
ago something was said.

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
- `attachments` (optional): files to show with the reply, by id or
  filename (the chat's own files or ones shared with it). Ergonaut shows
  several images or videos as a gallery that opens into a slideshow, and
  audio and video with players.

If the model answers in plain text instead, that text is taken as a
`message` reply (no suggestions or status) and stored as a `send_reply`
call, so the user gets it without a second model call and later turns only
see replies given through the tool.

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

| Tool                  | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ergo_bot_list`       | The bots it can message, with their `description`s (also in the context block below)                                                                                                                                                                                                                                                                                                                                                                                                |
| `ergo_thread_list`    | A bot's main chat, named chats and threads with the user (default: this bot). Use `query` to find a specific thread by title, chat name or ID prefix; all words must match, and resolved threads are included by default. Without a query only active threads are listed; `include_archived` overrides that choice. Results are newest first, limited to 50 by default and at most 50; use `limit` for fewer. Read a known thread ID directly with chat history instead of listing. |
| `ergo_thread_send`    | Message a bot's `main` chat, a named chat, a thread id, or a `new` thread; returns at once. A busy thread queues the message (in order, as its next turn) instead of refusing it; optional `interrupt` replaces this chat's own running request there                                                                                                                                                                                                                               |
| `ergo_thread_forward` | Hand the user's own message (the one this turn answers) to a chat or thread, word for word with its author, time and files, plus an optional `note`. The recipient treats it as the user speaking and answers there; nothing comes back. Refused when the turn isn't answering the user                                                                                                                                                                                             |
| `ergo_thread_stop`    | Stop the running turn of a thread this bot started (or one of its own), or end its turn that waits for approval (declining those tool calls), and cancel what it queued there                                                                                                                                                                                                                                                                                                       |
| `ergo_thread_resolve` | Resolve a finished thread (this one, one of this bot's, or another bot's it started), with a one-line `summary`; refused while workers run, a request is open, an approval is pending, or its last reply asks the user something. History stays readable; a new message reopens it. `ergo_thread_archive` is a deprecated alias                                                                                                                                                     |

List entries and successful send, forward, upward-message, stop and resolve
results include `session_id`, `title` (the bot-and-chat display label), and
`bot` for the target. Send and forward retain `thread_id` and report delivery
`status` (`sent` or `queued`, with `queue_position` when queued). List entries
retain `id` and their session `status`. Resolve and its archive alias return an
object with `status: "resolved"` and the original confirmation text in `note`;
older stored results may be plain text. A refused call that concerns a known
thread (stop or resolve refused, send to the chat itself, a nudge right after a
reply) fails with a JSON object: the reason in `error`, plus the same
`session_id`, `title` and `bot`; other failures stay plain text.

Text one agent writes for another must read as plain sentences. Some models
drop the spaces between words when they write for an agent rather than a person
("Measured openingboundaryconfirmed: deliveredVO003guestline ends14.35s"), so
`ergo_thread_send` and `ergo_message_up` messages, `ergo_thread_forward` notes,
`ergo_agent_start` briefs and `ergo_agent_reply` answers with several
run-together words are refused with a request to rewrite them
(`django_ergo.bots.plain_text`). Paths, URLs, ids, hashes, file names and
hyphenated compounds don't count.

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

### Budgeted turns

A thread message can carry a hard budget for the turn that answers it
(`messaging.send(..., metadata=budget.attach({}, TurnBudget(...)))`, or
`Bot.ask(..., budget=...)`). It is opt-in: a message without one runs exactly
as above. The turn runner then holds the turn to a number of model turns,
input and output tokens in total, a wall-clock deadline and a list of allowed
tools (`django_ergo.conversation.budget`); it makes no retries (its own or the
SDK's), no compaction and no model routing, never pauses for approval (a tool
that needs one is refused), and refuses worker, agent and background-task
starts and thread sends even from an allowed tool. Only the Claude and OpenAI
API engines can hold a request to a budget; a subclass has to claim that
itself, so the subscription CLIs refuse a budgeted turn before any request.
When a limit is hit the call ends `failed` with `error_category` `budget`,
the message ends `failed` with an error that says which limit, what was used
and that there is no final answer, and `call.metadata["budget"]` keeps the
numbers. A budget that can't be read, or whose start window passed while the
chat was busy, fails the message ("Not run: ...") rather than running it
unbudgeted. The realtime voice prototype is the user:
[realtime-voice.md](realtime-voice.md) has the limits table and conditional
cost estimate, not a provider-billing ceiling. Its client-delegation voice
adapter has no function tools; provider signals open a pending request that
is queued after a detected speech boundary, pause grace and transcript drain.
This is not a guarantee that speech or transcription is complete.
The session card distinguishes generated speech from
durable submitted/queued/answered/failed work and explicitly reports when
no background request or verified history result exists.

Voice is an interface to its exact launch thread, not a separate orchestrator
or a global owner briefing. PR #293 (deployed `06284d4`) restores that thread's bounded
instructions, history/summary, decisions and attachment references. Startup now
also carries a bounded timestamped exact-thread work/current-session evidence
snapshot (three active workers and two recent request/result summaries);
recorded status is not liveness or model consumption. Unrelated threads remain
on-demand through the owning bot's authorized tools. Budgeted voice is restricted, and a
subscription/CLI launch thread cannot silently switch to an API model to run
delegation. The successor's **default-off** normal-thread execution policy is
separate from the priced voice lookup: exact chat, owner, bot and a reviewed
normal-task-cost approval reference are required in settings and in the durable
session grant. It uses the existing normal bot engine/tools/context and action
approval path, never voice-approved actions or globally granted tools. Accepted
normal work uses durable provider-invocation claims; an uncertain external write
is not automatically repeated. Causal thread updates retain the original voice
delegation ID, including across reconnect and after End. No live policy is enabled
by this source change; ordinary non-voice turns and budgeted-turn guards are unchanged.
The normal grant defaults to one request. An exact scope may explicitly approve
`max_requests: 3` with `followup_costs_approved: true` and a separate
`followup_approval_reference`: three sequential execution attempts per logical
call, never parallel roots, unlimited inference or increased provider duration.
Only fresh sessions receive changed grants. Expired attempts count; reconnect
cannot reset allowance or the original 90-second deadline. Status observation
and persisted-result reading use no execution slot or new inference; the UI
shows remaining allowance and an owning-text-thread continuation at exhaustion.
Handler acceptance, result persistence and an announcement claim remain distinct
from actual model execution/consumption, playback and physical hearing.
See the [current release and capability evidence](realtime-voice-causal-review.md#current-release-reconciliation--2026-11-12)
and [acceptance gates](realtime-voice-progress.md#hosted-phone-acceptance-and-follow-ups--2026-11-12).
The web entry is the originating thread's top-bar options → Voice call.
Its dedicated page keeps one app-owned connection across in-tab navigation;
End call stops voice, not submitted thread work. Progressive drafts are not
normal chat history until finalized, and displayed SDK activity is an
observation, not proof of playback or background execution.
Thread options share desktop/mobile purpose groups for conversation actions,
model/reasoning, export and worker-stopping thread resolution. Resolve warns
that reopening the thread does not resume cancelled work. The voice page keeps
conversation, observed activity, capture controls and actionable failures/
approvals visible; identifiers, transport/provider details, route configuration
and runtime events are collapsed under Diagnostics. Enable sound unlocks
playback on the existing room (not another join or microphone start) and stays
visible until the browser confirms the assistant's audio is playing.
Microphone state observes the existing capture track and publication instead of
assuming a successful enable remains live. Device changes do not automatically
capture or unmute. Recover microphone is absent from healthy/default controls;
an explicit error-only action can request capture on the same room without
resetting its deadline. The click-only existing-track probe records/uploads
nothing. Capture, publication, Silero speech, pre-send queueing
and provider transcription are separate evidence boundaries; none identifies
phone/headset hardware or proves remote receipt by itself. See
[microphone diagnostics and recovery](realtime-voice.md#microphone-input-evidence-and-explicit-recovery).

Mute/unmute and recovery exclude one another per room; End/disconnect or lost
publication ownership stops late capture. Explicit recovery can restore an
SDK-muted track without overriding intentional owner mute, and a successful
Unmute clears stale capture errors only after live publication is observed.
The owner/time-filtered `voice_audit` management
command exports sanitized session/event/call metadata in JSON/CSV inside a
PostgreSQL READ ONLY snapshot, without loading engines or dispatching tasks.
See the [all-session audit and schema](realtime-voice-audit-20261113.md).

Composer dictation is separate from voice calls: Stop finalizes into the latest
editable draft without sending or deleting selected text; Send stops recording,
awaits final transcription and uses the normal handler exactly once. Duplicate
clicks/Enter share finalization. Concurrent edits survive; failed transcription
keeps draft/recoverable audio and offers Retry/Download without a partial send.
Cancel, navigation and unmount invalidate late callbacks and pending sends.


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
  requests open in and out, running workers, and "blocked" when a chat it
  waits on is paused for the user's approval
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

- **Upward.** Any chat may message its own bot's main chat. With
  `orchestration` on, any chat may also message its parent bot's main chat,
  named chats, or threads directly. Bots with `orchestration: false` get the
  always-loaded `ergo_message_up` tool for their own main chat and, from
  their main chat, the parent's main chat.
- **Downward** to sub-bots needs `orchestration` (on by default).
- **Sideways** to another bot, including a sibling bot, needs
  `permissions.call_bots`, except for the exact starter chat. The user's
  `permissions.users` access to the target bot is required in every case;
  superusers retain access. A chat may message any same-user chat or thread
  of an authorized bot.
- **New threads are the target's call.** `thread: "new"` needs the target
  bot's `threads.allow_create`, whether the sender is the bot itself or
  another bot; otherwise the sender messages its main chat. A thread another
  bot started records `started_by` (the sending chat), `started_by_bot` and
  `started_by_label` in its metadata; Ergonaut links back to that chat from
  the thread's header, and `ergo_thread_list` shows it.
- **Reach the exact starter.** A thread may use `ergo_thread_send` or
  `ergo_thread_forward` with the starter's bot name and the starter session
  ID as `thread`, even if the bot would otherwise be unreachable. This also
  applies to starters recorded on same-bot ancestor threads, so a nested
  thread can reach its inherited starter. The exception is for those exact
  chats, not other chats of otherwise unauthorized bots.
  Starter references must identify an existing session belonging to the
  same user and recorded bot. Forwarding still delivers the user's words
  without routing a reply back.
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
- **Reports get no reply.** A thread's message to its own main chat, a chat's
  message to its parent bot's main chat (unless the parent is also in
  `permissions.call_bots`), or a message addressed by session ID to an exact
  or inherited starter is a one-way report by default. The
  recipient's turn starts with `[Report from …]` and its reply isn't sent
  back, so a status update doesn't cost the sender an acknowledgement turn.
  Pass `ask: true` to `ergo_thread_send` (or `ergo_message_up` for its
  upward destinations) when an answer is needed.
- **Worker results reach the starter.** A thread often answers a request
  with "I've started a worker". When that worker finishes, the thread's reply
  to the result goes to the chat that started the thread as a report
  (`[Result of <worker>]`), unless that turn already messaged the starter.
  Workers' progress updates and questions stay in the thread.
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
  `interrupt: true`, and that only replaces a request _this chat_ sent that
  the thread is working on right now: a turn that answers the user (their
  message, a forward of it, a schedule or a worker) or another chat is never
  stopped by a bot, and the message queues behind it (the result says
  `interrupted: false` and why). It also needs `TURN_STOPPER`. Messages the
  user types in Ergonaut keep their own path: they steer a running turn, or
  `interrupt` it, whoever started it.
- **Managing what it started.** `ergo_thread_stop` and `ergo_thread_resolve`
  (with `bot`) work on threads of other bots that a chat of this bot
  started. Stopping a turn goes through
  `DJANGO_ERGO["TURN_STOPPER"]` (`callable(session_id) -> bool`; Ergonaut
  sets its stop flag, which the turn checks at its next step, and ends a
  turn paused for approval by declining its tool calls, so the request gets
  an answer and what queued behind it goes out). Without it,
  `ergo_thread_stop` only cancels this bot's queued messages there.

Threads idle longer than `sessions.archive_after_days` (default 7) are
archived by `django_ergo.bots.archival.archive_idle_threads`, which
Ergonaut's Celery beat runs hourly; threads with a turn in progress, a
pending approval, an unanswered thread message or a running worker are left
alone. Main and named chats are never archived. Archiving or resolving a thread
(the app's Resolve button, `ergo_thread_resolve`, the idle sweep) cancels the
workers it still has and stops the coding agents they watch, so nothing keeps
running for a resolved thread. A message to an archived thread reopens it.

`BotRegistry.discover("bots/")` loads every folder at or under `bots/` that
has a `bot.yaml`, and lets bots find each other by name. Bot folders can
nest: a bot folder inside another bot's folder is its sub-bot, and the
parent may message its sub-bots with `ergo_thread_send` (as well as
any bot in `permissions.call_bots`), and any chat with orchestration enabled
may message its parent bot's chats and threads. A thread may also reach its
exact starter as described above. Each session starts with an
`ergo_bot_list` result already in its history, built from each bot's
`description` in bot.yaml, so instructions don't need to list the bots.

A bot with `private: true` is closed to other bots: none of them, its parent
and bots that list it in `call_bots` included, can message it, list its chats,
see it in the "Bots and threads" block, open a link to one of its chats with
the history tools, or open its files (attachments, `orca_upload`). It is one
way: the private bot still messages its parent and the bots in its own
`call_bots`, gets their replies, and can share a file with a message
(`attachments`), which the recipient may then open. A thread it started may
report to and ask the exact chat that started it, as any thread may. What the
private bot sends is then in the other bot's history, so its instructions
should say what may leave. This is a rule for Ergo's own tools, not a sandbox:
a bot with a shell on the same host and user can still read the files and the
database (see "Secret hygiene and its limits").

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

### observability

```yaml
- name: observability
  profiles:
    prod:
      grafana_url: http://grafana.monitoring.svc
      public_url: https://grafana.example.com
      token_env: GRAFANA_VIEWER_TOKEN
      datasources: [prometheus]
```

Read-only Grafana and Prometheus. Keys: `profiles` (name to `grafana_url`,
`public_url`, `token_env`, `token_file`, `datasources`, `mode` proxy|direct,
`prometheus_url`, `renderer`, `ownership`) and `root_only` (default false). The model
cannot supply URLs, headers, credentials or datasource uids outside `datasources`.
Direct mode accepts at most one configured datasource uid as the fixed origin's
identity. Renderer authorization fails closed for unresolved datasource references;
the allowlist is not a replacement for Grafana-side token permissions.
Tools: `ergo_grafana_search`, `ergo_grafana_dashboard`, `ergo_prom_query`,
`ergo_prom_targets`, `ergo_grafana_link`, `ergo_prom_chart` (and
`ergo_grafana_panel_render` when a profile has `renderer: true`). Charts need
`django-ergo[observability]`. See [Observability](observability.md).

### runpod

```yaml
- name: runpod
  account: gpu_jobs
  profiles: [h3_frozen]
  root_only: false
```

Provides `ergo_runpod_profiles`, `ergo_runpod_stock` (live stock for a
profile's envelope), `ergo_runpod_start`, `ergo_runpod_status` (including the
chosen offer and fallback attempts), `ergo_runpod_collect` and
`ergo_runpod_terminate`, backed by a real
`runpod:watch` Worker and a separate durable rental ledger. Only `account`,
`profiles` and `root_only` are bot-editable. `root_only` defaults to true;
false exposes tools in threads, not spending authority.

The optional resource app, server-only principal/bot/profile policy, durable
single-host rental filesystem, credential binding and independent maintenance
consumer are required. Start needs human approval, fresh maintenance and an
active allowlisted superuser. Defaults are one real/unresolved lifetime
rental, a planned $3 project cap and ≤1h absolute deadline; no replacement or
uncertain-create replay. The host's `budget_usd` sets the project cap and
`max_rental_usd` (default $3) the per-rental ceiling. Cleanup-complete
zero-create attempts, including creates refused for capacity, cost $0 and do
not consume `max_rentals`; unresolved intents still reserve a slot.
The first reviewed adapter is the frozen six-shot H3 comparison, not chapter
execution or arbitrary commands. See [native installation](runpod.md) and
[image/runtime preparation](runpod-h3.md). Bot YAML alone never enables spending.
The host profile declares a hardware envelope (`config.hardware`: ordered GPU
preferences, VRAM/RAM/vCPU/driver/compute-capability minimums, regions, stock
wait) and an hourly ceiling; none of it is bot-editable. The Worker picks the
best in-stock offer inside it at launch, falls back to the next offer when a
create is refused for capacity, and waits for stock up to the profile's deadline
(`ergo_runpod_stock` previews it; status shows the choice and attempts).
Changing GPUs, regions or caps is a policy-file edit, not a deploy; see
[offer selection](runpod.md#launch-time-offer-selection-and-stock-fallback).
Provider REST `imageName` is normalized before strict ownership checks; missing
or mismatched image evidence never authorizes deletion.
Host-only `reconciled_costs` can record an explicitly reviewed conservative cost
after confirmed terminal cleanup; it never clears real-rental admission counts or
authorizes an active/uncertain rental, replacement or top-up. The host binds
`ssh_private_key_file` to the approved public key; the Worker maintains an
authenticated per-job SSH tunnel to the loopback-only model service, bounded by
the original cutoff. Fingerprint-bound launcher failures stop work promptly.
Collection also retains authenticated launcher failure state, observed timings
and a bounded redacted model stderr tail as `launcher-failure.json`, before
provider cleanup removes access to the rental's logs.

### ergo_kb

```yaml
- name: ergo_kb
  path: kb # Markdown folder, relative to the bot folder
  # or knowledgebases: [Kitchen] (knowledgebase: Kitchen for one), or toolkit: "myapp.kb:make_toolkit"
  # owner: "42"                      # knowledgebases: pick the owner's KB when several share a name
  write: false # path KBs: the bot may save articles (ergo_kb_write)
  commit: true # ...committed and pushed when the KB is in a git checkout
  prefetch: new_session # new_session | every_turn | off
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
  mode: propose_pr # or merge_main
  main_branch: main
  approve_publish: true
  watch_checks: true # message the chat when its own PR's checks pass or fail
  root_only: false # true: top-level chats only, not threads
  run: # optional: ergo_config_repo_run
    approve: true # each run waits for approval (default)
    timeout: 300
    commands:
      toolkit-test: { argv: [npm, test], cwd: ficsit/toolkit }
      node: { argv: [node], cwd: ficsit/toolkit, args: true }
```

Lets the bot maintain the git repository its folder lives in, so it can
change its own instructions, config and tools, or add new bots.
`ergo_config_repo_status`, `ergo_config_repo_list`, `ergo_config_repo_read`,
`ergo_config_repo_grep` and `ergo_config_repo_diff` inspect the files.
Use `ergo_config_repo_edit` for an exact, targeted replacement in an existing
file; `ergo_config_repo_write` creates or replaces a whole file. Ranged reads
return numbered lines, and all paths can't leave the repo or touch `.git`.
Files that cannot be decoded as UTF-8 are treated as binary: reads report their
size, edits reject them, and grep identifies a named binary file or skips and
counts binary files when searching a directory, including when nothing matches.
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

The bot can also follow its pull requests through review and CI.
`ergo_config_repo_pr_status(number)` shows any PR's state, mergeability, review
decision, each check with its link, the last lines of each failed GitHub Actions
job's log (`gh run view --log-failed`), reviews and the latest comments. The rest
only work on the bot's own open PRs, the ones on a `bot/<name>/` branch.
`ergo_config_repo_checkout_pr(number)` makes the draft that PR's branch (the draft
must have no unpublished changes), so the bot reads and edits the files as the PR
has them; `ergo_config_repo_publish` then pushes a commit to the same PR instead of
opening another (on top of anything else pushed there meanwhile), and the draft
goes back to main. `ergo_config_repo_discard` drops the PR draft without pushing.
`ergo_config_repo_close_pr(number, comment)` closes a PR the bot replaced or no
longer wants and deletes its branch; it waits for approval like publishing.

A published PR is recorded in the chat, and the host refreshes recorded PRs
(Ergonaut every minute). With `watch_checks` (on by default), when the checks of one
of the bot's own PRs finish, failing or passing, the chat gets an automated
event from `GitHub` that starts a turn. Its source identity is `system` /
`github`, with event provenance; it is not a new request or approval from
the person. The event provides the check result and repository policy:
on a failure the bot reads the logs and pushes a fix to the same PR, and
on a pass it tells the person the PR is ready for review. A passing event
does not authorize merging. It hears again only when the result changes,
such as after a fix is pushed. Existing publish approvals still apply.

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
  environment: devhost # every call is pinned to this Orca environment
  executable: orca-ide # default: orca-ide if installed, else orca
  files_host: devhost # SSH host holding worktrees; "" means local
  max_attach_bytes: 20000000 # largest worktree file orca_attach brings into a chat
  max_upload_bytes: 200000000 # largest chat file orca_upload copies to a worktree (streamed)
  usage_minutes: 10 # minimum minutes between agent session scans
  approve_changes: true
  root_only: true
```

Lets the bot run the Orca CLI on its host to manage worktrees, terminals and
supervised workers. `orca_read` runs inventory commands with no approval
(`status`, `worktree ps|list|show`, `terminal list|read|show`, `repo
list|show`, `orchestration run-list|worker-list|worker-read|worker-show`,
`search`, `skills get`, anything with `--help`). `orca_run` runs everything
else, such as `worktree create`, `terminal send` or `orchestration
worker-stop`, and waits for approval unless `approve_changes: false`. It
refuses `orchestration worker-start`: workers start only through
`orca_start_worker` or `ergo_agent_start`, which watch them and bring their
report back to the chat. When the chat's saved mailbox terminal is gone or
lost its pane after an Orca restart, they make a new one and retry once.
Arguments are an argv list, never a shell string, and `--json` is added for
the bot. With `environment` set, the bot can't point a call elsewhere.
It is also the `orca` agent manager (see [Agents](#agents)): `agents`
(default `[codex, claude, omp]`) lists what `ergo_agent_start` may start, and
loading the `orca` skill loads `agents` with it.

### local_shell

```yaml
- name: local_shell
  root: ~/agent-jobs/ads # each job is <root>/<workspace>
  template: ~/kits/creative # optional: copied into each new job folder
  agents: [claude, codex]
  executables: { claude: claude-subscription } # default: the agent's name
  command_prefix: [] # e.g. [sudo, -n, -u, agentrunner] or a bwrap sandbox
  allowed_tools: [Read, Write, Edit, Glob, Grep, Bash]
  disallowed_tools: []
  permission_mode: dontAsk # Claude Code: anything not allowed is refused
  codex_sandbox: workspace-write
  codex_network_access: true # false disables networking in workspace-write
  max_turns: 100
  pass_env: [PATH, HOME, LANG, LC_ALL, TZ, USER]
  secrets: [HIGGSFIELD_API_KEY] # bot secrets every job gets
  timeout_minutes: 60
  max_concurrent: 2
  approve: true
  root_only: false
  poll_seconds: 30
```

The `local` agent manager (see [Agents](#agents)): `ergo_agent_start` with a
job name as `workspace` makes `<root>/<name>/` (copying `template` the first
time, without symlinks), writes the brief to `brief.md` and runs the agent
there headless (`claude -p --output-format stream-json`, or `codex exec
--json`). A small supervisor runs it detached in its own process group and
keeps the transcript, a heartbeat and the exit status in `<job>/.ergo/`, so a
worker in another process or pod that shares the folder can watch it. A run
whose heartbeat stops (its pod restarted) fails the worker. The agent and
everything it starts get the highest OOM score, so a job that runs its pod out
of memory is killed instead of the Ergonaut process that launched it, and the
worker fails saying so. A final message
that starts `NEEDS_INPUT:` reaches the chat as a question; `ergo_agent_reply`
resumes the same Claude session with the answer. An exited Codex run cannot be
resumed: its worker finishes with `needs_new_run` and retains the question and
outputs. Replies are rejected immediately. If authorized, start a new run
explicitly with the answer, existing artifacts and constraints in the brief;
no successor is started automatically. A worker follows the
run it started and the runs that answered its questions; once another
`ergo_agent_start` begins a fresh run in the same folder, the older worker
closes (done, or failed if its run never finished) and stops repeating the
newer run's questions. Before every run,
including a resumed one, the job's copy of the template is brought up to
date: files it lacks are copied in, and files still exactly as the template
gave them (hashes in `.ergo/template.json`) get the template's new version;
files the job changed are never overwritten. So a job made before a kit fix
or a new helper still gets it. The report lists the
files the run made or changed, with sizes and SHA-256 hashes.

Each job's token counts go to Costs and the chat's usage chart, for Claude
Code and Codex alike: every 5 minutes while a run is going and again when it
ends. They come from the agent's own session files (`~/.claude/projects`,
`~/.codex/sessions`) for the job folder and the worker's time span, so every
request is counted under its own model, sub-agents included. When those can't
be read (an agent running as another user), the run transcripts are summed
instead: Claude Code's `result` events (`modelUsage` per model) and Codex's
`turn.completed` usage. Only the worker's own runs count, not a later fresh
run in the same folder. `ergonaut manage agent_usage [--dry-run] [--session
<id>]` reads usage again for workers whose counts were missed.

Local Codex jobs default to the `workspace-write` sandbox with network
access enabled globally, without changing the CLI's home configuration.
Set `codex_network_access: false` to disable network access in that sandbox.
`codex_sandbox` still selects the sandbox; this setting does not affect Claude
or grant writes outside the workspace.

The agent's environment is rebuilt from `pass_env` plus `secrets`; variables
that would move an agent CLI onto API billing (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY` and the like) are never passed, and listing one in `secrets`
is an error. The bot's other secrets never reach it. A listed secret that
isn't set in the process launching the run is left out, and
`ergo_agent_start` (as `warnings`) or the reply's result says which ones.
Processes read their environment when they start, so after adding a key to a
deployment's secret, restart every process that runs tools and workers, not
just one of them. Job runs live in the process that launched them, so a
restart ends them: `ergonaut manage wait_idle` (and the upgrader) wait for
running `local_shell` jobs even with `--ignore-workers` /
`ERGONAUT_UPGRADE_WAIT_FOR_WORKERS=0`. Runs time out after
`timeout_minutes` and at most `max_concurrent` jobs run under `root`.
`local_shell_jobs` lists jobs, `local_shell_upload` copies chat files into a
job folder and `local_shell_attach` copies a job's file into the chat
(paths stay inside the job, `.ergo/` and secret-looking names are refused);
none of them asks first.
Use `local_shell_upload(job="render", files=["<shared-id>"], session_id="<source-session-id>")`
for a file explicitly shared with this chat. The ID alone also works; the
optional `session_id` restricts every file to that source. Filenames resolve
only in the current chat. Shared files stay in their source session and require
the attachments plugin with `other_sessions: true` and the same session owner;
unshared cross-session IDs are rejected, including an ID only quoted in a
message's text. A chat that received a share can pass it on: give its ID to
`ergo_thread_send` (or `ergo_message_up`) `attachments`, and the next chat can open
or upload it in turn.


### kubectl

```yaml
- name: kubectl
  clusters:
    cluster-name:
      kubeconfig: /mounted/kubeconfig
      namespace: default # optional default for calls without -n/--namespace
  approve: true # required; every kubectl_run call waits for approval
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
kubeconfig and rejects flags that could change the kubeconfig, context, cluster,
server (including the short `-s`), proxy, TLS server name or identity. It redacts structured `data`, `stringData`, and credential-shaped
output (including env-style names such as `DB_PASSWORD=`) and refuses `get secret` custom formats, so Secret values are never
returned.

### bash

```yaml
- name: bash
  cwd: ~ # working directory
  approve: true # every command waits for approval (review: see below; false: none)
  timeout: 120
  root_only: true
```

Gives the bot `ergo_bash_run(command, cwd?)`, which runs `bash -lc` on the
host as the user Ergonaut runs as. Output is stdout and stderr with the exit
code, trimmed to its start and end when long. It is the whole machine, so
keep `approve: true` unless you trust the bot with it.

#### Reviewed approval

`approve: review` keeps approval for every command except routine reads the
user clearly asked for, which a reviewer model lets through:

```yaml
- name: bash
  approve: review
  reviewer: # optional; these are the defaults except ssh_hosts
    model: auto/small # a providers.yaml model or auto/<tier>, picked by routing
    ssh_hosts: [rigel] # ssh aliases whose remote commands may be reviewed (default none)
    timeout: 45 # seconds; a slower review waits for the user
```

A command runs without asking only when all of this holds; anything else,
including any error, a timeout, an unreadable verdict or a missing model,
waits for the user's approval exactly as with `approve: true`:

- **Plain read-only shell.** Programs that only read (`ls`, `cat`, `head`,
  `tail`, `find`, `grep`, `stat`, `file`, `du`, `wc`, `sort`, `diff`, `cd`,
  ...), joined by `;`, `&&`, `||` or `|`, with output only discarded
  (`2>/dev/null`, `2>&1`), or `ssh <host> '<command>'` to a listed host whose
  remote command passes the same check. `$`, backticks, `$()`, `<`,
  heredocs, other redirections, subshells, escapes, interpreters, nested
  ssh, options that write or recurse (`sort -o`, `find -exec`/`-delete`,
  `grep -r`), globs in file reads, and paths that look like credentials
  (`.ssh`, `.env`, `/proc`, keys, tokens, shell history, ...) never pass.
- **The user's own words, this turn.** Every user message of the current
  turn is the chat owner's: sent from a signed-in Ergonaut browser session
  (not an API key) by an active admin, or such a message forwarded word for
  word with `ergo_thread_forward` that no newer message in its chat has
  superseded. A turn another bot, a worker, a schedule, an API key, voice or
  Telegram started or steered has no authorization, and an earlier turn's
  never carries over. Bot messages, tool output, file contents and
  forwarding notes never reach the reviewer, so they can't grant anything.
- **No secrets in the command.** A command or cwd that redaction would
  change (a token, `password=...`, a credential in a URL) waits.
- **The reviewer allows it.** The model sees the exact command, cwd, where it
  runs and those messages (secrets redacted), and must answer
  `{"decision": "allow"}` for a read that fits the request; it's told to
  escalate anything that writes, touches credentials, goes beyond the
  request or is unclear. The call is tool-less; a bot in a private pool has
  no reviewer.
- **Nothing changed meanwhile.** A message or stop that arrives during the
  review, or before the command starts, means the request changed: the
  command and the rest of that step's tool calls get a "Not run" result,
  then the message goes in and the model decides again.

Each call is reviewed on its own (no decision is cached) and every decision
is recorded in the turn's `StructuredCall.metadata["bash_reviews"]`:
decision, reason, model, a keyed hash of the command, the command and cwd
with secrets redacted, the tool call, turn and message ids, and how long it
took. The log line (`django_ergo.plugins.bash_review`) leaves out the command.

Accepted residual risks (Lee, 2026-11-13):

- Secret screening of the messages sent to the reviewer is best-effort:
  known token formats, private keys, `.env`-style and `password=`
  assignments, credentials in URLs, high-entropy strings and values of secret
  environment variables are redacted. A secret in no recognizable form can
  reach the reviewer model.
- Reads are approved by path. Nothing binds the reviewed path to the file
  the shell opens, locally or over ssh (`ssh rigel cat <path>`): a symlink
  can make the command read a different file than the one reviewed.

This is not a sandbox: Bash still runs as the Ergonaut OS user with a
scrubbed environment and login-shell startup files. Rollback: set
`approve: true` again.

### web_search

```yaml
- name: web_search
  backend: auto # auto, brave, claude_code or anthropic
  brave_api_key_env: BRAVE_API_KEY
  anthropic_api_key_env: ANTHROPIC_API_KEY
  model: "" # claude_code: haiku; anthropic: claude-haiku-5-5
  count: 8 # results per search
  timeout: 90 # seconds per search or fetch
  max_page_chars: 20000 # ergo_web_fetch trims pages to this
```

Gives the bot `ergo_web_search(query, count?)`, which returns the top results
as markdown links with snippets, and `ergo_web_fetch(url)`, which reads a page
as text. The tools are the same whichever engine a chat is routed to; the
backend decides where a search runs:

- `brave`: the Brave Search API, with the key in `brave_api_key_env`.
- `claude_code`: a short `claude -p` call that may only use Claude Code's
  WebSearch tool, on the host's subscription login (the one the Claude Code
  engine uses). No API cost, one small model call per search.
- `anthropic`: one Messages API call with Anthropic's server-side web search
  tool, billed per search on `anthropic_api_key_env`.
- `auto` (the default): `brave` when its key is set, else `claude_code` when
  the `claude` CLI is installed, else `anthropic` when its key is set.

`ergo_web_fetch` is a plain GET from the Ergonaut host and only reaches
public addresses: localhost, the private network and cloud metadata are
refused, also through a redirect. Use the `browser` plugin for pages behind a
sign-in or that block datacenter IPs. Add the `web_search` skill to the chats
that should search, or let the bot load it when it needs to.

### browser

```yaml
- name: browser
  cdp_url: http://127.0.0.1:9222 # Chrome's --remote-debugging-port, as ssh_host sees it
  ssh_host: rigel # optional: reach cdp_url through an ssh master on this host
  takeover: the Chrome window on rigel # where a person signs in for the bot
  sessions: true # browser tools need an approved ergo_browser_start
  idle_minutes: 30 # a session with no browser calls this long ends
  poll_seconds: 120 # how often the session worker checks Chrome
  launch_command: "" # run over ssh_host at start when Chrome doesn't answer
  approve_actions: false # clicks, typing and key presses also wait for approval
  root_only: true
  timeout: 30 # seconds per browser call
  max_snapshot_chars: 20000
  driver_model: auto/small # the model ergo_browser_task drives the browser with
  driver_steps: 30 # its tool calls per task, at most
```

Attaches to a running Chrome over CDP with Playwright (the `browser`
extra). `ergo_browser_start` always waits for approval and opens one
browser session for the bot, shown as a `browser:session` worker that
checks Chrome and ends after `idle_minutes` without a browser call;
`ergo_browser_stop` ends it and closes the tabs it opened. While a session
is open the bot has `ergo_browser_tabs`, `ergo_browser_open`,
`ergo_browser_snapshot`, `ergo_browser_screenshot`, `ergo_browser_wait`
(wait while the user signs in), and `ergo_browser_click`,
`ergo_browser_type` and `ergo_browser_press`, which also wait for approval
with `approve_actions: true`, as do `ergo_browser_batch` (several steps in
one call, one snapshot at the end) and `ergo_browser_task` (a goal handed
to `driver_model`, which drives the other tools in its own loop in a
`browser:task` worker and reports back in the chat; its `model` argument
picks another driver for one task, checked as in
[Model choice for tasks](#model-choice-for-tasks)). A bot in a private pool always drives with that pool (its
default, or a `driver_model` of the pool such as `personal/flash`). Actions refuse to run in a tab the user has taken over (navigated
since the bot last looked, or scrolled, clicked or typed in between 3 of
the bot's calls within 5 minutes). With `sessions: false`
there are no start and stop tools and the browser tools always work.
Snapshots are accessibility trees whose refs (`e12`) the actions take; a
Playwright selector works too. The bot is told to hand sign-ins, two-factor
codes and captchas to the user in `takeover`. With `ssh_host`, calls go
through an ssh ControlMaster forwarding a local port, one per host or pod.
Setting up Chrome and SSH: [Browser control](browser.md).

### attachments

```yaml
- name: attachments
  max_bytes: 5000000 # largest file the bot may write
  # Optional, off by default; absolute artifact roots on configured SSH aliases.
  import_roots: {rigel: /home/linked/artifacts}
  max_look_bytes: 20000000 # largest file ergo_attachments_look sends to the model
  other_sessions: true # may read files in the user's other sessions
  video_model: gemini-flash-latest # watches videos for ergo_attachments_look
  gemini_api_key_env: GEMINI_API_KEY
  max_video_bytes: 2000000000 # largest video sent to Gemini
```

Files in a chat session are `ConversationAttachment` rows: sent with a
message, uploaded to the session (Ergonaut's Files panel), or written by the
bot (`source` says which; session files have no `message_sequence`). The bot
gets `ergo_attachments_list`, `ergo_attachments_read`,
`ergo_attachments_create` and `ergo_attachments_update`. It writes text
files only in its own session, reads files in the same user's other
sessions, and sees the session's file list in every turn's context.
When `import_roots` is configured, `ergo_attachments_import(host, path)` asks
for approval, then copies one file from the configured absolute root on the
named SSH host (`local` means this host) into this chat. It rejects paths
outside the root, secret-looking filenames, oversized files and malformed
common image/PDF formats. The original is unchanged; only metadata, not file
bytes or base64, is returned to the bot. Imported files are visible in the
chat and downloadable. The source host needs Python 3 and a POSIX filesystem
with `O_NOFOLLOW`. Imports reject symlinks in any path component, including
the configured root and its ancestors, and reject non-normalized paths.
Grant roots narrowly: the host account's file access is not an OS sandbox,
and a source file can change between approval and reading.
`ergo_attachments_look(attachment_id, question)` lets it see an image or PDF.
An image comes back in the tool result, so the bot looks at it itself. A PDF
or other file goes to the bot's own model as an attachment in a separate call
(kind `attachment_look`) that answers the question. A video goes to
Gemini, which watches it with its sound (optionally only from
`start_seconds` to `end_seconds`) and answers; it needs `GEMINI_API_KEY`.
`ergo_attachments_frames(attachment_id, count | times)` takes up to 8 still
frames with ffmpeg (one low-memory run per frame) that the bot sees itself
and that are saved in the chat. See [Attachments: Video](attachments.md#video).
Files the model can't see at all (an archive) are refused with a note to
process them elsewhere first. Files sent with a
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
  users: { 123456789: lee } # Telegram user or chat id -> Django username
  respond_to:
    users: all # all, or Telegram ids and @usernames (default: those in users)
    messages:
      all # all, private (1:1 chats only), or mentions (in groups,
      # only @mentions of the bot and replies to it)
  target_thread: Telegram # every chat goes into one shared thread with this title
  passive_group_chats: [-100123] # optional group/supergroup chat IDs to capture as context
  owner: lee # the thread's owner, a username or email (default: the first admin)
  replies:
    auto # auto: answers go back to the chat; tool: the bot sends
    # with ergo_telegram_message
  photo_batch_wait: 0 # seconds to gather photos sent one at a time (0 = off)
  album_wait: 1.5 # seconds to collect an album's photos
  mode: auto # webhook, polling, or auto
  secret_env: KITCHEN_TELEGRAM_SECRET # optional webhook secret
  notify_delegations: true # pass delegated replies in the main chat on to Telegram
```

In `auto` mode Telegram uses a webhook when `BOT_WEBHOOK_BASE_URL` is set:
`bot.serve()` registers it and returns, and updates arrive at
`hooks/<bot>/telegram/update/` with Telegram's secret-token header. Without
a public URL it long-polls.

Polling first deletes any existing Telegram webhook. Transient network errors,
HTTP 408, 429 and 5xx retry indefinitely with delays doubling from one second
to a maximum of 60 seconds (an HTTP 429 `retry_after` or `Retry-After` raises
the wait to that value, capped at 300 seconds); polling starts only after
deletion succeeds. An unsupported `api_base` is a permanent configuration error.
Other failures, including authorization and configuration errors, disable
this Telegram poller and are logged without exception details or token-bearing
URLs; other plugins and bots continue. Correct the cause and restart the bot
runner to retry a permanent failure. A running process is therefore not proof
that every Telegram poller started. Cancellation interrupts the async wait;
an in-flight standard-library HTTP request runs in a thread and can remain
until its 60-second request timeout.

**Outages.** Each update is saved (as a `BotJob`, target `plugin:telegram`)
before Telegram is told it arrived: a webhook answers 500, and polling keeps
its offset, when the database can't take it, so Telegram sends it again.
A saved update the database failed on **before execution** is replayed after
two minutes (up to three tries); startup also queues recent updates the
process died before starting. A database failure after execution starts
does not automatically repeat tools. Other failures tell the chat that
handling failed. Dead database connections are dropped after a failure,
so a Postgres failover doesn't wedge the poller.

Polling and webhooks acknowledge button taps as soon as their updates are
saved, independently of turn execution. Messages run through a FIFO worker
per chat (one shared queue when `target_thread` is set), including album
collection, media downloads and turn-lock waits. Without `target_thread`,
an approval from another person in a group can bypass messages waiting on
an unrelated person's session; authorization is unchanged, same-person
messages and taps keep their order, and turns still take the session lock.
Idle queues are removed and worker failures are logged.

**Deployment boundary.** The intake FIFO is process-local, not a distributed
ordering guarantee. Durable acceptance uses check-then-insert deduplication;
concurrent webhook workers can race when accepting the same update. A single
polling process for a bot avoids those concurrent intake paths, provided no
second poller or webhook consumer handles that bot (including during rollouts).
Session locks do not turn multiple intake queues into one globally ordered
queue. Do not infer cross-worker ordering or exactly-once delivery from the
FIFO or replay behavior.

Ergonaut waits for a busy session with asynchronous polling and serialized,
nonblocking lock probes, not blocked executor threads or shared worker
slots. Busy sessions and long Telegram turns therefore do not prevent
unlocked chats, other bots, or unrelated approval decisions from running.
Intake and lock release retain executor capacity.

Every source job stays `pending` while queued, collecting an album or photo
batch, preparing media, or waiting for the host's turn lock. An album is
rebuilt from its saved `media_group_id` and chat on restart. All collected
source jobs are claimed as `in_progress` in one database transaction under
the session lock immediately before execution; competing replay processes
cannot run the same claimed sources again. The grouped turn delivers its
result and marks all sources `completed` before releasing that lock.
`photo_batch_wait` collections also stay inside the FIFO and retain their
source jobs until completion.

A crash before the claim leaves replayable jobs; a crash inside the claim
transaction rolls all claims back. A crash **after** the claim has an
uncertain outcome: some tools may already have run. Recovery waits for the
same session lock (so a live replica can finish), warns the Telegram chat
that execution was interrupted and must be checked, and marks the stranded
jobs `failed` without rerunning tools. A failed warning remains retryable;
a crash between warning and status update can repeat the warning, not the
tools. Approval decisions additionally retain their atomic resume claim,
so a double tap cannot execute an approved tool twice.

Polling advances its offset only after durable acceptance.
`await plugin.drain()` waits for saved updates to finish when using the
plugin directly. No migration or new configuration is required.

**Where messages go.** Without `target_thread`, each message's sender is
looked up in `users`, then its chat, so in a shared group chat each person
talks to the bot as themselves (in their own main chat) and the reply goes
back to the group; anyone else is ignored. With `target_thread`, every
message goes into one thread under the owner's main chat, whoever sent it,
so the household shares one conversation that also shows in the app. The
thread loads the main chat's skills and the `telegram` skill from the
start, and an archived thread comes back with the next message.
`respond_to: {users: all}` needs a `target_thread`.

**Who and what it answers.** `respond_to.users` is `all`, or a list of
Telegram user or chat ids and `@usernames`; by default it is whoever
`users` lists. `respond_to.messages` is `all`, `private` (1:1 chats only),
or `mentions`: in a group only messages that @mention the bot or reply to
it get a turn. `tags` is stricter: only an explicit @mention of the bot
starts a group turn; a reply alone does not. In all modes 1:1 chats always
pass the message filter (sender authorization still applies).

**Passive group context.** `passive_group_chats` is an opt-in list of
explicit Telegram group or supergroup chat IDs and requires `target_thread`.
For Storytime (`@storytime69bot`), use the actual numeric sender and group IDs:

```yaml
plugins:
  - name: telegram
    token_env: STORYTIME_TELEGRAM_TOKEN
    users: {LEE_TELEGRAM_ID: lee}
    owner: lee
    target_thread: Telegram
    passive_group_chats: [GROUP_CHAT_ID]
    respond_to:
      users: [LEE_TELEGRAM_ID]
      messages: tags
```

Replace both placeholders with numeric IDs; do not put a token in the bot
configuration. Obtain group members' consent before enabling passive capture.
Disable the Telegram bot's group privacy mode in BotFather so Telegram
delivers ordinary group messages. Telegram cannot backfill messages from before
the bot joined the group. Discover the group ID from existing session metadata
or a separate discovery bot, not a second `getUpdates` consumer using an
already-running bot's token. See the [Storytime operations handoff](telegram-storytime-handoff.md)
for the concrete sender policy and deployment prerequisites; kitchen defaults
remain unchanged.

Telegram gives a group a new id (`-100...`) when it becomes a supergroup,
which happens when it gains admin features such as message links. The
plugin follows the upgrade's service message: the new id is recorded as
passive (kept in the target thread's metadata, so it survives restarts), the
chat used for replies moves to it, and a warning asks you to put the new id
in `passive_group_chats`. An upgrade that happened before this was in place
was missed, so list the supergroup's id yourself (a `t.me/c/<n>/...` message
link means id `-100<n>`). Untagged messages from a group that is not listed
are dropped, with an info-level log line saying so.

In an opted-in group, messages that do not otherwise get a response are
stored in the target thread as user messages, without starting a bot turn or
changing the chat used for replies. They retain the real Telegram author and
`telegram_passive` provenance, including the originating chat and message IDs
and, when present, the replied-to message ID. Captions and downloaded media
are saved as message attachments; passive audio is not transcribed and passive
capture does not fetch author profiles. This is untrusted context, not an
instruction to the bot or authorization to act.
Active response authorization in these groups matches the sender only, never
the group ID; configure `respond_to.users` accordingly. Messages in opted-in
groups are processed individually, not batched as albums or photo collections,
so each message is authorized independently. Without the opt-in, existing
message handling and defaults are unchanged.

Active and passive message writes share a short database row lock while
allocating a sequence and saving content blocks. Concurrent first messages
recheck thread creation under an owner-row lock. Passive capture does not use
`CHANNEL_TURN_RUNNER`: it must not clear stop flags or dispatch queued work.
These database locks protect storage, not distributed intake order.

**Replies.** With `replies: auto` the turn's answer goes back to the chat
it came from. With `replies: tool` it stays in the app, and the bot writes
to Telegram with `ergo_telegram_message` (text, and `reply: true` to quote
the latest Telegram message), which sends to the chat that last wrote to
this conversation. `ergo_telegram_file(attachment_id, caption, reply, session_id)`
sends a saved file by ID or filename: images as photos, videos as videos, and
other files as documents. It uses the current session unless `session_id` names
another session owned by the same user; cross-session access requires the
attachments plugin with `other_sessions: true`. To send a design from another
bot, have it call `ergo_thread_send` to the Telegram thread with the file in
`attachments`. On the user's explicit request, call `ergo_telegram_file` in
that thread with the shared file's ID as `attachment_id` and its source thread ID
as `session_id`. The shared reference remains in the destination message and the
original stays in its source session; no file is moved. URL-only attachments
cannot be uploaded; uploads are limited to 50,000,000 bytes and inbound downloads
to 20,000,000 bytes. Telegram can impose stricter limits. Approval prompts and
errors reach the chat either way.

Both send tools report the Telegram message id, and the conversation remembers
the last 50 messages they sent (`telegram_sent` in its metadata).
`ergo_telegram_delete(message_id)` deletes one of the bot's messages from the
conversation's chat; without `message_id` it deletes the latest one sent. An id
the bot sent before ids were remembered can still be named. Telegram allows a
bot to delete its own messages for 48 hours in groups (longer in private chats).
A Telegram message that replies to one of the bot's messages starts with
`[Replying to your message N]`, so "delete this" can name it.

**People.** The message's `author` is the Telegram user (`telegram_user`,
their id and name), whichever chat it lands in, and the plugin keeps an
`AuthorProfile` for each one with their username and profile photo
(refreshed daily). Ergonaut shows the photo next to their name. Forwarding
preserves this `telegram_user` identity.

**Media.** Photos, videos, voice notes, audio and documents become attachments.
Active voice and audio are transcribed when `DJANGO_ERGO["AUDIO_TRANSCRIBER"]`
is set (Ergonaut sets it when `OPENAI_API_KEY` is); passive audio is not.
The photos of an album arrive as one active turn, and with `photo_batch_wait`
so do photos a person sends one at a time (handy for a recipe in several
screenshots); a text they send while photos are gathering goes with them at once.
Downloads are limited to 20,000,000 bytes per file; empty, oversized or failed
downloads are represented by a not-downloaded note. Telegram may enforce
stricter media and upload restrictions.


A turn that stops for approval replies with Approve and Deny buttons for the
pending batch of tool calls (in a shared thread anyone it answers may press
them; otherwise only the mapped session user may). The clicked prompt's
keyboard is removed before the resumed tools run, on a best-effort basis;
repeated or stale buttons never approve a newer batch. Telegram rejecting a
callback acknowledgement or message edit does not discard an otherwise
authorized decision. The final message's chat ID, message ID and approval
fingerprint are saved with the call so completion or a changed approval batch
also retires the prompt when the decision comes from another channel. A new
text message declines the pending batch and retires its prompt before starting
its own turn. Approval requests do not automatically expire.
The `tags` filter governs new group messages, not an authorized person's
explicit decision on an existing approval prompt. Lee can press a current
Approve/Deny button without adding a tag. Callbacks still require sender
authorization (never a group-ID substitute in an opted-in group), the matching
bot/thread, and the current pending-batch fingerprint checked under the turn
lock; a partner cannot approve through a shared group mapping.
Failure to clear the saved prompt after a turn is logged without failing an
already executed turn or withholding its result.
`plugin.notify(user, text)` sends a message from other code. Turns run
through `DJANGO_ERGO["CHANNEL_TURN_RUNNER"]` when it is set, which Ergonaut
uses to hold the session's turn lock so Telegram and the app take turns.

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

## Private inference

A **private pool** is a set of API-billed models that only the bots it
names may use. Pools are listed under `private_pools` in providers.yaml,
never under `providers` (`django_ergo.bots.providers`,
`django_ergo.conversation.inference_policy`):

```yaml
private_pools:
  personal:
    enabled: false
    provider: fireworks
    allowed_bots: [personal]
    default: flash
    daily_usd_cap: 5 # optional; example value — Lee chooses
    models:
      flash: accounts/fireworks/models/deepseek-v4p1-flash-1017
      glm: accounts/fireworks/models/glm-5p3
      heavy: accounts/fireworks/models/kimi-k3
```

| Key             | What it does                                                                                                                  |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `enabled`       | `true` or `false` (default). Nothing sends to the pool until it's `true`.                                                     |
| `provider`      | Only `fireworks`: the endpoint is fixed at `https://api.fireworks.ai/inference/v1` and the key comes from `FIREWORKS_API_KEY` |
| `allowed_bots`  | The bots that may use the pool. The host controls this list; a bot's own files can't add it.                                  |
| `default`       | The alias `personal` alone means; one of `models`                                                                             |
| `models`        | Alias to exact model ID. An alias is one path segment; a model ID appears once across all pools.                              |
| `daily_usd_cap` | Optional, more than 0: US dollars per UTC day (see [Spend cap](#spend-cap))                                                   |

The file is parsed strictly. Any of these is a load error (a reload keeps
the last good public models but drops every private pool, a fresh start
skips the file; admins see the error either way, and `personal` is never
replaced by a public model):

- an unknown key in a pool, a duplicate YAML key under `private_pools`, or a
  pool named like a provider or `auto`;
- a top-level key that starts with `private` but isn't `private_pools` (a
  typo such as `private_pool`);
- a public provider, model, `default`, `tiers` or `agents` entry that names
  something private: a pool selector, a pool member's model ID, a Fireworks
  model ID (`accounts/fireworks/...`) or anything containing `fireworks`;
- a public provider named `personal`, with a `base_url`, `api_base` or
  `endpoint` on a `fireworks.ai` host, or with an `api_key_env` that is a
  protected name (`FIREWORKS_*`, see [Secret hygiene](#secret-hygiene-and-its-limits)).

`personal` is a reserved selector even when no pool by that name is
configured: a bot that asks for it fails instead of getting a public model.

### Using a pool

A bot listed in `allowed_bots` selects a member in its bot.yaml, as
`personal` (the pool's `default`) or `personal/<alias>`:

```yaml
engine:
  config: { model: personal/heavy, max_tokens: 8192 }
```

Other engine options such as `max_tokens` and `temperature` pass through.
The endpoint, key and model come from the pool: `api_key`, `api_key_env`,
`base_url`, `api_base`, `endpoint`, `url`, `organization`, `project`,
`default_headers`, `default_query`, `http_client` and `max_retries` in
`engine.config`, or `engine.api_key_env`, fail the turn. `engine.type:
fireworks` can't be set directly; only a pool selector builds that engine.

In Ergonaut, `GET /api/bots/<bot>/models` lists the members under
`private_models`, apart from `models`, and only for an allowlisted bot.
Picking one for a chat (`POST /api/sessions/<id>/model`, or the model of a
new thread) returns 403 for any other bot (see
[Running Ergonaut](ergonaut.md#private-inference)).

For a bot listed in a pool's `allowed_bots`, thread titles, illustration
text suggestions, and compaction summaries use that pool's default model and
its ledger and cap, regardless of the chat's model selection. They require an
authorized pool (a disabled pool still counts, and refuses) and never fall
back to a public model. Other bots cannot use a private engine for these
ancillary calls. Illustration generation itself
is unchanged.

Audio transcription isn't one of them: `DJANGO_ERGO["AUDIO_TRANSCRIBER"]`
is a deployment setting, so where it's set (for example to
`openai_transcriber`), a voice note in an allowlisted bot's chat is
transcribed by that public service, as in any other chat.

### What fails closed

A refusal is a `PolicyError`. The structured call records it with
`error_category` `policy`, never retries it, and never falls back to a
public model. It is raised when:

- the bot isn't in `allowed_bots`, the pool is disabled, the alias is
  unknown, or `personal` isn't configured;
- a chat's picked private model can't be used any more: the pick is kept
  and the turn fails, rather than moving to the bot's public default;
- `FIREWORKS_API_KEY` isn't set.

Public selection never reaches a pool: auto tiers, routers, the
providers.yaml `default`, the model list and `agents` tiers hold only public
models.

Only `Bot.make_engine` mints a **grant**, one per engine (one turn), from
the bot's reviewed config or its chat's authenticated pick. It is bound to
the bot, the exact model ID, the session, the user and the purpose, lives
in memory only (never read from session metadata, engine config, prompts or
messages; it can't be copied or pickled) and expires after 6 hours. Nothing
inherits it: a new thread gets its own grant on its own turns, and a
delegated sub-bot is checked under its own name.

The private engine (`conversation/engines/fireworks_api.py`) checks the
grant before every HTTP attempt, before it reads the key. It speaks
OpenAI-compatible Chat Completions to the fixed endpoint, reads
`FIREWORKS_API_KEY` at send time inside its HTTP transport, sends only the
grant's model, one HTTP request per attempt, and doesn't stream. The SDK's
retries are off; the structured call's retry of transient failures (same
model) is the only retry, and each attempt is checked and recorded again.

The public engines refuse private endpoints. The OpenAI and Claude API
engines, the legacy workflow engine, audio transcription and OpenAI
embeddings fail on an effective base URL on a `fireworks.ai` host, including
one from `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL`.

### Cost ledger

Each HTTP attempt is an `InferenceRequest` row, written before it's sent:
bot, session, user, structured call, selector, pool, provider, exact model,
purpose, attempt number, `billing_mode` (`api`), prompt, cached, output and
reasoning tokens (reasoning is part of output), the rates used, the
reservation (`reserved_usd`), `cost_usd` and `cost_status` (`priced`,
`unknown` or `not_sent`), and the outcome (`ok`, `error` or `not_sent`).
No key or grant is stored.

Prices are exact by provider and model ID (`pricing.EXACT_API_PRICES`),
with no prefix matching. A model without an exact price, or a response
without usage, leaves the cost unknown, never 0. USD per million tokens,
Fireworks standard serverless, checked 2026-10-28:

| Model (`accounts/fireworks/models/...`) | Input | Cached input | Output |
| --------------------------------------- | ----- | ------------ | ------ |
| `deepseek-v4p1-flash-1017`              | 0.30  | 0.006        | 1.20   |
| `deepseek-v4p1-flash`                   | 0.30  | 0.006        | 1.20   |
| `glm-5p3`                               | 1.40  | 0.26         | 4.40   |
| `glm-5p3-flash`                         | 0.15  | 0.03         | 0.50   |
| `kimi-k3`                               | 3.00  | 0.30         | 15.00  |

GLM 5.3 Flash rates were checked against its
[Fireworks model page](https://fireworks.ai/models/fireworks/glm-5p3-flash)
and [serverless pricing](https://docs.fireworks.ai/serverless/pricing) on
2026-10-29.

The structured call's cost uses the same prices, so cost totals include
private spend. Ergonaut's Costs API also breaks it down by pool, provider,
model and bot (the `private` block).

### Spend cap

With `daily_usd_cap` set, each attempt reserves its worst-case cost before
it's sent: every request byte counted as an uncached input token, plus the
full output limit (`max_tokens`, 32000 by default) at the output price.
Under a lock (a PostgreSQL advisory lock), the attempt is refused
(`SpendCapError`, a `PolicyError`, no HTTP) when today's spend plus held
reservations plus this one would pass the cap. Days are UTC. It is also
refused when the model has no exact price or the request has no output
limit, since its cost can't be bounded.

Priced attempts count at their cost. Attempts in flight, and failed
attempts that may have reached Fireworks, keep their reservation; attempts
refused before sending count nothing. Without a cap nothing limits spend,
so set one.

### Coding agents

`ergo_agent_start`, `orca_start_worker` and `agents.start` refuse a private
selector, a Fireworks model ID or any model containing `fireworks`, before
any side effect, including a model picked from an `agents` tier. Their
harnesses can't carry or enforce a grant, so private coding agents aren't
supported. For the same reason a bot in a private pool's `allowed_bots`
can't start coding agents at all: its brief would leave the pool.

### Secret hygiene and its limits

Environment variables starting with `FIREWORKS_`, plus any prefixes in
`DJANGO_ERGO["PROTECTED_ENV_PREFIXES"]` (which can add to the defaults,
never remove them), are protected:

- they are left out of the environment of the bash and kubectl tools, the
  Codex and Claude Code CLI engines, Orca and browser commands, git and the
  other subprocesses Ergo starts;
- `local_shell` refuses them in `secrets` and drops them from `pass_env`;
- `ctx.secret()` raises `PolicyError` for them;
- the telegram plugin's `token_env` and `secret_env`, a public provider's
  `api_key_env` and a bot's `engine.api_key_env` can't name them.

This is defense in depth, not a sandbox. The key is in the Ergonaut
process's environment, so code running in that process or as the same OS
user (a Python tool, a bash command reading `/proc` or a shell startup
file) can still read it. Orca remote workers' own environments are outside
these checks. The planned second phase moves private inference into a
separate, credential-isolated service: its own workload and service user,
the key mounted only there, signed short-lived grants, and network egress
limited to Fireworks.
