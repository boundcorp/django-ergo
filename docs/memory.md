# Memory and knowledge bases

A bot remembers things in four places. Pick by how the knowledge changes and
who should review it:

| Where | What goes there | Who changes it |
| --- | --- | --- |
| Chat history | everything said, every tool call | grows on its own; searchable forever |
| `kb/` folder | facts, preferences, reference notes, in Markdown | you (reviewed), or the bot (`write: true`) |
| Tables | structured records: rows with fields | tools, schedules and the bot (see [Data tables](tables.md)) |
| `agents.md` and skills | how the bot behaves | you, or the bot through `bot_management` |

## Chat history

Nothing is ever deleted from a chat. A main chat sees only a window of
recent messages each turn, and older threads fold into summaries
([compaction](compaction.md)), but the full record stays readable through
the `history` skill, which every chat has loaded:

| Tool | Does |
| --- | --- |
| `ergo_chat_history_sources` | the conversations it can read |
| `ergo_chat_history_search` | search messages by text |
| `ergo_chat_history_tail`, `ergo_chat_history_read` | page backwards and forwards by line |
| `ergo_chat_history_around` | messages around a line |
| `ergo_chat_history_by_date` | messages in a date range |

In any chat or thread these cover every session the bot has had with
that person, threads included. Each tool
takes a granularity: `conversation` (what was said), `reasoning` (plus
thinking and short tool calls) or `full`. See
[message history](message-history.md).

So the bot doesn't need to write down what was said. Use the KB for things
that should be true going forward.

## The kb/ folder

A `kb/` folder in the bot folder is its knowledge base. No configuration is
needed: the `ergo_kb` plugin is added automatically with `path: kb`.

```
kitchen/
  kb/
    index.md                 # always in context
    household.md
    preferences/diet.md
```

- Each `.md` file is an article, titled by its first `# Heading` (else its
  file name).
- `kb/index.md` (or `README.md`) is the root article. It is in context on
  every turn under "Knowledge base: <title>", whether or not the `kb` skill
  is loaded. Keep it short: who the bot serves, standing facts, and a map of
  the other articles.
- The `kb` skill gives `ergo_kb_search` (word matches in titles and
  bodies, no index or embeddings), `ergo_kb_read(path)` and `ergo_kb_list`.
- Files are read fresh on every call, so edits take effect at once.

Ergonaut's **Memory** page (from the bot's page) shows the articles.

### Prefetch

Prefetch searches the KB with the person's message and puts matching
articles into that turn's context, so the bot sees them without a tool
call. Results aren't stored in the chat.

```yaml
plugins:
  - name: ergo_kb
    path: kb
    prefetch: every_turn   # new_session (default): a chat's first turn only; off
    top_k: 5
    weight: 1              # share of the turn's context budget
```

Main chats rarely start over, so `every_turn` suits bots that mostly talk
in their main chat. With a folder KB a turn with no match adds nothing.

### Letting the bot write

```yaml
plugins:
  - name: ergo_kb
    path: kb
    write: true            # adds ergo_kb_write(path, content)
    commit: true           # default: commit and push each write
```

The bot can then save `.md` files inside the KB folder, and nothing else.
In a git checkout each write is committed and pushed to the current branch
immediately, without review. Tell the bot in `agents.md` what is worth
saving, for example:

```markdown
When Lee states a lasting preference ("we don't eat pork"), save it to
kb/preferences/ with ergo_kb_write. Don't save one-off requests.
```

For changes you want to review first, leave `write` off and add the
`bot_management` plugin: the bot edits KB files with
`ergo_config_repo_write` and proposes them as a pull request.

### Sharing a KB

`path` may point next to the bot folder in the same repo (`../kb`), so
several bots can share one folder. Only one should write to it.

## Other knowledge bases

`ergo_kb` can serve other sources instead of a folder:

```yaml
plugins:
  - name: ergo_kb
    knowledgebases: [Kitchen, Recipes]     # Knowledgebase rows in the database
  - name: ergo_kb
    toolkit: "myapp.kb:make_toolkit"       # any factory(ctx) -> Toolkit
    search_tool: crm_search               # prefetch calls it with {query, top_k}
```

- `knowledgebases` uses Ergo's database KB (the `legacy` extra): articles
  with embeddings and semantic search, managed in the admin or with
  `django_ergo.kb_tools`. Tools: `kb_list`, `kb_search`, `kb_get_article`,
  `kb_table_of_contents`. See [semantic search](semantic-search.md).
- `toolkit` takes any toolkit, such as a `CorpusToolkit` over the
  [knowledge foundation](knowledge-foundation.md)'s memory, database or Git
  corpora with lexical, semantic and hybrid search and reviewed writes.
  Set `search_tool` to its search tool for prefetch.

## Choosing

- A fact the bot should always act on: `kb/index.md`.
- A fact it needs sometimes: another KB article, found by search or
  prefetch.
- Something it learns from people as it goes: a KB with `write: true`.
- A list of records you'll filter, sum or chart: a [table](tables.md).
- A procedure: a [skill](skills.md).
- What happened in a past conversation: nothing to do; it's in the history.
