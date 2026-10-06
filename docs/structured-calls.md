# Structured calls

A structured call sends a request, lets the model use tools, and ends with
a validated response. Every call is a `StructuredCall` row with a `kind`. A
call can stand alone or be one turn of a conversation session. The code is
in `django_ergo.conversation.structured`.

```python
from pydantic import BaseModel
from django_ergo.conversation.structured import (
    PreSeedCall, StructuredCallSpec, revise_structured_call, run_structured_call,
)

class Plan(BaseModel):
    title: str
    steps: list[str]

spec = StructuredCallSpec(
    kind="planner",
    system_prompt="Plan the work.",
    response_model=Plan,
    toolkits=[my_toolkit],
    pre_seeds=[PreSeedCall("get_ticket", {"id": 7}, load_ticket)],
    max_turns=10,
)

result = await run_structured_call(spec, "Plan ticket 7", user=user)
result.parsed   # Plan(...)
result.call     # StructuredCall row: kind, request, response, status, tokens

# Correct it. The revision is a new call whose parent is the first one.
fixed = await revise_structured_call(spec, result.call, "Drop step 3")

# Or make the call one turn of an existing conversation.
result = await run_structured_call(spec, "Plan it", session=session)
```

## Standalone calls

- No session is created. The tool loop runs in memory and is saved on the
  row as `transcript` (engine-native messages), along with `system_prompt`
  and `engine_type`.
- `revise_structured_call` replays the parent's transcript plus the
  correction on the same engine type, and links the new call through
  `parent` (`call.revisions` goes the other way).
- Attachment bytes are sent to the model but not stored in the transcript.
  A placeholder takes their place.

## Calls inside a session

- Pass `session=`. The call's messages go into the session like any chat
  turn, and the row records their span (`first_sequence`, `last_sequence`).
- The spec's `system_prompt` is added to the session's own system prompt for
  that turn only.
- With `response_model`, the turn ends with the response as an assistant
  text message. Later chat turns, history tools and context builders then
  see the answer, not just a tool call.
- Sessions can mix structured calls and ordinary chat turns freely. There is
  no structured-only session type. `session.structured_calls` lists a
  session's calls.
- Revising an in-session call just adds another structured turn to the
  session, with `parent` set.

## Behavior

- With `response_model`, the model gets a `submit_output` tool whose input
  schema is the Pydantic schema. Invalid input goes back as a tool error. A
  plain-text answer gets a correction asking for the tool.
- Without it, the final text is parsed by `output_parser` (`json.loads` by
  default). A parse failure goes back as a correction message.
- Tool failures and unknown tools go back to the model as error results.
  Tools that need approval are refused, because structured calls run
  unattended.
- Transient API failures (connection, timeout, 5xx) are retried with
  backoff. Nothing is written until a call succeeds, so retries never
  duplicate history. Other API errors are recorded on the row with an
  `error_category` (`auth`, `network`, `model`, `other`).
- Statuses: `completed`, `failed` (API error or `max_tokens` stop), and
  `turn_limited` (no valid output within `max_turns`).
- Rows never store engine config, so credentials are never written.
- Each model call carries only the newest
  `DJANGO_ERGO["TOOL_RESULTS_IN_CONTEXT"]` large tool results (default 3,
  "large" meaning over 500 characters) in full, and older ones too while the
  kept results total at most `DJANGO_ERGO["TOOL_RESULTS_CHARS_IN_CONTEXT"]`
  characters (default 40,000; 0 keeps only the count). Older ones are sent
  as a stub such as `[penpot_tree result, 180 lines, 9,412 chars; trimmed
  from context to save space. Note what you need; call the tool again only if
  you still need detail.]`. Errors and short results are sent as they are.
  Only the request changes: the transcript and session rows keep every
  result, and tool call ids still pair up. Images in a stubbed result stay,
  and the image window (`IMAGES_IN_CONTEXT`) decides about them as before.
  An engine's `tool_results_in_context` attribute overrides the setting;
  `None` in the setting sends every result in full. The same applies to chat
  turns (`run_conversation_turn`), since both engines apply it when they
  rebuild a session's messages.

Compaction summaries are structured calls of kind `compaction`; see
[compaction.md](compaction.md).

## Mapping from Cabal's Assistant

| Cabal (`cabal/apps/agents/assistant.py`) | Ergo |
| --- | --- |
| `AssistantSpec` | `StructuredCallSpec` |
| `ToolDef` list | `toolkits` (any `Toolkit`) |
| `PreSeedCall` | `PreSeedCall` (same fields) |
| `output_parser` + correction | `output_parser` + correction, or `response_model` |
| `Assistant` row | `StructuredCall` |
| `Assistant.turns` JSON | `StructuredCall.transcript` |
| `run_assistant` / `arun_assistant` | `run_structured_call` |
| none | `revise_structured_call`, and `session=` for calls inside a chat |

Not carried over: Cabal's company/ticket/step foreign keys (put them in
`metadata`, or add a model in the host app), `ToolCallLog`, and cost
pricing.
