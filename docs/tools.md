# Tools and plugins

Tools are how a bot acts. There are three ways to add them, from smallest
to largest:

1. **Tool files**: `@bot_tool` functions in the bot folder (`tools/*.py`, or
   `skills/<name>/tools.py`). Use these for anything specific to one bot.
2. **Toolkits**: a `Toolkit` class from your own package, named in
   `toolkits:`. Use these to share tools between bots or with non-bot code.
3. **Plugins**: a `BotPlugin` that adds tools plus context, lifecycle hooks,
   long-running services and webhooks. Use these for channels and
   integrations.

Each of them becomes a [skill](skills.md).

## @bot_tool

```python
"""Recipes, meal plans and the shopping list in Tandoor."""

from django_ergo.bots import bot_tool

@bot_tool
def find_recipe(query: str, limit: int = 5) -> list[dict]:
    """Search recipes by name or ingredient."""
    return tandoor().recipes(query=query, limit=limit)

@bot_tool(requires_approval=True, takes_context=True)
def add_to_shopping_list(ctx, item: str, amount: str = "") -> str:
    """Add an item to the shopping list."""
    tandoor(ctx.secret("TANDOOR_API_KEY")).add(item, amount)
    return f"Added {item}."
```

- Parameters come from type hints (`str`, `int`, `float`, `bool`, `list`,
  `dict`); parameters without defaults are required. Pass `parameters=`
  (JSON Schema properties) and `required=` to spell them out.
- The docstring is the description, or pass `description=`. `name=`
  overrides the function name.
- A string result goes back as is; anything else is sent as JSON. Raise an
  exception to report an error to the model.
- Tools are plain synchronous functions, run off the event loop, so the
  Django ORM and blocking HTTP clients are fine.

## The tool context

With `takes_context=True` the first argument is a `ToolContext`:

| | |
| --- | --- |
| `ctx.bot`, `ctx.session`, `ctx.user` | the bot, the chat session and the person |
| `ctx.is_root` | true in a top-level chat, false in a thread |
| `ctx.secret(NAME)` | `NAME__<USERNAME>` from the environment if set, else `NAME`, so each person can bring their own key |
| `ctx.now()`, `ctx.timezone` | the time in the person's timezone, else the bot's, else `TIME_ZONE` |
| `ctx.table("AdStat")` | one of the bot's [tables](building-bots.md#tables-and-pages) (a Django model) |
| `ctx.tasks` | start `@bot_task` functions in the background |
| `ctx.workers` | start a worker that reports back to this chat |

Never put secrets in bot.yaml or the prompt. Read them from the environment
with `ctx.secret`; `ergonaut check` lists any that are missing.

## Approvals

`requires_approval=True` stops the turn before the tool runs. The web app
and Telegram show Approve and Deny; `bot.resume(session, decisions)` does
the same in code. Declined calls go back to the model as errors. Use it for
anything that spends money, sends messages to other people, or is hard to
undo.

## Returning images

```python
from django_ergo.bots import ToolImage, ToolResult, bot_tool

@bot_tool
def sales_chart(days: int = 7) -> ToolResult:
    """Chart of the last few days' sales."""
    return ToolResult(f"Sales, last {days} days", [ToolImage(png_bytes, name="sales.png")])
```

The model sees the image itself. It's saved as a file in the chat, and only
the latest two images are sent on each call. See
[attachments](attachments.md).

## Context functions

Put live data into every turn without a tool call:

```python
from django_ergo.bots import bot_context

@bot_context(title="Shopping list", weight=1.0)
def shopping_list(ctx, message: str) -> str:
    return render(tandoor(ctx).shopping_list())
```

Each function's text becomes a section of the turn's context, trimmed to its
share of `root.budget_tokens`. Return an empty string to add nothing. An
exception is logged and skipped, so one failing source never fails a turn.
Keep these cheap: they run on every turn.

## Slow work

### Background tasks

A tool that would take more than a few seconds can hand work to a Celery
worker and wait for it there, so the web process stays free:

```python
from django_ergo.bots import bot_task, bot_tool

@bot_task
def import_receipts(month: str) -> dict:
    ...

@bot_tool(takes_context=True)
def receipts(ctx, month: str) -> dict:
    return ctx.tasks.run(import_receipts, month, timeout=300)
```

`ctx.tasks.start(...)` returns a handle at once (`job.wait()`, or `await
job`). Arguments and results must be JSON-serializable. Ergonaut runs these
on a separate `bot_tasks` queue.

### Workers

For work that takes minutes or hours (a deploy, a data pull, a coding
agent), start a **worker**. The tool returns at once, the chat shows as
busy, and when the worker finishes its result arrives in the chat as a
message the bot answers:

```python
@bot_task
def watch_deploy(ctx, release: str):            # ctx is a WorkerContext
    status = check(release)
    if status != "done":
        return ctx.again(60, progress=f"deploy {status}")   # poll again in a minute
    return {"release": release, "status": status}

@bot_tool(takes_context=True)
def deploy(ctx, release: str) -> str:
    ctx.workers.start("watch_deploy", title=f"Deploy {release}", release=release)
    return "Deploying; I'll report back when it's live."
```

Each poll is a fresh task, so nothing holds a process while it waits and a
restart loses nothing. `ctx.state` persists between polls, `ctx.tell(text)`
messages the chat mid-run, and `ctx.stopping` turns true when someone
cancels it. Every chat has the `workers` skill to list, start and cancel
workers. See [Workers](bots.md#workers).

## Toolkits

Name a factory `(ctx) -> Toolkit | list[Toolkit]` in bot.yaml:

```yaml
toolkits: ["myapp.toolkits:make_crm_toolkit"]
```

The simplest toolkit wraps `@bot_tool` functions:

```python
from django_ergo.bots.tools import FunctionToolkit

def make_crm_toolkit(ctx):
    """Customers and deals in the CRM."""
    return FunctionToolkit.from_functions([find_customer, log_call], ctx)
```

For full control subclass `django_ergo.conversation.toolkit.Toolkit`
(`has_tool`, `execute_tool`, `get_tools_schema`, `render_overview`, and
optionally `requires_approval` and `pre_seeds`). Toolkits also work outside
bots: pass them to `run_conversation_turn(..., extra_tools=[...])` or a
[structured call](structured-calls.md).

A toolkit's `pre_seeds()` are tool calls run before the session's first
model call and written into its history, so the model starts out knowing
their results. `FunctionToolkit(tools, ctx, seed=["tool_name"])` seeds
zero-argument tools.

## Plugins

```python
from django_ergo.bots.plugins import BotPlugin

class CRMPlugin(BotPlugin):
    name = "crm"
    description = "Customers and deals in the CRM"

    def on_load(self): ...                                  # bot constructed; self.config is the bot.yaml entry
    def toolkits(self, ctx): return [make_crm_toolkit(ctx)]
    def context_sources(self, ctx, message): return []      # while the skill is loaded
    def always_context_sources(self, ctx, message): return []
    def skill_hint(self, ctx): return ""                    # shown in the skill list while unloaded
    async def before_turn(self, session, message): ...
    async def after_turn(self, session, message, result): ...
    async def serve(self): ...                              # long-running, e.g. polling a channel
    def webhooks(self): return {"event": self.on_event}
    def worker_functions(self): return {}                   # "<plugin>:<name>" workers
```

Use it from bot.yaml by dotted path, or register a short name in
`DJANGO_ERGO["BOT_PLUGINS"]`:

```yaml
plugins:
  - name: myapp.plugins:CRMPlugin
    base_url: https://crm.example.com      # every other key is self.config
```

Hooks may be sync or async. `serve()` runs in `ergonaut bots` (one
process for all bots). Webhooks are served at
`/hooks/<bot>/<plugin>/<name>/`; `self.webhook_url(name)` gives the public
URL once `DJANGO_ERGO["BOT_WEBHOOK_BASE_URL"]` (Ergonaut:
`ERGONAUT_PUBLIC_URL`) is set. Handlers check their own signatures.

## Official plugins

| Plugin | Gives the bot |
| --- | --- |
| `ergo_kb` | knowledge base search, reading and (with `write: true`) note-taking; added for any `kb/` folder |
| `bot_management` | read, edit and publish its own config repo, as a PR or straight to main |
| `pages` | write live `.jhtml` pages into a chat and pin them |
| `attachments` | list, read, write, look at and archive chat files |
| `telegram` | a Telegram bot, by webhook or polling, with approval buttons |
| `orca` | the Orca CLI: worktrees, terminals and supervised coding workers |
| `bash` | shell commands on the host, each approved by default |

Options for each are in the [bot reference](bots.md#official-plugins).
