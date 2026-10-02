# Getting started

This runs the example `hello` bot in Ergonaut, then adds a tool to it.

## 1. Install

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Node (for the web
app).

```bash
git clone https://github.com/boundcorp/django-ergo
cd django-ergo/ergonaut
make venv                      # .venv with Ergo (editable, from ..) and Ergonaut
source .venv/bin/activate
(cd frontend && npm install && npm run build)
```

## 2. Point Ergonaut at a bot

```bash
export ERGONAUT_BOTS=../examples/hello
export ANTHROPIC_API_KEY=sk-ant-...      # hello uses the Claude engine
ergonaut check
```

`ergonaut check` loads every bot and prints its skills, tools, plugins,
people and any secrets it can't find. Fix what it reports before going on.

With no `DATABASE_URL`, Ergonaut runs an embedded PostgreSQL (with pgvector)
under `~/.ergonaut/`, so there is nothing else to install.

## 3. Start it

```bash
ergonaut up
```

The first start migrates the database. Then, in a second terminal (it finds
the running `up` and uses its database):

```bash
source .venv/bin/activate
ergonaut manage createsuperuser
```

Open http://localhost:8000 and sign in. The sidebar lists your bots; pick
**hello** to open your main chat with it, then ask it to roll some dice.

You can also talk to a bot from the terminal:

```bash
ergonaut chat hello
```

`ergonaut up` also starts `redis-server` and `garage` when they're on your
`PATH`, then runs the web server, Celery workers, Celery beat and the bots'
channels (Telegram). Without Redis there is no worker or beat: turns run
inside the web process and schedules never fire, so install Redis once you
want either. See [Running Ergonaut](ergonaut.md) for the details and for Docker.

## 4. Make your own bot

Copy the example somewhere outside the repo, ideally its own git repository
(the bot can later propose changes to it):

```bash
cp -r ../examples/hello ~/my-bots/notes
cd ~/my-bots/notes && git init
```

Edit `bot.yaml`:

```yaml
name: notes
description: Keeps my running notes and reminders
engine:
  type: claude
  config: {model: claude-sonnet-5-5}
tools: [tools/notes.py]
```

`agents.md` is the system prompt. Write it the way you'd brief a person:
who the bot is, who it works for, and how it should behave.

```markdown
You are Notes, Lee's note keeper. Keep replies short.
When Lee mentions something to remember, save it with add_note.
```

Add `tools/notes.py`:

```python
"""Saving and finding notes."""

from django_ergo.bots import bot_tool

NOTES: list[str] = []

@bot_tool
def add_note(text: str) -> str:
    """Save a note."""
    NOTES.append(text)
    return f"Saved ({len(NOTES)} notes)."

@bot_tool
def find_notes(query: str) -> list[str]:
    """Notes containing the query."""
    return [n for n in NOTES if query.lower() in n.lower()]
```

Type hints become the tool's parameters and the docstring its description.
The module docstring describes the `notes` skill (see [Skills](skills.md)).

Point Ergonaut at it and restart:

```bash
export ERGONAUT_BOTS=~/my-bots/notes
ergonaut check && ergonaut up
```

While Ergonaut runs, it reloads a bot when its files change, so edits to
`agents.md`, `bot.yaml` and tools reach the next turn without a restart.

Notes kept in a Python list vanish on restart. For data that lasts, give the
bot a [table](tables.md) or a [knowledge base](memory.md) folder it can
write to.

## Next

- [Building bots](building-bots.md): chats, threads, sub-bots, knowledge, tables
- [Tools](tools.md): context, secrets, approvals, background work
- [Memory and knowledge bases](memory.md): what the bot remembers and how it learns
- [Schedules](schedules.md): have the bot do things on its own
