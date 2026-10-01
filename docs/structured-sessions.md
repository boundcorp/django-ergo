# Structured sessions

A structured call is a conversation session whose turns each end in a
validated output. It lives in `django_ergo.conversation.structured`.

```python
from pydantic import BaseModel
from django_ergo.conversation.structured import (
    PreSeedCall, StructuredCallSpec, StructuredSession, run_structured_call,
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

result = await run_structured_call(spec, user=user, message="Plan ticket 7")
result.parsed          # Plan(...)
result.record          # StructuredOutput row: status, output, tokens, error

# A follow-up corrects the output in the same session.
again = await StructuredSession(spec).send(result.session, "Drop step 3")
```

## How it works

- The session has `mode="structured"`, `kind` set from the spec, and
  `system_prompt` set from the spec. The system prompt overrides
  `workflow.instructions` for both engines.
- Each user message creates one `StructuredOutput` row (`sequence` 0, 1, ...)
  recording the request, status, parsed output, error, turns used, token
  totals, and the span of engine messages it wrote (`first_sequence`,
  `last_sequence`).
- With `response_model`, the model gets a `submit_output` tool whose input
  schema is the Pydantic schema. Invalid input goes back as a tool error. A
  plain-text answer gets a correction asking for the tool.
- Without it, the final text is parsed by `output_parser` (`json.loads` by
  default). A parse failure goes back as a correction message.
- Tool failures and unknown tools go back to the model as error results.
  Tools that need approval are refused, because structured calls run
  unattended.
- Transient API failures (connection, timeout, 5xx) are retried with
  backoff. The engine persists nothing until a call succeeds, so retries
  never duplicate history. Other API errors are recorded on the row with an
  `error_category` (`auth`, `network`, `model`, `other`).
- Statuses: `completed`, `failed` (API error or `max_tokens` stop), and
  `turn_limited` (no valid output within `max_turns`).
- Credentials in the engine config (`api_key`, `*_secret`, `password`) are
  never written to session metadata.

## Mapping from Cabal's Assistant

| Cabal (`cabal/apps/agents/assistant.py`) | Ergo |
| --- | --- |
| `AssistantSpec` | `StructuredCallSpec` |
| `ToolDef` list | `toolkits` (any `Toolkit`) |
| `PreSeedCall` | `PreSeedCall` (same fields) |
| `output_parser` + correction | `output_parser` + correction, or `response_model` |
| `Assistant` row | `ConversationSession` + `StructuredOutput` |
| `Assistant.turns` JSON | engine-native message rows on the session |
| `run_assistant` / `arun_assistant` | `run_structured_call` |
| none (one shot only) | `StructuredSession.send` for follow-ups |

Not carried over: Cabal's company/ticket/step foreign keys (put them in
`metadata`, or add a model in the host app), `ToolCallLog`, cost pricing, and
`extra_user_content` image blocks. Ergo engines only take text messages so
far.
