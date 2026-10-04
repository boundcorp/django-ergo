# Spec: simpler, more lenient context and compaction, plus a Context view

Status: approved by Lee (2026-10-04), not yet built. Findings that led here:
the "Context and compaction review" (summary below).

## Why

Today a bot turn loses too much and is hard to inspect:

1. **Main and named chats lose tool history between turns.** They are window
   chats (`native_history: "turn"`): only the current turn is sent as real
   messages. Earlier turns go in as quoted text in an 8,000-token context block
   in the system prompt, at `conversation` granularity, which drops every tool
   call and result. (`bots/runtime.py` `chat_session`, `context_builder`;
   `conversation/window.py`; `compaction.apply_native_window`.)
2. **One long message wipes the recent history.** `MessageContextSource._fit`
   (`conversation/context.py`) stops at the first message that doesn't fit,
   newest first. If the newest message is larger than the section's share
   (about 3,700 tokens with default weights), the section renders nothing and
   the model sees no earlier conversation.
3. **Compaction summaries can't keep what tools found.** `compact_session`
   renders folded messages with `ConversationRenderer(detail="skeleton")`,
   where a tool result is only `[tool_result #3: (40 lines)]`.
4. **Tool results are trimmed by count, across the whole history.**
   `TOOL_RESULTS_IN_CONTEXT=3` keeps only the newest 3 large results in full
   (`conversation/tool_results.py`).
5. **Message counts are the wrong unit** (`recent: 15`, `keep_recent: 15`,
   `batch: 10`). A tool-heavy turn is 20+ messages, a chatty one is 2.
6. **The per-turn context block breaks the prompt cache.** It is appended to
   the system prompt (`engine.ephemeral_context`) and changes every turn (the
   time is to the minute), so each thread turn re-caches its whole history.
7. **Nothing is inspectable.** The assembled context block is never stored,
   and compactions and stubbed results don't show in Ergonaut.

## Model context windows

Claude 5.x models have 1M windows on the API, but through the Claude Code CLI
a plain model name (`claude-opus-5-5`) runs with 200k. The CLI takes
`claude-opus-5-5[1m]` for 1M. So thresholds come from the model's window, not
a fixed number.

- Add an optional `context_window` (tokens) to a model in `providers.yaml`
  (`bots/providers.py` `Model.config` is fine, or a field). Defaults when
  unset: a name ending in `[1m]` is 1,000,000; anything else is 200,000.
- The engine exposes it as `engine.context_window` (set where `make_engine`
  builds it in `bots/runtime.py`; library default 200,000).
- `[1m]` model names must still work everywhere a model name is used:
  strip the suffix for pricing lookups (`pricing.py`) and display labels.
  The CLI engine passes the name through unchanged to `--model`.

## 1. One compaction policy: native history, compacted by tokens

Reuse the existing `context_size` mode (no new enum value, no schema change)
and make it the default for every bot session, main chats included.

Config keys (all optional; old keys still read):

| key | default | meaning |
|---|---|---|
| `compact_at_tokens` | 75% of `context_window` | compact before a turn once the context reaches this |
| `keep_tokens` | 25% of `context_window` | newest messages kept verbatim after compacting |

Old keys: `max_context_tokens` is read as `compact_at_tokens`. `keep_recent`
(a message count) is ignored by the new policy.

Decision (`decide_compaction`, mode `context_size`): measure the context as
the larger of (a) the last assistant row's prompt tokens (`_prompt_tokens`)
and (b) an estimate of the messages since the latest compaction (characters
of their JSON content / 4). (b) matters on the first turn after a main chat
switches to native history: its stored rows were tiny window prompts, but its
full history can be far over the window.

Folding (`compact_session`): walk back from the newest message summing
estimated tokens until `keep_tokens` is reached; that index is the target
cut. Keep the current rule that moves the cut back to a turn start (a user
message with text), so tool calls are never split from their results. If the
cut lands at 0 (one huge turn), fold nothing, as today.

Mode mapping, so stored sessions and bot.yaml keep working:

- `rolling` (and its alias `stream`) is read as `context_size`.
  `normalize_compaction_mode` maps both. The `ROLLING` choice stays in
  `CompactionMode` so stored values remain valid; `DEFAULT_CONFIG["rolling"]`
  goes away.
- `time` and `none` stay as they are.
- `BotDefinition.default_compaction_mode` defaults to `context_size`.

Main and named chats stop being window chats:

- `Bot.chat_session` creates them with `compaction_mode=context_size` and
  `compaction_config={}`.
- Data migration (in `src/django_ergo/migrations/`): for sessions with a
  `bot_name` and `compaction_config.native_history == "turn"`, drop
  `native_history` and set `compaction_mode` to `context_size`; set every
  `rolling`/`stream` session to `context_size` and drop `keep_recent`,
  `batch`, `min_tokens` from its config. Reverse is a no-op.
- `Bot.context_builder` no longer adds the "Recent messages in this
  conversation" source (`is_window` is False for these sessions now).
- `reply_spec`: pre-seeds go in once per session **and again on the first
  turn after each compaction** (the seeded tool exchange gets folded into the
  summary otherwise). In `run_structured_call`: seed when
  `pre_seed_each_turn`, or when no call of this kind with `seeded=True` has
  `first_sequence` greater than the latest compaction's `upto_sequence`.
- The history toolkit stays on main chats, reading every session the user has
  with the bot (unchanged `history_sources`), for reaching past a summary.
- `WindowChat`, `apply_native_window` and `native_history` stay in the
  library for non-bot callers. bot.yaml keys `recent`, `granularity` and
  `budget_tokens` still parse; `recent` and `granularity` no longer do
  anything for bots (say so in docs), and `budget_tokens` still sizes the
  per-turn context block.

## 2. Summaries see tool output, in chunks

- Add a `digest` detail to `ConversationRenderer`: like `skeleton`, but tool
  arguments are shown up to 300 characters, and each tool result's text up to
  1,500 characters (with `… [N more chars]`). Errors are marked as now.
- `compact_session` renders folded messages with `digest`.
- When the rendered transcript is over 100,000 estimated tokens (or 40% of
  the engine's window, whichever is smaller), split the folded messages into
  chunks under that size (on message boundaries) and summarize them in order,
  each call taking the previous chunk's summary as "Previous summary". Only
  the final summary is stored, as one `ConversationCompaction` covering the
  whole fold (its `structured_call` is the last chunk's call; the other calls
  are kept as ordinary `compaction` structured calls).
- Update `SUMMARY_SYSTEM` to say tool results are shown truncated and that
  findings from them (paths, ids, numbers, errors, conclusions) should be
  kept.

## 3. Tool results trimmed by a token budget

Replace the count with a budget, keeping the same stub format and rules
(errors and results of 500 chars or less are never stubbed; ids untouched;
images stay).

- New setting `DJANGO_ERGO["TOOL_RESULTS_TOKENS"]`, default `None`, meaning
  20% of the engine's `context_window` (40k on 200k, 200k on 1M).
- `trim_tool_results(messages, *, budget_tokens, protect_latest=3)`: walk
  large results newest first, summing estimated tokens; the newest
  `protect_latest` are always kept in full; once the running total passes
  the budget, every older large result is stubbed.
- `TOOL_RESULTS_IN_CONTEXT` and bot.yaml `tool_results_in_context` still
  work for anyone who set them: when set (not `None`) they apply the old
  count rule instead. The default for `TOOL_RESULTS_IN_CONTEXT` becomes
  `None`. New bot.yaml key `tool_results_tokens` overrides the budget.
- `trim_tool_results` returns how many results it stubbed (or the engine
  records it), for the Context view.

## 4. Per-turn context goes with the turn, not in the system prompt

Keep the system prompt identical from turn to turn so the history prefix
stays cached.

- `ClaudeAPIEngine` (and so the CLI engine) and `OpenAIAPIEngine`: stop
  joining `ephemeral_context` into the system prompt. In
  `reconstruct_messages`, after trimming, insert the context as a text block
  at the start of the user message that starts the current turn
  (`native_turn_start(messages)`; if None, the first user message). Wrap it
  as `<turn-context>…</turn-context>`. This happens only in the request; the
  stored row is unchanged.
- OpenAI: the same, prepended to that user message's content.
- Round the "Current time" context to the minute as now (it is no longer in
  the cached prefix, so that's fine).

## 5. Fix the empty-history bug

In `MessageContextSource._fit`: when nothing has been selected yet and the
newest visible message alone doesn't fit, include it truncated to the budget
(`… [truncated, read it with ergo_chat_history_read]`), then stop. Test: ten
short messages plus a 20,000-character newest message still renders a
section.

## 6. Record each turn's context, and show it in Ergonaut

Record (library):

- On each in-session `StructuredCall`, store `metadata["context"]` from the
  first model request of the turn:
  - `sections`: for each context-block section, `{title, tokens, complete,
    text}` (text capped at 20,000 chars each).
  - `compaction`: the compaction in effect, `{id, upto_sequence,
    message_count, reason, created_at}` or null.
  - `native_messages`: how many messages were sent, and `first_sequence` of
    the oldest one sent.
  - `stubbed_results`: count stubbed by trimming; `images_dropped` if easy.
  - `estimated_tokens`, `context_window`, `compact_at_tokens`.
  - Prompt tokens come from the call's existing usage fields.
- The engine can expose what it built as `engine.last_request_info` (a dict
  set in `reconstruct_messages`); `_SessionTranscript.respond` copies it into
  the call's metadata once per call.
- `BuiltContext` keeps its sections; `_extra_system` returns them along with
  the text so they can be recorded.

Show (Ergonaut, `ergonaut/`):

- API (`ergonaut/api/bots.py`): a turn's payload gets a `context` summary
  (section titles and tokens, totals, compaction in effect, stub count), and
  a new endpoint returns the full recorded context for one call
  (`GET .../calls/{id}/context`), with the summary text of the compaction in
  effect.
- Also list compactions for a session in the messages payload: `{id,
  upto_sequence, from_sequence, message_count, reason, created_at}`.
- Frontend (`ergonaut/frontend`): each assistant reply gets a small
  "Context" button (next to the existing cost or details affordance). It
  opens a panel showing: prompt tokens vs window, the sections with token
  counts (each expandable to its text), the compaction in effect (expandable
  to the summary), how many messages were sent natively, and how many tool
  results were stubbed. In the message list, each compaction shows as a
  divider after its `upto_sequence` message: "Earlier messages summarized
  (N messages)", clickable to read the summary.
- Follow the existing frontend patterns and both light and dark themes;
  prettier and the frontend tests must pass.

## Tests

- `tests/test_conversation_compaction.py`: token-based decision (both
  measures), keep by tokens with the turn-start rule, rolling/stream read as
  context_size, chunked summarization, digest rendering includes truncated
  tool output.
- `tests/test_conversation_tool_results.py`: budget rule, `protect_latest`,
  old count setting still honored.
- `tests/test_conversation_context.py`: the empty-history fix.
- Engine tests: system prompt no longer contains the context; it is the
  first block of the turn-start user message; stored rows unchanged.
- `tests/test_bots.py`: main chats are created as `context_size` with native
  history; pre-seeds return after a compaction; `metadata["context"]` is
  recorded on a turn.
- Migration test in the style of `tests/test_compaction_mode_migration.py`.
- Ergonaut: API tests for the context endpoint and compaction list.

## Docs

Per CLAUDE.md, in the same PR: `docs/bots.md` (sessions/compaction keys,
`tool_results_tokens`, `context_window` in providers.yaml, what `recent` and
`granularity` now do), the compaction and context guide in `docs/`, the
module docstrings of `compaction.py`, `tool_results.py`, `window.py`,
`bots/runtime.py`, and `TODO.md`.

## Rollout

octo auto-upgrades from main within about 15 minutes of a merge, and the
data migration changes every live main chat. The first turn of each main
chat after the upgrade will compact its long history in chunks (several
structured calls). That's expected; mention it in the PR.
