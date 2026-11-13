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
it. A plugin without tools is always active, and so are a plugin's hooks
(its channel, webhooks and turn hooks) whether its skill is loaded or not.

### Official plugins

| Plugin | Skill | Gives the bot |
| --- | --- | --- |
| `ergo_kb` | `kb` | search, read and optionally write its knowledge base; added automatically for a `kb/` folder ([Memory](memory.md)) |
| `bot_management` | `config_repo` | read, edit and publish its own repo, as a PR or straight to main |
| `pages` | `pages` | write live `.jhtml` pages into a chat and pin them |
| `attachments` | `attachments` | list, read, write, look at and archive chat files ([Attachments](attachments.md)) |
| `telegram` | `telegram` | a Telegram bot, by webhook or polling: one shared thread or a chat per person, voice and photos, approval buttons, `ergo_telegram_message` ([bots.md](bots.md#telegram)) |
| `orca` | `orca` | the Orca CLI: worktrees, terminals and supervised coding workers |
| `local_shell` | `local_shell` | agent CLIs (Claude Code, Codex) on briefs in job folders on this host, watched as workers |
| `bash` | `bash` | shell commands on the host, each approved by default |
| `browser` | `browser` | a running Chrome over CDP: open, read, click, type, screenshot ([Browser control](browser.md)) |
| `kubectl` | `kubectl` | configured-cluster Kubernetes inspection and individually approved changes with redacted previews |
| `web_search` | `web_search` | `ergo_web_search` and `ergo_web_fetch` on every engine, through Brave, the Claude Code subscription, or Anthropic's search tool |

`bash`, `browser`, `orca` and `kubectl` default to `root_only: true` (top-level chats
only, never threads); `bot_management` defaults to `root_only: false`, so a
thread can change its own bot folder. All of them ask for approval before changing
anything. `kubectl` always requires individual approval and shows a bounded,
redacted server-side preview for supported changes. In Ergonaut only admins
can approve their tools. Every option is in the
[bot reference](bots.md#official-plugins).

## Offline Runpod bridge

`django_ergo.plugins.runpod` contains local queue/experiment building blocks,
a single-create REST controller with offline fixtures, an explicit
image-conditioned H3 pilot worker, and independent cleaner supervision. It is
**not** an enabled bot plugin or deployed rental controller and adds no
bot.yaml configuration.
The cleanup command remains dry-run by default:

```bash
python -m django_ergo.plugins.runpod.cleanup LEDGER
```

Run the no-network create/recovery fixtures locally:

```bash
PYTHONPATH=src pytest -q tests/test_runpod_create.py tests/test_runpod_cleanup.py tests/test_runpod_transport.py
```

`RESTCleanup.configure_create` binds an immutable account, attempt and exact
request profile: one GPU, SECURE cloud, an image and container disk, explicit
`volumeInGb: 0`, no template and no network/global-volume option. Its SHA256
fingerprint covers the canonical request before adding
`ERGO_ATTEMPT_FINGERPRINT`. Ownership checks require the account, attempt,
fingerprint and matching profile; a nonunique Pod name never authorizes deletion.
The existing `configure(Ownership)` path is cleanup-only, without a create body.

`create_once` without a client is a dry run: no POST or create intent.
The controller's fixture mode uses an injected-opener `FixtureRunpodREST`.
Its explicit live mode additionally requires a bound live configuration,
an executing independent fresh cleaner and a named credential callback;
the live path has not been exercised against Runpod. The ledger durably
records `create_unknown`/`create_started`
before the one POST. A timeout, restart, empty response or failed receipt write
does not authorize replay; a subsequent create attempt is rejected. A valid
returned ID is only a receipt, never evidence of running or generation.

Each reconciliation tick performs one list scan and at most ten resource
actions. Later scans can discover owned resources after an uncertain create,
but repeated list absence never makes that unknown create clean. A known-ID
GET 404 establishes compute absence; DELETE 404 is ambiguous and needs a later
GET 404. Deletion timeouts retain pending state for retry. Bounds cover request
count, response size and per-socket timeout. The optional independent supervisor
bounds each cleanup child process and gates the worker on heartbeat freshness;
no scheduler has been installed or provider billing guarantee proved.

### Public API evidence and remaining uncertainty

- [List Pods](https://docs.runpod.io/api-reference/pods/GET/pods) and
  [Get Pod](https://docs.runpod.io/api-reference/pods/GET/pods/podId) document
  `consumerUserId` as the renting user, plus `env`, `id`, `image`, `volumeInGb`
  and `networkVolume`; reads request `includeNetworkVolume=true`. Names are
  not unique. These are documented fields, not undocumented ownership guesses.
  The schemas leave fields optional and do not establish strong list
  consistency or guarantee that no network volume is represented by explicit
  null. Missing ownership evidence cannot be substituted with a name.
- [Create Pod](https://docs.runpod.io/api-reference/pods/POST/pods) documents
  a 201 Pod response, `imageName`, `gpuTypeIds`, `gpuCount`, `env`, optional
  `networkVolumeId`/`templateId`, and a default `volumeInGb` of 20; the strict
  request explicitly overrides that default with zero.
- [Delete Pod](https://docs.runpod.io/api-reference/pods/DELETE/pods/podId)
  documents 204 success, 400 and 401, **not** DELETE 404 as successful deletion.
- [Storage types](https://docs.runpod.io/pods/storage/types) distinguish ephemeral
  container storage and Pod-deleted volume disks from independently persistent
  network/global volumes, with network storage attached at creation. Only the
  immutable strict no-volume create profile, an ownership-checked Pod and a
  later GET 404 permit storage absence to be concluded. That profile accepts
  an omitted or null `networkVolume` only alongside integer-zero `volumeInGb`
  and exact account/environment/image/disk checks: this relies on the request's
  absence of attachment options, not a guaranteed null response. Legacy
  cleanup-only configuration still requires explicit null for its profile
  check; legacy or otherwise insufficient storage evidence remains
  `storage_unverified`. This profile proves what this bridge requested, not the
  absence of later out-of-band global-volume attachments; those need separate
  evidence and must remain unresolved if external changes are possible.

The standalone controller accepts live create only with explicit configuration
and execution flags, bound quote and independent cleaner freshness. The
cleanup command's `--execute --credential-env NAME` uses the same callback
pattern. Neither route was exercised against Runpod; verify the account binding,
image delivery and worker access before any rental.

The [frozen H3 worker guide](runpod-h3.md) gives the executable validation and
six-shot inference commands, pinned FL2VA image-conditioning schema, frame
lattice/29-second PCM-preserving export, concrete host/price estimate, and
remaining unmeasured conditioned-fit/startup limits. The
[independent cleaner guide](runpod-cleaner.md) provides two-process supervision,
freshness gates and killed-worker fixture evidence. These are local preparation,
not a deployment or paid-call authorization. The $3 plan is an evidenced estimate
with margin and an immutable ≤1-hour deadline, **not** a zero-overrun guarantee
gate. Single-create/no-replay and cleanup retries remain required. Existing bot
configurations and chapter readiness remain unchanged.

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

An automated webhook notification is not a human channel message. Queue it
through `messaging.send_event`, rather than calling `bot.ask` with the
chat owner's default identity:

```python
from asgiref.sync import sync_to_async
from django_ergo.bots import messaging

await sync_to_async(messaging.send_event)(
    session,
    "Checks failed for the watched pull request.",
    source="github",
    source_label="GitHub",
    registry=self.bot.registry,
    metadata={"pull_request": pull_request_url, "checks": "failing"},
)
```

The source is stored as a `system` author and `event` provenance, while the
body stays unchanged. The model sees an `[Event from GitHub: automated
notification, not a message from the user. …]` header. Events use the same
queue and wake-up behavior as delegated messages, but do not impersonate
the person or authorize actions that require their approval. Keep genuine
human channel input on `bot.ask` (with its actual author), and bot-to-bot
messages and reports on `messaging.send(sender_session, ...)` so their
reply/report routing and bot attribution remain intact.

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
