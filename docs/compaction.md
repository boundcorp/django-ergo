# Session compaction

Bots keep native message history, including tool exchanges, and use
`context_size` by default. Set `ConversationSession.compaction_mode` and
`compaction_config`, or pass them to `SessionManager.create_session(...)`.
Before each turn the runtime checks whether to summarize older messages.

| Mode | Compacts when | Config defaults |
| --- | --- | --- |
| `none` | never | |
| `time` | the last message is older than `idle_seconds` | `idle_seconds=3600`, `keep_recent=0` |
| `context_size` | prompt usage or estimated unsummarized history reaches the threshold | `compact_at_tokens=75%` of window, `keep_tokens=25%` of window |

`rolling` and its old alias `stream` are read as `context_size`. The old
`max_context_tokens` overrides the threshold when `compact_at_tokens` is
unset. `keep_recent`, `batch` and `min_tokens` are ignored by this policy.
Time compaction retains its message-count `keep_recent` setting.

Context size is the larger of the last assistant message's input plus cache
read/write tokens and an estimate of the JSON message content since the
latest compaction (four characters per token). Output usage does not count.
This estimate makes old main chats with small window prompts compact their
full history on their first native-history turn.

The engine window defaults to 200,000 tokens, or 1,000,000 for a model name
ending in `[1m]`. Set a model's `context_window` in `providers.yaml` (or its
`config`) to override it. CLI model names retain `[1m]`; labels and pricing
ignore it.

## What happens on compaction

The newest messages are kept until their estimated tokens reach
`keep_tokens`. The cut moves back to a user turn start, keeping tool calls
and their results together. If one huge turn consumes the kept budget,
nothing is folded. Compaction never deletes messages; history tools still
read every session the user has with the bot.

The folded transcript uses `ConversationRenderer(detail="digest")`: tool
arguments are capped at 300 characters, results at 1,500, with omitted
character counts and error markers. The summary preserves findings such as
paths, ids, numbers, errors and conclusions. Transcripts are chunked on
message boundaries at 100,000 estimated tokens or 40% of the window,
whichever is smaller. An individually oversized message is indivisible and
occupies its own chunk. Each chunk incorporates the previous summary; only
the final summary becomes one `ConversationCompaction` covering the entire
fold. Every chunk remains a recorded standalone `compaction` structured
call, and the compaction links to the final call.

A failed or empty summary leaves history uncompacted and the next turn
continues. Setting the mode to `none` ignores stored compactions and sends
full history. `compact_session(session, engine, keep_tokens=N)` summarizes
on demand; explicit `keep_recent=N` remains available to library callers.
Pre-seeded tool exchanges run again after the summary folds their seeded
call, as well as once on the session's first turn.

## Inspecting context in Ergonaut

Each assistant reply's **Context** button shows recorded prompt usage and
window size, estimated context and compaction threshold, section token
counts and expandable text, native message count and oldest sequence, and
the number of stubbed tool results. The active compaction expands to its
summary. History shows a clickable divider after each summarized range.
Older calls made before this feature have no recorded context.

The first model request of each in-session structured call records
`metadata["context"]`. Section text is capped at 20,000 characters each.
The call's existing usage fields aggregate all model requests in that call;
the panel's prompt usage is therefore the turn total, while its assembled
context snapshot describes the first request.

## Existing-session migration

Migration `0031_native_token_compaction` removes `native_history: turn` from
bot sessions and switches them to `context_size`. Every `rolling`/`stream`
session switches to `context_size` and drops `keep_recent`, `batch` and
`min_tokens`. Other config keys, messages, tool output and summaries stay.
Already migrated sessions are unchanged on re-run. A private session
metadata backup stores the original policy for reversal; reversal restores
it if the policy still matches the migrated version, preserving later
policy edits. No schema change is needed.

During rollout, quiesce turns while migrating. The first turn of a long main
chat can make several summary calls, increasing initial latency and usage.
Ergonaut deployments that auto-upgrade will apply the migration after merge;
Lee's explicit approval is required before merging this change.
