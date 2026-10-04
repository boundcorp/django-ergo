# Message history: sources, granularity, and the history toolkit

`django_ergo.conversation.history` reads conversations from different places
into one record shape. `MessageHistoryToolkit` gives an agent tools to page
and search those records.

## Sources

| Source | Reads | `source_id` | `line` is |
| --- | --- | --- | --- |
| `SessionSource(session)` | a `ConversationSession` (its `SessionMessage` rows, plus attachments) | `session:<uuid>` | message sequence |
| `ClaudeCodeSource(path)` | a Claude Code transcript, `~/.claude/projects/*/<id>.jsonl` | `claude:<id>` | JSONL line number |
| `CodexSource(path)` | a Codex CLI rollout, `~/.codex/sessions/**/rollout-*.jsonl` (new and legacy formats) | `codex:<date-id>` | JSONL line number |

`sources_from_paths([...])` walks files and folders and picks the right
source for each transcript. `default_cli_paths()` returns the usual Claude
Code and Codex folders that exist on the machine.

Each `HistoryMessage` has `source_id`, `line`, `timestamp` (timezone-aware),
`role` (`user`, `assistant`, `tool`, `system`) and normalized `blocks`
(`text`, `thinking`, `tool_use`, `tool_result`, `attachment`, `context`).
Text injected by the CLI (environment context, slash-command output, system
reminders) becomes a `context` block rather than user text.

## Granularity

| Level | Shows |
| --- | --- |
| `conversation` (default) | what the user and the assistant said, plus attachment labels |
| `reasoning` | adds thinking, tool calls with short arguments, and tool results as line counts plus a preview |
| `full` | adds tool inputs and results verbatim, plus injected context |

Messages with nothing to show at a level are skipped, but they keep their line
numbers, so paging stays stable when you switch levels.

Rendered messages look like
`[claude:abc L42 2026-09-01T10:00:08+00:00 ASSISTANT] You have 4 eggs.`.
The source id, line and timestamp are the keys the tools accept.

## Toolkit

```python
from django_ergo.conversation.history import SessionSource, sources_from_paths, default_cli_paths
from django_ergo.conversation.history_search_toolkit import MessageHistoryToolkit

own_history = MessageHistoryToolkit([SessionSource(session)])        # search its own thread
everything = MessageHistoryToolkit(
    [SessionSource(s) for s in sessions] + sources_from_paths(default_cli_paths())
)
await run_conversation_turn(engine, session, message, extra_tools=[own_history])
```

| Tool | Does |
| --- | --- |
| `ergo_chat_history_sources` | lists sources with message counts and date ranges |
| `ergo_chat_history_read` | pages by line: `start_line` goes forward, `end_line` alone reads the messages before it |
| `ergo_chat_history_tail` | latest N messages (default 15) |
| `ergo_chat_history_around` | `before`/`after` messages around a line, to expand a hit |
| `ergo_chat_history_by_date` | `since`/`until` window across sources, oldest first |
| `ergo_chat_history_search` | all words must match, searching full content including tool I/O, newest first |

Every read takes `granularity`. Results end with the next call to make, for
example `More: ergo_chat_history_read start_line=57`. `source_id` is optional when the
toolkit holds a single source. Session sources re-read the database on each
call, so a live session sees its own new messages.

## Importing

`ImportService().import_auto(records, user)` (and the `import_conversations`
command) now accepts Codex rollouts as well as Claude Code transcripts. Both
importers keep each message's original timestamp in `created_at`, which
date-range reads rely on.
