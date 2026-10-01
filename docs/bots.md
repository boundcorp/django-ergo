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
sessions:
  allow_create: true                 # may the root start threads?
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
| `threads_list` | This bot's threads with the user |
| `threads_create` | Start a thread (needs `sessions.allow_create`), optionally with a first message whose reply is returned |
| `threads_send` | Message a thread and return its reply |
| `threads_close` | Close a thread; its history stays readable |
| `bots_call` | Message a bot in `permissions.call_bots` (needs a `BotRegistry`) |

Threads are ordinary sessions with the bot's default compaction mode. A
thread that stops for approval reports that back to the root. Set
`orchestration: false` for a bot that only ever talks in its root session:
the thread and `bots_call` tools are left out entirely.

`BotRegistry.discover("bots/")` loads every subfolder with a `bot.yaml` and
lets bots find each other by name.

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

## Built-in plugins

### ergo_kb

```yaml
- name: ergo_kb
  path: kb                           # Markdown folder, relative to the bot folder
  # or knowledgebases: [Kitchen], or toolkit: "myapp.kb:make_toolkit"
  prefetch: new_session              # new_session | every_turn | off
  search_tool: kb_search
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
article titled by its first `# Heading`. The bot gets `kb_search` (keyword
search), `kb_read` and `kb_list`, and prefetch adds matching articles to the
context only when something matches. Add the `bot_management` plugin and the
bot can edit articles with `repo_write` and propose them with
`repo_publish`, which is how it keeps notes such as household preferences up
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
change its own instructions, config and tools. `repo_status`, `repo_list`,
`repo_read`, `repo_diff` and `repo_write` work on the working copy (paths
can't leave the repo or touch `.git`). `repo_publish` commits everything,
then either rebases on main and pushes (`merge_main`) or pushes a
`bot/<name>/<time>-<title>` branch and opens a pull request with `gh`
(`propose_pr`), returning to main afterwards. It needs approval unless
`approve_publish: false`. `repo_pull` fast-forwards main and `repo_prs`
lists open pull requests. Changes take effect when the bot is loaded again.

### telegram

```yaml
- name: telegram
  token_env: KITCHEN_TELEGRAM_TOKEN
  users: {123456789: lee}    # chat id -> Django username
```

`bot.serve()` long-polls the Bot API. Messages from listed chats go to that
user's root session; other chats are ignored. Photos, voice notes, audio
and documents become attachments. A turn that stops for approval replies
with Approve and Deny buttons that resume it. `plugin.notify(user, text)`
sends a message from other code.
