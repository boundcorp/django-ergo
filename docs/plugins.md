# Plugins

A plugin is a Python class that gives a bot more than tools: context on
every turn, hooks around the bot's lifecycle, long-running services such as
a chat channel, webhooks, and background workers. Ergo's own integrations
(knowledge base, Telegram, self-management) are plugins, and yours work the
same way.

Reach for a plugin when one of these is true:

- several bots should share the integration, configured per bot;
- it needs context or hooks, not just tools;
- it runs a service (polling, a webhook) or connects a channel.

For tools belonging to one bot, a [tool file](tools.md) is simpler.

## Using plugins

List them in bot.yaml. `name` picks the plugin; every other key is the
plugin's config:

```yaml
plugins:
  - name: ergo_kb               # an official plugin, by short name
    path: kb
    write: true
  - name: telegram
    token_env: KITCHEN_TELEGRAM_TOKEN
  - name: myapp.plugins:CRMPlugin   # yours, by dotted path
    base_url: https://crm.example.com
```

A plugin with tools is one [skill](skills.md), loaded when the chat needs
it. A plugin without tools (like `telegram`) is always active.

### Official plugins

| Plugin | Skill | Gives the bot |
| --- | --- | --- |
| `ergo_kb` | `kb` | search, read and optionally write its knowledge base; added automatically for a `kb/` folder ([Memory](memory.md)) |
| `bot_management` | `config_repo` | read, edit and publish its own repo, as a PR or straight to main |
| `pages` | `pages` | write live `.jhtml` pages into a chat and pin them |
| `attachments` | `attachments` | list, read, write, look at and archive chat files ([Attachments](attachments.md)) |
| `telegram` | | a Telegram bot, by webhook or polling, with approval buttons |
| `orca` | `orca` | the Orca CLI: worktrees, terminals and supervised coding workers |
| `bash` | `bash` | shell commands on the host, each approved by default |
| `kubectl` | `kubectl` | configured-cluster Kubernetes inspection and approved changes; Secret values redacted |

`bash`, `orca`, `kubectl` and `bot_management` default to `root_only: true`
(top-level chats only, never threads) and ask for approval before changing
anything.
In Ergonaut only admins can approve their tools. Every option is in the
[bot reference](bots.md#official-plugins).

## Writing a plugin

```python
# myapp/plugins.py
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import FunctionToolkit, bot_tool
from django_ergo.conversation.context import TextContextSource


class CRMPlugin(BotPlugin):
    name = "crm"
    description = "Look up customers and log calls in the CRM"   # the skill listing

    def on_load(self):
        # Runs once, when the bot is built. Validate config here: an error
        # marks the bot folder as broken instead of failing mid-chat.
        self.base_url = self.config["base_url"]
        self.approve_writes = bool(self.config.get("approve_writes", True))

    @property
    def skill_instructions(self) -> str:
        # What the model reads when it loads the skill.
        return "Customers are keyed by email. Log every call you discuss."

    def toolkits(self, ctx):
        client = CRMClient(self.base_url, ctx.secret("CRM_TOKEN"))

        @bot_tool
        def crm_find(email: str) -> dict:
            """A customer by email."""
            return client.customer(email)

        @bot_tool(requires_approval=self.approve_writes)
        def crm_log_call(email: str, summary: str) -> str:
            """Log a call with a customer."""
            client.log(email, summary)
            return "Logged."

        return [FunctionToolkit.from_functions([crm_find, crm_log_call], ctx)]

    def context_sources(self, ctx, message):
        # Extra context each turn while the skill is loaded.
        return [TextContextSource("Open deals", lambda: open_deals_summary(self.base_url))]

    def skill_hint(self, ctx) -> str:
        # One line in the skill listing while it's not loaded.
        return "3 open deals"
```

Use it:

```yaml
plugins:
  - name: myapp.plugins:CRMPlugin
    base_url: https://crm.example.com
```

Or give it a short name for every bot in the project:

```python
DJANGO_ERGO = {"BOT_PLUGINS": {"crm": "myapp.plugins.CRMPlugin"}}
```

### What a plugin has

| | |
| --- | --- |
| `self.bot` | the `Bot` it belongs to (`self.bot.name`, `self.bot.definition`, `self.bot.table(...)`) |
| `self.config` | its bot.yaml entry, minus `name` |
| `ctx` (in `toolkits`, context and hints) | a [`ToolContext`](tools.md#the-tool-context): session, user, `ctx.secret(...)` |

Build tools per call to `toolkits(ctx)` so they can close over the session
and user. To keep a tool out of threads, return `[]` when
`not self.bot.is_root(ctx.session)`.

### Hooks

Override any of these; each may be sync or async.

| Hook | When |
| --- | --- |
| `on_load()` | the bot was built (also on every reload) |
| `toolkits(ctx)` | each model call while the skill is loaded |
| `context_sources(ctx, message)` | each turn while the skill is loaded |
| `always_context_sources(ctx, message)` | each turn, loaded or not (the KB's root article) |
| `on_session_created(session)` | a chat or thread was created |
| `before_turn(session, message)` | before a turn |
| `after_turn(session, message, result)` | after a turn; `result.reply`, `result.text`, `result.approvals` |
| `on_session_closed(session)` | a session was closed |
| `serve()` | long-running work; see below |

`skill_requires` (a list of skill names) loads other skills along with
this one, the way `pages` loads `tables`.

Context sources are `ContextSource`s from `django_ergo.conversation.context`
(`TextContextSource(title, text_or_callable, weight=...)` covers most
cases). They share the turn's context budget by weight and are trimmed to
fit. A source that raises is logged and skipped.

## Channels: serve() and webhooks

A channel plugin connects a bot to somewhere people already talk.
`serve()` runs for the life of the process: Ergonaut runs every bot's
`serve()` in the `ergonaut bots` process (one replica). A plugin that only
answers webhooks can register them in `serve()` and return.

To run a turn from a channel, find the person's chat and ask:

```python
session = await self.bot.main_session(user)
result = await self.bot.ask(session, text, attachments=files or None)
if result.approvals:
    ...   # show buttons; later: await self.bot.resume(session, approved)
else:
    await send(result.text, buttons=result.suggestions)
```

Replies the bot makes outside your channel (scheduled messages, delegated
work coming back) arrive through `after_turn`, which is how the Telegram
plugin forwards them.

Webhooks:

```python
class GitHubPlugin(BotPlugin):
    name = "github"

    def webhooks(self):
        return {"push": self.on_push}

    async def on_push(self, request):
        verify_signature(request, self.config["secret"])   # handlers check their own secrets
        ...
        return {"ok": True}       # JSON, an HttpResponse, or None for an empty 200
```

Each handler is served at `/hooks/<bot>/<plugin>/<name>/`.
`self.webhook_url("push")` gives the full public URL to register with the
outside service, once `DJANGO_ERGO["BOT_WEBHOOK_BASE_URL"]` is set
(Ergonaut sets it from `ERGONAUT_PUBLIC_URL`). Outside Ergonaut, mount
`django_ergo.bots.urls` and call `django_ergo.bots.webhooks.set_registry(...)`
at startup; see [the reference](bots.md#webhooks).

## Workers

For long-running work started by a tool (a deploy, a coding agent),
return worker functions and start them from tools:

```python
def worker_functions(self):
    return {"watch": self.watch}           # started as "crm:watch"

def watch(self, ctx, job_id: str):        # ctx is a WorkerContext
    status = poll(job_id)
    if status == "running":
        return ctx.again(60, progress="still running")
    return {"job": job_id, "status": status}
```

In a tool: `ctx.workers.start("crm:watch", title="Import", job_id=job_id)`.
The chat shows the worker as busy, and its result comes back to the chat as
a message. See [Workers](tools.md#workers).

## Testing a plugin

Build a bot folder in a temp directory and load it:

```python
from django_ergo.bots.runtime import Bot

def test_crm_plugin(tmp_path, monkeypatch):
    (tmp_path / "bot.yaml").write_text(
        "name: t\nengine: {type: openai}\n"
        "plugins:\n  - name: myapp.plugins:CRMPlugin\n    base_url: http://crm\n"
    )
    (tmp_path / "agents.md").write_text("Test bot.")
    bot = Bot.load(tmp_path)
    plugin = bot.plugin("crm")
    assert plugin.base_url == "http://crm"
```

`ergonaut check` lists each bot's plugins and skills and reports plugins
that fail to load.
