# Realtime voice delegation: offline causal review

## Third authorized live acceptance — 2026-11-12 UTC

Lee approved exactly one third attempt, with the same one-connection, 90-second and bounded-delegation limits. It produced **one real, bounded, same-session history lookup and durable result**; the synthetic participant received assistant audio, not browser playback or evidence of human hearing. No fourth attempt, retry, shared deployment, project-privacy change or production mutation was made. The prototype remains disabled and not ready to merge or deploy.

Preflight used a fresh isolated PostgreSQL database, Redis broker, solo Celery worker, user, bot, voice chat, dispatch, room prefix and one-shot markers. The actual Django delivery task was published, consumed and acknowledged on the empty isolated broker before starting the worker; afterward its process environment, routes, empty queue and exact `gpt-6-luna` 4-turn / 40,000-input / 1,500-output / 45-second budget were checked. LiveKit Agents 1.8.11 and OpenAI 3.49.0 were isolated; both model metadata endpoints returned 200 with the exact IDs. The same protected SOPS LiveKit configuration and Octo runtime OpenAI key were used without printing credentials; the prior shell key's 401 remains the reason for the runtime key.

A unique non-sensitive pantry code was stored in a separate owner-and-bot-scoped conversation before the voice connection. The actual allowed `ergo_chat_history_search` returned that fact and excluded another user's test conversation; the fresh voice snapshot contained no fact, events or delegation. The fact was not injected into startup voice context, synthetic speech or time context. `AgentSession.start(..., record=False)` was checked in the actual bridge; hosted job metadata still advertised recording enabled, and hosted storage was not independently audited. Project observability was not changed.

| UTC event | Observed evidence |
| --- | --- |
| 01:20:02.864 | Synthetic participant connected; microphone track initially carried silence. |
| 01:20:03.441 | Delegation listener ready; the supervisor gated speech on this log and audio subscription. |
| 01:20:04.607–01:20:15.613 | One synthetic spoken request, with no rephrase or retry. |
| 01:20:10.583 | First non-silent remote frame, 5.977 s after speech began and 5.030 s **before it ended**; 6,001 frames in total. This includes an early acknowledgement, not necessarily the answer. |
| 01:20:11.893–11.895 | Valid-ID/client-target wire delegation and application signal, with matching payload-free correlation. |
| 01:20:11.900–11.918 | Durable signal and exactly one same-chat delegation/message. |
| 01:20:12.117–16.308 | Isolated Celery received and completed the real delivery task in 4.190 s. |
| 01:20:14.751–14.785 | Structured `ergo_chat_history_search` called with `pantry code`; its result included the seeded earlier owner-scoped source and fact. |
| 01:20:16.667–16.672 | Same-session durable answered outbox event and queued commentary with the **same delegation correlation**, 4.773 s and 4.779 s after the wire event. The outbox claim and SDK queued hook do not prove transmission or playback. |
| 01:20:28.997 | Persisted generated assistant transcript reported the matching code; delivery remains unknown. |
| 01:21:03.619 | Synthetic participant disconnected; supervisor stopped owned bridge and client at 73.444 s. The isolated worker was stopped separately. |

The completed structured call reported `gpt-6-luna`, two turns, 68 output tokens, six uncached input tokens and 1,710 cache-read input tokens in its recorded fields; the budget ledger separately reported 3,710 input tokens, 68 output tokens and 3.993 seconds. Its recorded $0.00030095 is an internal estimate, not a provider bill. OpenAI organization usage and costs endpoints returned HTTP 403 before and after the run. Actual billed voice duration, exact cost and LiveKit charges remain unknown; $0.15 plus tax was a conditional planning reserve, not a financial cap, and the shared allowance was unknown and accepted.

**Semantic/context limits remain:** delegation arrived while the user was still speaking. The submitted context contained the complete first sentence through “report the code” but not the later sentence prohibiting premature claims; persisted final fragments also did not retain that sentence in full. The model first generated “Sure”; after the durable answer existed, another generated transcript said the request “may not have started yet” and “I don't have a result yet” before continuing “I found it.” This stale, self-contradictory status is not evidence of a false pre-result “checked” claim in this run, but it is a real sequencing/truthfulness defect. Neither the cause of the missing transcript words nor the model's choice to delegate mid-utterance is established. No claim is made that the final spoken code was heard.

The third root is `/tmp/ergo-voice-isolated-ek_ix1as`; logs contain identifiers and must not be published raw. Payload-free correlation metadata was added to the wire/signal and SDK commentary-queued logs before launch. Both previous roots and their markers/messages remain untouched; all three launch/call marker pairs remain, and the third delivery queue is empty. An immediate read-only room list briefly showed one remaining participant; a subsequent read-only list showed zero matching rooms. The 29 targeted offline backend cases passed after the metadata change, and Ruff passed. No additional paid attempt is authorized by this result.

## Offline correction after the third attempt

The installed SDK's `gpt_live.py:880-891` closes a user segment on a timestamp gap, and `:1159-1164` closes it after audio has been pushed without a new transcript fragment; `_end_speech` emits a final transcript and `input_speech_stopped`, including on SDK shutdown (`:660-661`). Those are SDK segment boundaries, not proof that the speaker finished the entire instruction. The third trace also shows that final fragments can omit words. No silence timer, SDK `is_final`, or model delegation signal is therefore accepted as a complete request.

The bridge records the first client signal and ordered transcript drafts in the voice session. The owner must finish speaking, enter the **complete** request and constraints, and explicitly confirm it in the voice panel within 60 seconds. The owner's text, not a supposedly complete SDK transcript, becomes the one budgeted request. The server serializes confirmation and cancellation against session end; expiry, cancellation, bridge shutdown and session end do not dispatch. Repeated signals do not create a second request; no paid retry or second provider connection is added. The draft is assistance only, not guaranteed transcription, and no automatic speech-end dispatch is claimed.

For a durable outcome the bridge now queues authoritative silent context before same-ID commentary and no longer queues the old submitted/may-not-have-started commentary or delayed no-result speech status. This prevents those server-authored stale notices; it cannot guarantee the model's generated words, transmission, playback or hearing.

Offline proof: the installed-SDK parser regression feeds early signal, a final segment, a late constraint in another final segment, explicit owner confirmation, and a duplicate signal; the single queued request contains the constraint and the scripted bounded two-turn result is injected as thinking before correlated commentary. Owner authorization, cancellation, expiry, session end and bridge-shutdown cancellation are covered separately. A local Chromium fixture displayed the actual confirmation panel, entered a complete constrained request and observed the form close after a simulated confirmation; no room or model was connected. Earlier artifact roots remain `/tmp/ergo-voice-isolated-e1cy2fyd`, `/tmp/ergo-voice-isolated-dy_xdck4`, and `/tmp/ergo-voice-isolated-ek_ix1as`; do not publish raw logs, reuse markers or replay messages.

The targeted backend run, frontend build and frontend suite are reported in [progress](realtime-voice-progress.md). This correction has not been tested against hosted audio; browser playback, interruption, reconnect, model truthfulness and provider billing remain open. No fourth inference, room join, replay, credential rotation, merge, deployment or shared restart was authorized.


## Historical offline review and earlier authorization state

## Historical conclusion and authorization after attempt two

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

## Historical fix and invariant before attempt three

These pre-third-attempt implementation details are retained as historical evidence and are superseded by the offline correction above.

- `ergonaut/ergonaut/realtime_voice_bridge.py:44-100`: clarify client delegation instructions; identify saved context separately from real user messages and verified outcomes. Leave voice tools empty and keep `max_retry=0`, one provider connection and existing runtime/one-shot caps.
- `:102-139`: consume async callback exceptions; log only exception class, persist a library-authored unconfirmed-action status and attempt truthful commentary. Add payload-free listener-ready, wire-delegation ID/target booleans and application-signal logs. Persist receipt of the application signal before queue work. No retry or fallback dispatch.
- `:141-154`: generated assistant speech without a durable answer gets a separate persisted `background_status` warning, rather than becoming lookup evidence.
- `:174-223`: use bounded `Agent.chat_ctx` user messages plus the pending open segment, not the open segment alone. Empty pending text after finalization is valid; truly empty user context asks for clarification without queueing. Existing server-owned budget and one-request limit remain authoritative.
- `:225-238`: final outbox commentary carries the original delegation ID; the existing durable claim still allows at most one attempt and is not playback proof.
- `ergonaut/frontend/src/components/VoiceSessionCard.tsx:18-74`: separate reported/unverified Stage and generated speech from durable work; explicitly show no submitted request/no verified history result, queued/may-not-have-started, failure, and answered durable results. Non-answered/error-bearing text cannot render as a result.

A signal is not a task execution; a queued message is not running; assistant promises are not successful work; only the linked durable outcome is result evidence. An answered background result still does not independently prove which tool the model used: structured-call/tool records are needed for the live history-lookup acceptance check.

## Historical offline verification before attempt three

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

1. Obtain explicit approval covering a new single paid voice connection, at most one bounded delegation, the shared project/remaining allowance and reconciled or explicitly accepted unknown billing. Confirm one-shot/runtime limits, no retries and a fresh isolated DB/broker/worker/account/bot/chat/session/dispatch/prefix/markers. Keep all three prior roots untouched. Existing protected credentials and SDK3 installation stay unchanged.
2. Prepare deterministic, owner-scoped stored history with a non-sensitive unique fact, for example `offline acceptance pantry code: violet-ladle`, in an earlier chat accessible to the allowed history tools. Keep that answer OUT of voice startup state/transcript, speech and injected time context. Verify the fresh voice session has no delegation or result and that the actual Celery task can be published/consumed harmlessly before a worker starts.
3. Start only the separately approved isolated worker/bridge/client. Before publishing speech, require `voice delegation_listener_ready`; retain payload-free `voice delegation_wire_received` and `voice delegation_signal_received` records with UTC timestamps. Keep `record=False`; do not change project observability.
4. Speak once, naturally and unambiguously: **“Ask the background assistant to search my earlier conversation history for my pantry code and report the code. Do not answer from this conversation or say you checked before its result arrives.”** Do not retry or rephrase within the approval if delegation is absent.
5. After speech ends, inspect the pending draft and explicitly enter and confirm the complete request including the late constraint. Verify that no task was sent before confirmation. Correlate: wire event with valid ID/client target → application signal → durable `delegation_signal` → owner confirmation → exactly one delegation/message in this voice chat → real Celery receipt → structured call using the pinned bounded model → actual allowed history search/read tool calls → answer containing the seeded fact → same-session durable result → silent context and commentary using the SAME delegation ID. Record timestamps/latency and budget metadata; speech promises alone fail acceptance.
6. If no valid event arrives, no request must be auto-dispatched. The UI/history must show no submitted request/no verified result, and any generated “I checked” claim is a failed semantic acceptance even with the unverified label. Preserve evidence, stop at the cap and report upstream/model behavior as unresolved; do not spend another attempt automatically.
7. Stop only owned isolated processes; retain consumed markers and any queued messages, perform approved read-only room absence and billing checks, and report actual usage or its absence. Browser hearing/playback, interruption/reconnect and migration/retention remain separate gates; do not infer them from synthetic track receipt.

**Open behavior:** the hosted model's decision to emit delegation and its spoken truthfulness cannot be guaranteed by offline tests or instructions. This patch fixes the proven application-contract/status defects and adds evidence boundaries; it does not claim the second attempt's unknown wire/model cause is solved or authorize further spending.
