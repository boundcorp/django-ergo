---
name: skillbuilder
description: Build or change this bot's dashboards and pages (.jhtml), tables, tools, schedules and skills, and propose them as a pull request. Use it for any change to a bot's own folder, such as a new dashboard or a column on a table, instead of Orca workers. Load it before writing any Python, bot.yaml, skill or .jhtml change.
requires: [config_repo, introspection]
plugins:
  bot_management: {mode: propose_pr}
---
You change bots by editing their folder in the bot repository and proposing a
pull request. Nothing you write runs until a person merges it.

## The workflow

1. `ergo_config_repo_status` and `ergo_config_repo_prs` first: don't redo a
   change that's already proposed. `ergo_config_repo_pull` if main moved.
2. Read before you write: `ergo_config_repo_list` the bot folder, then
   `ergo_config_repo_read` its `bot.yaml`, `agents.md` and the tool files
   closest to what you're adding. Copy their style. To see how an Ergo
   API works, load `introspection` and read its source with
   `ergo_self_read("ergo:bots/tools.py")` (or `ergo:bots/pages.py`,
   `ergo:bots/workers.py`, ...).
3. Write with `ergo_config_repo_write` (whole files) and
   `ergo_config_repo_delete`. You're editing a draft worktree of main, so the
   running bots don't change.
4. Check: `ergo_config_repo_diff` for the whole change, and
   `ergo_config_repo_preview(path)` for every `.jhtml` page you touched (it
   applies the draft's migrations and fills empty tables with sample rows).
5. `ergo_config_repo_publish` with a short title and a body that says what
   the person will see change. It opens the PR (after their approval) and
   gives you the link; put the link in your reply.

Keep one PR to one purpose. If the person asks for more while a PR is open,
say whether it rides along or gets its own.

## Pick the smallest shape

| Need | Shape |
| --- | --- |
| A procedure the bot should follow | `skills/<name>.md` (instructions only) |
| A procedure plus its own tools | `skills/<name>/SKILL.md` + `skills/<name>/tools.py` |
| Tools on their own | `tools/<name>.py`, listed under `tools:` in bot.yaml |
| Data you'll filter, count, sum or chart | a table in `tables.py` |
| Prose the bot should know | the knowledge base (`kb/`), not a table |
| Work on a clock | a `schedules:` entry with `run:` and `prompt:` steps |
| A dashboard | a `.jhtml` page, pinned in `chats.<name>.pins` |

Skill front matter: `name`, `description`, `requires: [...]` (skills loaded
with it), `always_load`, and `plugins: {name: {setting: value}}` (plugins it
needs). The description is all the model sees before loading, so say *when*
to use the skill. A skill with tools must also be loadable: don't put
everything in `always_load`; list it in `chats.<chat>.skills` only when that
chat uses it on most turns.

## Writing a tool file

```python
"""Receipts from the shared inbox: list, read and file them."""   # the skill's description

from django_ergo.bots import bot_tool

@bot_tool
def find_receipts(month: str, limit: int = 20) -> list[dict]:
    """Receipts received in a month (YYYY-MM), newest first."""
    return [summary(r) for r in inbox().search(month)[:limit]]

@bot_tool(takes_context=True, requires_approval=True)
def file_receipt(ctx, receipt_id: str, category: str) -> str:
    """File a receipt under an expense category."""
    books(ctx.secret("BOOKS_TOKEN")).file(receipt_id, category)
    return f"Filed {receipt_id} under {category}."
```

- The module docstring's first line describes the skill; each function's
  docstring describes the tool. Write them for the model: what it returns
  and when to use it.
- Parameters come from type hints (`str`, `int`, `float`, `bool`, `list`,
  `dict`); no default means required. Don't start tool names with `ergo_`
  (that's Ergo's).
- Return only what the bot needs, trimmed. Large results cost tokens on
  every later call in the turn. Raise an exception with a clear message to
  report an error.
- `requires_approval=True` for anything that spends money, messages other
  people, deletes, or is hard to undo.
- Secrets come from the environment through `ctx.secret("NAME")` (a
  per-user `NAME__<USERNAME>` wins). Never put a key in bot.yaml, a file or a
  prompt. Name new secrets in the PR body so someone sets them.
- `ctx` (with `takes_context=True`) has `bot`, `session`, `user`,
  `is_root`, `now()`, `timezone`, `table(name)`, `tasks` and `workers`.
- Tools are plain synchronous functions; the Django ORM and blocking HTTP
  clients are fine. Import heavy libraries inside the function.

## Slow work: tasks and workers

- A few seconds to a few minutes, and the answer belongs in this reply:
  a `@bot_task` run from the tool with `ctx.tasks.run(fn, *args, timeout=300)`.
- Minutes to hours, or polling something external: a **worker**. The tool
  returns at once, the chat shows as busy, and the result arrives later as
  a message the bot answers.

```python
from django_ergo.bots import bot_task, bot_tool

@bot_task
def watch_export(ctx, export_id: str):          # ctx is a WorkerContext
    status = api().export(export_id)
    if status.state != "done":
        return ctx.again(60, progress=f"export {status.percent}%")
    return {"export": export_id, "rows": status.rows}

@bot_tool(takes_context=True)
def start_export(ctx, report: str) -> str:
    """Start a report export; the result comes back when it's ready."""
    export_id = api().start(report)
    ctx.workers.start("watch_export", title=f"Export {report}", export_id=export_id)
    return "Export started; I'll report back when it's done."
```

Each poll is a fresh task, so nothing waits in a process. `ctx.state` keeps
a dict between polls, `ctx.tell(text)` messages the chat mid-run,
`ctx.stopping` means it was cancelled. Arguments and results must be JSON.

## Files and attachments

- Return images with `ToolResult(text, [ToolImage(png_bytes, name="chart.png")])`
  (from `django_ergo.bots`); the model sees them and they're saved in the
  chat. Only the latest two images ride along on each call.
- Read the chat's files in a tool with `ctx.session.attachments.all()` and
  `django_ergo.conversation.attachments.read_text(row)`.
- For big outputs (a CSV, a long report), write a file instead of returning
  it: add the `attachments` plugin (`ergo_attachments_create`) or the `pages`
  plugin for pages, and return a short summary.

## Pre-loading results

When the bot needs some data at the start of nearly every chat, don't make
it call a tool first:

- `@bot_tool(seed=True)` on a tool with no required parameters: it runs
  before the chat's first model call while its skill is loaded (each turn in
  main and named chats) and its result sits in the history as if the model
  had called it. Good for "what's on today's list" or account limits.
- `@bot_context(title="...")`: text added to every turn's context, trimmed
  to a share of the budget. Good for a small live status. It runs on every
  turn, so keep it cheap and short; return `""` when there's nothing to say.
- Neither, when the data is only sometimes needed: a normal tool is cheaper.

## Tables

Use a table when the data is structured and you'll query or chart it.

```python
# tables.py   (and in bot.yaml: tables: [tables.py])
from django.db import models
from django_ergo.bots import BotTable

class Receipt(BotTable):
    """Receipts we've filed."""                  # describes the table to the bot
    vendor = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.CharField(max_length=40, choices=[("food", "Food"), ("travel", "Travel")])
    filed_on = models.DateField()
```

- Rows get `id`, `created_at`, `updated_at`. Everyone shares the rows.
- Schema changes need migrations; publishing runs `ergo_bot_makemigrations`
  for you, so the PR carries them. Don't hand-write migration files unless
  it's a data migration.
- Code uses `ctx.table("Receipt").objects...`; the bot itself gets the
  `tables` skill (`ergo_table_query`, `ergo_table_add`, ...).
- To keep a table fresh without spending model calls, pull data in a
  schedule `run:` step and only `prompt:` when there's something to say:

```yaml
schedules:
  - name: daily-receipts
    cron: "0 7 * * *"
    actions:
      - run: tools/receipts.py:pull_yesterday
      - prompt: "New receipts: {result}. Flag anything unusual."
        to: {thread: "Receipts {date:%b %d}"}
```

## Dashboards: .jhtml pages

A `.jhtml` file is a Jinja template rendered over the tables each time it's
opened. Pages are read-only and sandboxed.

```html
<h1>Receipts</h1>
{{ blocks.metric(label="This month", table="Receipt", aggregate="sum", field="amount", format="money") }}
{{ blocks.chart(table="Receipt", x="filed_on", y="amount", group="category", kind="bar") }}
{{ blocks.table(table="Receipt", columns=["vendor", "amount", "category"], order_by=["-filed_on"], limit=20) }}
{% for row in table("Receipt").filter(filed_on__gte=days_ago(30)).group("category", total="sum:amount") %}
  {{ row.category }}: {{ row.total | money }}<br>
{% endfor %}
```

- `table(name)` offers `filter`, `exclude`, `order_by`, `limit`, `count`,
  `sum`, `avg`, `min`, `max`, `group`, `first`, `rows`. Also `now`, `today`,
  `days_ago(n)`, `user`, `bot`, the `money`/`number`/`percent`/`markdown`
  filters, and `{% include %}` of other bot-folder files.
- Without an `<html>` tag a page gets a layout with Chart.js.
- Put repo pages in `pages/` and pin them:
  `chats: {main: {pins: [{path: pages/receipts.jhtml, title: Receipts, icon: "🧾"}]}}`.
- Always `ergo_config_repo_preview` a page before publishing and fix what it
  reports.

## Before you publish

- Every new tool file is listed under `tools:` (skill-folder `tools.py`
  files load on their own), every table file under `tables:`.
- New skills, tables, schedules and pins are explained in the PR body,
  with any secrets someone must set.
- Re-read the diff as a reviewer would: no secrets, no debugging leftovers,
  docstrings that tell the model when to use each tool.
