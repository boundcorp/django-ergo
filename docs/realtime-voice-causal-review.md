# Realtime voice delegation: offline causal review

## Conclusion and authorization

Two paid approvals are consumed. **No third inference, room join, replay or new room link is authorized.** This review made no provider call or room join, changed no credential or isolated SDK installation, and did not modify either attempt's database, queued messages or markers. Ergonaut remains disabled, draft and not ready to merge or deploy.

The exact reason GPT-Live did not produce a durable delegation in attempt two is **not recoverable from the retained protocol evidence**. Zero delegation rows is not proof that no wire event arrived. Concrete client-contract defects were found and fixed offline: treating an optional open speech segment as the entire task, omitting the delegation ID from final commentary, and dropping exceptions from asynchronous event callbacks. Missing tool registration is **not** the defect: client delegation deliberately has no tool channel.

## Attempt evidence: facts, not a reconstructed wire trace

Artifacts are local and sensitive; do not publish their raw contents.

| Evidence | Observed fact |
| --- | --- |
| First root `/tmp/ergo-voice-isolated-e1cy2fyd/probe-report.md:11-15`; `bridge-probe.log:45` | Assistant audio arrived 1.677 s after synthetic speech ended. A delegation/message persisted at 23:54:21.152 UTC, but post-commit dispatch raised the missing Redis transport exception. The asynchronous task exception was unconsumed. The old message remains queued and was not replayed. |
| Second root `/tmp/ergo-voice-isolated-dy_xdck4/redacted-evidence.log:2-16` | Joined 00:16:17.999 UTC; speech 00:16:21.654–00:16:29.010; first non-silent assistant frame 00:16:30.739; assistant-turn log 00:16:39.413; disconnect 00:17:17.018. |
| Second DB, read-only transaction inspection | Final user transcript fragments persisted at 00:16:24.210 (`Please`), 00:16:27.806 (`delegate this thread-only task to the background assistant. Check our`), and 00:16:30.958 (`conversation history and tell me what I asked you to do`). |
| Second DB, same read-only inspection | Assistant at 00:16:39.424: `I checked. You asked me to delegate a thread-only task to the background assistant, and check our conversation history to tell you what you asked me to do.` Session state was `{}` and delegation count was zero. This is a recap, not verified history-tool work. |
| `/tmp/voice-second-attempt-report.md:11-15,31-37` | Actual isolated Celery task publication/consumption was checked before the attempt; the fresh worker received no delivery task, and final inspection found no messages, structured calls or result events. Billing APIs returned 403; spend remains unknown. |

Both raw bridge logs were inspected internally through filtering/redaction, without exposing credentials or service identifiers. The second log contains no retained complete raw protocol event stream; the old bridge did not subscribe to `openai_server_event_received`. Consequently it cannot distinguish upstream non-emission, a target/ID mismatch, an event-dispatch exception, registration timing, or a callback failing before persistence. The first run proves the callback could receive a client delegation; it does not prove the second did.

## Exact installed LiveKit 1.8.11 contract

Source root: `/tmp/ergo-voice-bridge/lib/python3.12/site-packages/livekit/`. The public OpenAI plugin's `realtime/gpt_live_model.py:1-35` wraps the shared implementation below. OpenAI SDK is 3.49.0; the isolated installation was not changed.

| Source | Contract |
| --- | --- |
| `agents/llm/_realtime/gpt_live.py:200-205,227-230,518-520` | `delegation="client"` serializes `delegation: {"type":"client"}`. There are no backend tools in this mode. `responses_options` belongs only to Responses delegation. |
| `agents/llm/_realtime/gpt_live.py:1254-1265` | Nonempty framework tools in client mode raise `RealtimeError`; do not add a fake `delegate` function schema. |
| `agents/llm/_realtime/gpt_live_types.py:253-262` | Wire event: `{"type":"session.delegation.created","delegation":{"id":"opaque-id","target":"client"}}`. No task, arguments or request text exists in this event schema. |
| `agents/llm/_realtime/gpt_live.py:817-833,950-961` | Parser emits `delegation_created` with `GPTLiveDelegation(id, pending_transcript)` only for a nonempty ID and `target == "client"`. Missing ID warns; other targets do not emit that application event. |
| `agents/llm/_realtime/gpt_live.py:121-131,880-948,1159-1164` | `pending_transcript` accumulates the still-open user speech segment; it is neither a generated task nor full history. It becomes empty after `_end_speech("user")`, including the SDK's silence boundary. The request must be interpreted with the agent conversation context. |
| `agents/voice/agent.py:169-179,698-712` | `Agent.chat_ctx` supplies conversation history. `Agent.duplex_session` returns the actual provider session behind the realtime wrapper. The provider session itself has no public `chat_ctx` property. |
| `agents/llm/duplex_adapter.py:340-347,560-563` | Generic adapter forwarding is not the delegation channel; subscribing directly on `Agent.duplex_session` is correct. |
| `agents/llm/_realtime/gpt_live.py:1194-1222` | `append_instructions` = standing rule; `append_thinking` = silent context; `append_commentary(text, delegation_id=id)` = speakable answer to this client delegation. Repeated commentary with the same ID continues that work. Queuing an append proves neither transmission nor playback. |
| `agents/llm/_realtime/gpt_live.py:768-779,803-811` | Raw server hook precedes dispatch; raw client hook precedes sending. Previous logs did not capture these comprehensively. |

### Model instructions and injected context

At revision `8e5c318`, `realtime_voice_bridge.py:69-78` constrained status claims but did not describe when/how to use client delegation or explicitly prohibit substituting an immediate recap for a stored-history lookup. **Hypothesis:** this instruction gap and the self-referential spoken request contributed to the recap. The SDK has no mandatory magic prompt or forced client-delegation switch; source inspection cannot prove the hosted model's decision.

Startup restored saved state and finalized transcript; only answered linked messages were labeled verified background results. Per-user-turn `append_thinking` contained server UTC and activity elapsed times, not tool results. In attempt two, startup was empty and state remained `{}`. There is **no evidence that actual history results were injected as already fulfilled**. Silent context can still influence model semantics, which cannot be established offline. The fix explicitly labels saved state/time as non-result context and excludes saved-state/result wrappers from request extraction.

## Minimal fix and invariant

- `ergonaut/ergonaut/realtime_voice_bridge.py:44-100`: clarify client delegation instructions; identify saved context separately from real user messages and verified outcomes. Leave voice tools empty and keep `max_retry=0`, one provider connection and existing runtime/one-shot caps.
- `:102-139`: consume async callback exceptions; log only exception class, persist a library-authored unconfirmed-action status and attempt truthful commentary. Add payload-free listener-ready, wire-delegation ID/target booleans and application-signal logs. Persist receipt of the application signal before queue work. No retry or fallback dispatch.
- `:141-154`: generated assistant speech without a durable answer gets a separate persisted `background_status` warning, rather than becoming lookup evidence.
- `:174-223`: use bounded `Agent.chat_ctx` user messages plus the pending open segment, not the open segment alone. Empty pending text after finalization is valid; truly empty user context asks for clarification without queueing. Existing server-owned budget and one-request limit remain authoritative.
- `:225-238`: final outbox commentary carries the original delegation ID; the existing durable claim still allows at most one attempt and is not playback proof.
- `ergonaut/frontend/src/components/VoiceSessionCard.tsx:18-74`: separate reported/unverified Stage and generated speech from durable work; explicitly show no submitted request/no verified history result, queued/may-not-have-started, failure, and answered durable results. Non-answered/error-bearing text cannot render as a result.

A signal is not a task execution; a queued message is not running; assistant promises are not successful work; only the linked durable outcome is result evidence. An answered background result still does not independently prove which tool the model used: structured-call/tool records are needed for the live history-lookup acceptance check.

## Exercised offline verification

| Command/scenario | Actual result |
| --- | --- |
| `cd ergonaut && PYTHONPATH=.:../src /tmp/ergo-voice-test/bin/pytest ergonaut/api/tests/test_realtime_voice_bridge.py ergonaut/api/tests/test_realtime_voice_budget.py ergonaut/api/tests/test_realtime_voice.py ergonaut/api/tests/test_realtime_voice_outbox.py ergonaut/apps/bots/tests/test_turn_budget.py -q --disable-warnings` | 29 passed. Seven new cases use the installed SDK parser, event emitter, transcript accumulation/finalization, client configuration and actual commentary serialization. They cover open/finalized speech, no event/wrong target/missing ID, callback exception, saved-state-only signal, real queue/turn runner and same-session durable result. No model or network call. |
| `ERGO_TEST_ISOLATED=1 /tmp/ergo-voice-test/bin/pytest tests/test_conversation_budget.py -q --disable-warnings` | 34 passed; server budget/authored limits/no-retry behavior unchanged. |
| `cd ergonaut/frontend && bun test` | 240 passed; existing React act warnings. |
| `cd ergonaut/frontend && bun run build` | Passed; existing large-chunk warning. |
| `cd ergonaut && ruff check ergonaut/realtime_voice_bridge.py ergonaut/api/tests/test_realtime_voice_bridge.py` | Passed after formatting these two files. |
| Standalone disposable runtime smoke, not a pytest | Real `AgentSession.start(agent=assistant, record=False, session_host=False)` without a room, actual framework transcription projection and duplex accessor/on-enter wiring. Only the provider transport task was parked; actual SDK event parsing and bridge ran. A finalized user request (empty pending segment) produced `queued` → scripted bounded two-turn history-source call/reply → `answered` → durable same-session result `announcement_claimed` → commentary with original delegation ID. Both scripted provider options had `max_retries=0`; no socket or paid provider was used. |
| Chromium against the actual component on a local Vite server | Visually inspected zero-delegation assistant claim, queued request, durable answer, and expanded transcript's unverified-work label. No microphone, room connection or authenticated hosted journey. Screenshot: `/tmp/omp-sshots-15ca992081dfbf63.webp` (closed transcript); second capture showed expanded claim. |

The permanent tests bypass only SDK network-task construction and the framework property boundary; the separate real-AgentSession smoke exercised those framework wiring boundaries without a hosted transport. The initial smoke harness parked shutdown indefinitely and was stopped; its corrected parked task observed `_closing`, and the completed bounded run produced the result above. The throwaway script was removed after proof.

`envcheck` succeeded during this review: both cluster readyz checks and the documented SSH routes to macaroni, rigel, vega and enterprise were reachable. No infrastructure unavailability claim was needed. No shared restart, config/secret change, production DB mutation, migration, merge or deployment occurred.

## Next live acceptance script — requires a NEW approval

This is an acceptance procedure, **not authorization or a runnable launch command**. Do not source either historical environment or reuse a consumed marker.

1. Obtain explicit approval covering a new single paid voice connection, at most one bounded delegation, the shared project/remaining allowance and reconciled or explicitly accepted unknown billing. Confirm one-shot/runtime limits, no retries and a fresh isolated DB/broker/worker/account/bot/chat/session/dispatch/prefix/markers. Keep both prior roots untouched. Existing protected credentials and SDK3 installation stay unchanged.
2. Prepare deterministic, owner-scoped stored history with a non-sensitive unique fact, for example `offline acceptance pantry code: violet-ladle`, in an earlier chat accessible to the allowed history tools. Keep that answer OUT of voice startup state/transcript, speech and injected time context. Verify the fresh voice session has no delegation or result and that the actual Celery task can be published/consumed harmlessly before a worker starts.
3. Start only the separately approved isolated worker/bridge/client. Before publishing speech, require `voice delegation_listener_ready`; retain payload-free `voice delegation_wire_received` and `voice delegation_signal_received` records with UTC timestamps. Keep `record=False`; do not change project observability.
4. Speak once, naturally and unambiguously: **“Ask the background assistant to search my earlier conversation history for my pantry code and report the code. Do not answer from this conversation or say you checked before its result arrives.”** Do not retry or rephrase within the approval if delegation is absent.
5. Correlate: wire event with valid ID/client target → application signal → durable `delegation_signal` → exactly one delegation/message in this voice chat → real Celery receipt → structured call using the pinned bounded model → actual allowed history search/read tool calls → answer containing the seeded fact → same-session durable result → commentary using the SAME delegation ID. Record timestamps/latency and budget metadata; speech promises alone fail acceptance.
6. If no valid event arrives, no request must be auto-dispatched. The UI/history must show no submitted request/no verified result, and any generated “I checked” claim is a failed semantic acceptance even with the unverified label. Preserve evidence, stop at the cap and report upstream/model behavior as unresolved; do not spend another attempt automatically.
7. Stop only owned isolated processes; retain consumed markers and any queued messages, perform approved read-only room absence and billing checks, and report actual usage or its absence. Browser hearing/playback, interruption/reconnect and migration/retention remain separate gates; do not infer them from synthetic track receipt.

**Open behavior:** the hosted model's decision to emit delegation and its spoken truthfulness cannot be guaranteed by offline tests or instructions. This patch fixes the proven application-contract/status defects and adds evidence boundaries; it does not claim the second attempt's unknown wire/model cause is solved or authorize further spending.
