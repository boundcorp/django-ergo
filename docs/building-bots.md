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
      - {name: gpt-6.1-sol, label: Sol, config: {reasoning_effort: high}}
  anthropic:                   # optional: only if you have an Anthropic key
    type: claude
    api_key_env: ANTHROPIC_API_KEY
    models: [claude-sonnet-5-5]
```

OpenAI is the default engine; Anthropic (Claude) is optional and only
needed if you list it here or set `engine.type: claude`.

Models are named `provider/model`. A bot picks one with
`engine: {config: {model: openai/gpt-6.1-sol}}`; a bot with no `engine` uses
`default`. In Ergonaut, the chat header and the New thread page have a
model picker listing every model whose provider's key is set. Any chat can
switch to any of them, and the pick (stored on the chat as
`ConversationSession.model`) applies to every later turn (a running turn
finishes on the model it started with). Messages are stored the same way for every engine
(`SessionMessage` rows of text, tool call, tool result and thinking blocks)
and each engine renders them into its API's format when it sends them, so a
chat moves between OpenAI and Claude models with its history intact. Thinking
from one provider isn't sent to another. Chats left on "Default" follow the
bot's model, even when it moves to another engine.
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

### OpenAI on your ChatGPT subscription

The same `transport: cli` on an `openai` provider runs OpenAI models through
the Codex CLI, on the ChatGPT plan it's logged in with:

```yaml
providers:
  chatgpt:
    type: openai
    transport: cli
    config: {effort: medium}   # optional: command, codex_home, effort, timeout
    models: [gpt-6.1-sol, gpt-6-astra, gpt-6-luna, gpt-5.6-terra]
```

Install the CLI (`npm install -g @openai/codex`) where turns run and log it
in with `codex login`. Each model call starts `codex app-server` once: the
chat history goes in as Responses API items, Ergo's tools as client-run
tools, and the process stops after one response, so Ergo still runs the
tools, approvals and compaction. Codex's own tools, skills, plugins,
sub-agents and code mode are off. Calls refuse to run unless the login is a
ChatGPT account, and `OPENAI_API_KEY` is not passed to the CLI, so usage
never bills an API key. Codex still adds a global `AGENTS.md` from its
`CODEX_HOME` to the prompt; set `codex_home` to a folder without one if
that matters. `thread/inject_items` and client-run tools are experimental
in Codex's app server (checked with Codex 0.160.0).

Anthropic allows this for your own login on the unmodified CLI. It doesn't
allow serving other people's requests on your login: in a deployment other
people use, each of them needs their own login, so keep a CLI provider to
bots only you use.

### Routing by tier

Instead of a fixed model, a chat (the model picker's `auto` entries) or a
bot (`engine: {model: auto/medium}`, or `default: auto/medium` in
providers.yaml) can ask for `auto/<tier>`. Each turn takes the first model
in that tier whose subscription still has room. A chat keeps the model it
had while that model qualifies, so its prompt cache isn't thrown away.

`small`, `medium`, `large` and `xlarge` work without declaring `tiers` or
`agents`. The older names `low` and `high` still work as aliases of `small`
and `large`, in `auto/<tier>` and as `tiers`/`agents` keys.
Ergo builds their defaults only from models actually listed on your
configured `transport: cli` providers; it never adds a provider or model
and never includes an API-key provider in a default tier. Provider names
are yours to choose; defaults match their engine type and exact model name:

| Tier | Candidate order (unlisted models are skipped) |
| --- | --- |
| `small` | Claude Haiku 5.5, GPT-6 Luna, Claude Haiku 4.5 |
| `medium` | Claude Sonnet 5.5, GPT-6.1 Sol, then the `small` pair |
| `large` | Claude Opus 5.5, GPT-6 Sol, then the `medium` pair |
| `xlarge` | Claude Fable 5.1, GPT-6 Astra, then the `large` pair |

If several subscriptions list the same model, they are tried in provider
declaration order. A tier with no matching candidates cannot be used.
Built-in agent choices use `claude` or `codex` at effort `medium`, whatever
the tier. Chat tiers select models only: their engine settings still come
from the provider, model and bot config.

Effort is set apart from the model. Subscription models (Claude Code and
Codex) run at the chat's effort, else the provider's or bot's `effort`
config, else `medium`. A chat picks its effort (`low`, `medium`, `high` or
`xhigh`) with the slider in its thread options; on the OpenAI API it is sent
as `reasoning_effort`, and the Claude API engine ignores it.

Tier names aren't limited to the built-in ones. You can use
any non-empty name without `/`, for both chats and agents. These defaults
are editable in `providers.yaml`: each declared `tiers.<name>` or
`agents.<name>` **replaces that name's entire default list**, independently;
other defaults stay in place. Existing explicit lists remain authoritative,
including an older `high` (now `large`) that uses the same models as `medium`. An empty
list disables that tier. For example:

```yaml
default: auto/medium
providers:
  anthropic:
    type: claude
    transport: cli
    models: [claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5, claude-fable-5-1]
  openai-codex:
    type: openai
    transport: cli
    models: [gpt-6-luna, gpt-6.1-sol, gpt-6-sol, gpt-6-astra]
tiers:                # chat candidates, in preference order
  small: [openai-codex/gpt-6-luna, anthropic/claude-haiku-5-5]  # override a built-in
  research: [anthropic/claude-fable-5-1, openai-codex/gpt-6-astra]  # custom
agents:               # coding agents (ergo_agent_start tier=...), subscriptions only
  large:              # explicit override; not extended or silently rewritten
    - {agent: codex, model: gpt-6.1-sol, effort: high, provider: openai-codex}
  research:
    - {agent: claude, model: claude-fable-5-1, effort: high, provider: anthropic}
routing:
  limits:             # skip a provider once a window is this % used
    - {provider: anthropic, window: five_hour, max_used: 85}
    - {provider: openai-codex, window: weekly, max_used: 80}
    - {provider: anthropic, window: weekly_fable, max_used: 80}
```

Use `engine: {model: auto/research}` in bot.yaml (or choose `auto/research`
in the chat picker), and `tier: research` when starting an agent. Edit the
deployment's providers.yaml to change defaults or add tiers, then restart
Ergonaut and its bot workers so both reload the configuration.

Windows come from two places, both stored in `ProviderUsage`. A periodic
sync runs `omp usage --redact --json` (`DJANGO_ERGO["USAGE_COMMAND"]`; the
Ergonaut worker runs it every 5 minutes, `ERGONAUT_USAGE_SYNC_SECONDS`, `0`
turns it off) and maps each account's limits: `anthropic` feeds the CLI
providers of type `claude` (`five_hour`, `weekly`, and scoped windows such as
`weekly_fable`), `openai-codex` those of type `openai` (Codex reports only
`weekly` on some plans), and accounts with no provider here (Grok:
`weekly_credits`, `weekly_grokbuild`) are stored under their own name and only
shown. Ergo does not assume that a subscription has a 5-hour/weekly pair. The
CLIs also report the windows of the call they just made (Claude's
`rate_limit_event`, Codex's `account/rateLimits`); those are merged in between
syncs. The Fable window only applies to Fable candidates, so an exhausted
Fable allowance can fall back to Opus on the same subscription. Missing
utilization is unknown (`used: null`), not zero.

Every stored window carries `observed_at`. Syncs and full snapshots replace
earlier windows (removing an obsolete Codex 5-hour window); a partial Claude
event updates only the windows it reports, so the others keep their own age.
A window is stale once its own observation is older than 15 minutes. A failed
fetch, or one account that errors, keeps the last values (aging, never
refreshed) and records why in `UsageSync`. The Routing page shows
used/remaining percentages, reset countdowns, status and each window's age.
Windows past their reset time are not used to disqualify a model. A call
refused for a limit counts that reported window as used up until it resets.
A turn refused that way is never retried on its own: the chat's error offers
**Retry on** the tier's next model with room (`POST
/api/sessions/<id>/resume?model=...`), next to Resume, which waits for the
same model. The chat then stays on the model it moved to while that model
qualifies.
Without limits, a provider is skipped only at 98% used. When every candidate
is over a limit, the one with the most room is used. Agent candidates must
name a `transport: cli` provider, so agents never run on an API key.
Explicit chat tier lists may still include a listed API-key model, but
selecting it bills that key; built-in defaults never do.

The same priorities can be written in words, in a `routing.md` next to
providers.yaml:

```markdown
Lean on Claude: use it until its 5-hour window is 85% used.
Keep Codex's weekly window under 80%, since it runs out first.
```

Use configured provider **names**, not engine types, in YAML limits: for the
example above, Codex/GPT priorities target `openai-codex`, not a separate
`openai` API-key provider. The priorities compiler is given this distinction;
older compiled policies are recompiled under the window-aware compiler.

The first turn after the file changes compiles it into limits with one
structured call, in the background; the `routing:` limits apply until it's
done. Ergonaut's **Routing** page shows each subscription's windows against
its limits, what every tier picks now and why, and the chats and workers
recently moved off their first choice. An admin can rewrite the priorities
there; the saved text replaces `routing.md` for that deployment until they
switch back. `DJANGO_ERGO["MODEL_ROUTER"]` replaces the policy with your own
callable `(candidates, usage, rules, current)`. A plugin's `route_turn` hook
can pick per turn instead, with the user's message in hand, and may move a
turn to another tier; the experimental [`decisions`](bots.md#decisions)
plugin picks the tier with an OpenAI Decisions API call.

## agents.md

The system prompt, rebuilt every turn, so edits reach existing chats at
once. Ergo adds the rest around it: the skill list, the current time, the
KB's index article, context functions and the chat's recent history. Keep
`agents.md` about the bot's job and voice; put procedures in skills, which
only load when needed, and facts in `kb/`.

## Chats and threads

Every person gets a **main** chat with each bot. It keeps its history as
real messages, tool calls and results included, and once the context reaches
75% of the model's window, older messages are summarized and the newest 25%
kept verbatim (see [compaction.md](compaction.md)). The model reads further
back with history tools. A main chat never needs resetting.

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

Use `ergo_thread_forward` to hand over the user's actual message rather than
retelling it with `ergo_thread_send`. The destination shows who wrote the
words separately from the bot that forwarded them and the source chat/time;
notes and shared files are separate from the original body. A forwarded
Telegram message keeps its Telegram author. The recipient answers there,
with nothing routed back; ordinary bot sends identify the sending bot and
still route replies back. See [message identity](bots.md#message-identity)
for the storage and compatibility details.

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
