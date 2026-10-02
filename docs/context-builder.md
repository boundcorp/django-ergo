# Context builder and window chats

## ContextBuilder

`django_ergo.conversation.context` assembles a context block from weighted
sources within a token budget:

```python
from django_ergo.conversation.context import (
    ContextBuilder, MessageContextSource, TextContextSource,
)
from django_ergo.conversation.history import SessionSource

builder = ContextBuilder(budget_tokens=8000)
builder.add(MessageContextSource(SessionSource(session), weight=3, min_messages=15))
builder.add(TextContextSource("Pantry", pantry_summary, weight=1))
built = builder.build()
built.text    # "<context>\n## ...\n</context>"
```

- The budget is split by weight. Budget a source leaves unused goes to the
  sources that ran out of room. Token counts are estimated at four
  characters per token.
- `MessageContextSource` takes any `MessageSource`, whether a DB session or a
  Claude or Codex transcript. It tries the most detailed granularity up to
  `max_granularity` (default `reasoning`) and steps down to `conversation`
  until at least `min_messages` of the latest messages fit. Then it adds as
  many more as fit, up to `max_messages`. Pass `granularity=` to fix the
  level, or `before_line=` to leave out recent lines. With
  `skip_native_turn=True` it leaves out the messages a window chat
  already sends natively (the current turn, or the turn a new message
  continues), so nothing appears twice; pass `incoming=False` when the
  builder is for resuming a stored turn (after an approval) rather than a
  new message.
- Messages are rendered with line numbers and timestamps. A section that
  couldn't fit everything ends with the call that reads further back, for
  example `ergo_chat_history_read source_id=session:... end_line=210`, plus a hint for
  getting more detail. Both are calls on `MessageHistoryToolkit`.
- `TextContextSource` takes text or a callable, for things like KB results,
  a profile, or live state, and truncates it to fit. Write your own source by
  subclassing `ContextSource` and implementing `render(budget_tokens)`.

Pass `context_builder=builder` to `run_conversation_turn` to send the built
text as extra system context on every model call in that turn. It is never
stored.

## Window chats

A window chat is a long-running chat whose context stays the same size
however long it runs: each turn sees a window of recent messages, never the
whole transcript. Bot main chats and named chats work this way. In code it
is `StreamChat` (window chats were called stream chats before):

```python
from django_ergo.conversation.stream import StreamChat

chat = await StreamChat.create(user=user, system_prompt="You are the kitchen bot.",
                               recent=15, context_sources=[pantry_source],
                               history_sources=sources_from_paths(default_cli_paths()))
async for event in chat.send("What fridge did we pick?"):
    ...
chat = await StreamChat.resume(session)   # later
```

Each turn the model gets:

1. the system prompt,
2. a context block with the chat's latest `recent` messages (conversation
   granularity by default) and any `context_sources`,
3. only the current turn as native messages: the new message plus that
   turn's tool calls and results. This is the session setting
   `compaction_config["native_history"] = "turn"`. The context block leaves
   these out, so the new message is never in both places.
4. `MessageHistoryToolkit` over its own history plus any `ergo_chat_history_sources`.
   It uses this to read further back or to see thinking and tool details for
   messages in the window.
