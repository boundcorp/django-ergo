# Session compaction

Set `ConversationSession.compaction_mode` and `compaction_config`, or pass them
to `SessionManager.create_session(...)`. Before each turn,
`run_conversation_turn` and structured turns call `maybe_compact`, which
checks the policy and, when it applies, folds older messages into a summary.

| Mode | Compacts when | Config (defaults) |
| --- | --- | --- |
| `none` | never | |
| `time` | the last message is older than `idle_seconds` | `idle_seconds=3600`, `keep_recent=0` |
| `context_size` | the last model call's prompt plus output exceeds `max_context_tokens` (cached tokens count) | `max_context_tokens=100000`, `keep_recent=6` |
| `rolling` | more than `keep_recent + batch` messages sit past the last summary and the last model call's prompt plus output reached `min_tokens` (cached tokens count) | `keep_recent=15`, `batch=10`, `min_tokens=80000` |

`rolling` was called `stream` before; `stream` is still accepted (in bot.yaml
and on stored sessions) and read as `rolling`. Don't confuse it with window
chats ([context-builder.md](context-builder.md)), which rebuild context every
turn instead of summarizing.

`keep_recent` counts stored engine messages, and tool calls and tool results
count as messages. The cut always moves back to a user message that starts a
turn, so a tool call is never separated from its result.

Each compaction changes the start of the prompt, so the next model call
writes a fresh prompt cache (billed near full input price) instead of reading
the cached prefix cheaply. On a small context that costs more than the
summary saves, and a tool-heavy turn adds many messages but few tokens, so
`rolling` waits for `min_tokens` too. Set `min_tokens: 0` to compact on
message count alone.

## What happens on compaction

- A `ConversationCompaction` row stores the summary, the reason, and the
  covered range (`from_sequence` to `upto_sequence`).
- Summaries roll forward: each new summary is written from the previous
  summary plus the newly folded messages, so only the latest one is used.
- When engines rebuild context, they replace the covered messages with one
  `<conversation-summary>` user message (placed after the system message for
  OpenAI). The message rows are never deleted, so renderers and history
  tools still see everything.
- Each summary is a standalone [structured call](structured-calls.md) of
  kind `compaction` on the session's engine. It returns a
  `CompactionSummary` (summary text, decisions, open items), and the
  compaction row links to it through `structured_call`, so every summary's
  tokens, failures and transcript are on record. Pass `summarizer=` (an
  async `(previous_summary, transcript)` returning text or a structured call
  result) to `maybe_compact` or `compact_session` to use something else.
- If summarizing fails (including a failed structured call), the turn goes
  ahead without compaction.
- Setting a session back to `none` restores full context. Stored
  compactions are kept and ignored.

`compact_session(session, engine, keep_recent=N)` compacts on demand.
