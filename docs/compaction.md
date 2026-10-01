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
| `stream` | more than `keep_recent + batch` messages sit past the last summary | `keep_recent=15`, `batch=10` |

`keep_recent` counts stored engine messages, and tool calls and tool results
count as messages. The cut always moves back to a user message that starts a
turn, so a tool call is never separated from its result.

## What happens on compaction

- A `ConversationCompaction` row stores the summary, the reason, and the
  covered range (`from_sequence` to `upto_sequence`).
- Summaries roll forward: each new summary is written from the previous
  summary plus the newly folded messages, so only the latest one is used.
- When engines rebuild context, they replace the covered messages with one
  `<conversation-summary>` user message (placed after the system message for
  OpenAI). The message rows are never deleted, so renderers and history
  tools still see everything.
- The default summarizer makes one stateless `engine.generate()` call. Pass
  `summarizer=` (an async `(previous_summary, transcript) -> str`) to
  `maybe_compact` or `compact_session` to use something else.
- If summarizing fails, the turn goes ahead without compaction.
- Setting a session back to `none` restores full context. Stored
  compactions are kept and ignored.

`compact_session(session, engine, keep_recent=N)` compacts on demand.
