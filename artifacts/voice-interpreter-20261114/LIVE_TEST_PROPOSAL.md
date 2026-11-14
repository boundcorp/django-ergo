# Bounded live-test proposal — NOT authorized or executed

## Decision and hard gate

Offline implementation approval does not permit this test. Obtain a fresh approval for the exact isolated setup, model, limits and verified cost reserve below; do not reuse old paid-attempt markers or attach to an owner session. No billing/project configuration change is part of this proposal without separate approval.

The requested **all-in spend ceiling is $2.00**, but it is **not currently enforceable or established**. Missing prerequisites are listed explicitly; therefore this proposal is not executable yet. A timer or response-created counter alone is not a hard provider spend bound.

## Maximum scope

- Two disposable logical calls, sequentially, each at most 90 provider seconds: **180 provider seconds requested maximum**, with a separate 115-second outer process timeout per attempt (**230 seconds total outer wall time**). Provider close/settlement overhang must be accounted for before claiming a billed-duration ceiling.
- Exactly one application GPT-Live WebSocket attempt per call; two total, zero reconnect retries, no sideband socket and no mode switch. `gpt-live-1` remains the voice model.
- Explicit managed backend: `gpt-5.6-luna`, default service tier, reasoning `none`, maximum 1,024 output tokens per backend response, only the narrow `ergo_voice_intent` function.
- Target maximum **16 managed backend responses total, including continuations**, not 16 user requests. The isolated supervisor must enforce a pre-agreed bound and account for already-started/autonomous Responses work; counting `response.created` after launch does not establish the bound by itself. No test begins until this prerequisite is satisfied.
- **Zero normal paid-model calls**: use the actual normal dispatcher, disposable PostgreSQL/Redis and scripted normal bot transport. Zero external action tools, real-owner tasks, production database access, physical microphones, rooms owned by others or devices. Prerecorded synthetic utterances only, in a fresh uniquely named test room/dispatch.
- Four in-flight task receipts remain backpressure, not a cumulative intentional-request quota. Test-only inference/spend limits do not become a product request cap.

## Exact cases

### Call A — intentional requests and independent admission (90 seconds maximum)

1. One clear standalone instruction. Completed function arguments must carry exactly that instruction; no mandatory confirmation.
2. Queue three more distinct intentional requests while the first remains queued: four accepted durable tasks, each in the original disposable owner/bot/thread, without falsely calling them running.
3. Complete the first scripted task, then ask a fifth distinct request on the same socket. It is accepted despite exceeding three cumulative requests; no provider connection or deadline reset.
4. Ask status of a known task and request readback of its completed result. No additional task or action approval is created; observed state, not model narration, determines queued/answered.
5. End while an accepted task is still queued. The normal task handler completes it afterward and its durable owning-thread result remains accessible.

### Call B — ambiguity, references, correction and interruption (90 seconds maximum)

1. An ambiguous “do that” without an authoritative target: useful specific clarification, zero task.
2. An explicit clarified standalone request in a new native delegation: one task, no fabricated reference to another thread.
3. A status/follow-up referring to an available bounded target: correct existing identity and context, zero duplicate work. A missing/evicted target must clarify rather than guess.
4. Mid-utterance correction/interruption before a completed function response: no obsolete partial request is admitted. Compare the full prerecorded utterance and emitted argument payload manually; a native envelope is not semantic proof.
5. Correction/cancel after submission: truthful refusal and normal-thread control guidance unless safe cancellation is independently implemented and authorized; never claim cancellation or rewrite that did not happen.
6. Continuation after a status/readback: no hidden fresh task. Save the native trace for a separate offline replay of duplicate/restarted/conflicting/orphan schedules; do not manufacture extra live backend requests to test transport replay.

Stop immediately on stale text admission, duplicate work, wrong owner/bot/thread, invented target, false approval/cancellation/running claim, second socket, unexpected model/tier/tool, isolation failure or any cost-limit uncertainty. An aborted attempt consumes its marker; no automatic retry.

## Cost evidence and missing prerequisites

Verified public prices: GPT-Live $0.05/minute billed per second; `gpt-5.6-luna` standard short-context input $0.20/M, cached input $0.02/M, output $1.20/M, cache writes $0.25/M (30m) or $0.50/M (12h). Long-context rates differ. Sources: [pricing](https://developers.openai.com/api/docs/pricing), [backend model](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [voice model](https://developers.openai.com/api/docs/models/gpt-live-1).

Conditional arithmetic only: 180 requested voice seconds = $0.15 voice-only; 16 responses at the 1,024-output-token limit = $0.0196608 output-only at short-context rates. These do not include input, cache writes, further responses, LiveKit, taxes or billing adjustments. Actual backend token usage and response count are not known from offline fixtures.

Required before approval/execution:

1. Verify the exact project's entitlement to both selected models; a public model page or SDK default is not project entitlement. No metadata API was called during this offline work.
2. Verify how the managed Live backend bounds/counts responses and input/cache tokens, including continuations and already-started work at close. Establish a real enforcement mechanism or revise the proposal; Live does not expose an application `max_input_tokens` parameter here.
3. Verify remaining LiveKit allowance and incremental fees for the actual project, plus provider billing/tier/regional/cache-write behavior. Historical invoices and allowance remain UNKNOWN.
4. Reconcile a conservative token/call/duration reserve within the proposed $2 all-in ceiling, including overhang. If this cannot be established without billing changes, stop and request the missing authorization; do not silently turn an estimate into a cap.
5. Exact-head independent review and all required CI must be green; coordinator/release-owner gates remain. A draft PR is not rollout approval.

## Rollback and success criteria

Rollback is isolated-test teardown: close the two owned provider connections, stop only the unique test bridge/worker, disconnect/delete only the new test rooms, and leave production/configuration untouched. Preserve raw identity/arguments, durable receipts, token/model usage and timing evidence before removing disposable resources. Submitted fake tasks may finish on their normal disposable queue; do not interrupt unrelated work or restart shared services.

Success requires every listed case to satisfy task-count/identity/text/approval invariants, one socket per call, no cumulative intentional-request cap, no semantic misclassification in the prerecorded cases, no false work claims, task survival after End, reconciled usage under the approved ceiling, and preserved exact trace evidence. Audio output generation is not playback or physical-hearing acceptance. This remains a proposal until separately authorized.
