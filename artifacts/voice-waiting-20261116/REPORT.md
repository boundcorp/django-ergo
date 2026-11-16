# November 16 owner call: repetitive background-status speech

## Finding and targeted correction

**This was routine progress commentary, not an interpreter/tool loop or another premature hangup.** The latest retained call ran for **629.139 seconds** and ended explicitly. Five distinct root/linked-message queued/delivered transitions each requested unlabelled progress speech; a linked task could say queued after its root was delivered, and pending beside the root's handoff result. The repeated wording concealed their different durable identities.

The voice-only correction keeps those transitions durable and supplies their machine task identity, root request, transition, status and observation flag as **silent context**, once per locally observed transition. It does not claim or request an announcement for queued/delivered. Normal approval waits and terminal outcomes keep their existing single durable announcement attempt. No task dispatch, approval, ACL, model routing, cancellation, retry, cap, connection, lifetime, schema, generic lifecycle ownership or deployment behavior changes.

This removes the bridge's explicit routine-status speech requests. It cannot guarantee the model never independently mentions pending work, and it does not suppress the pinned SDK's mandatory native tool-result continuation.

## Source and deployed runtime

- Read `/home/dev/ENVIRONMENT.md` first; `envcheck` succeeded on the documented routes.
- Current remote main and served web revision: `e176c09e34d6a724731945f8db851015b827d136`.
- Application pods use immutable digest `sha256:0aa398c8e202429cc5ead2a69c0edcb94967850b66f1f3d8645e0b678ae1b369` (November 16 rollout, not the older `71ce6be…` image).
- No changes between diagnostics merge `2c2ece0` and current main in the voice bridge/interpreter/API/outbox files. Served web file hashes match the prior diagnostic baseline. Source includes PR #335's native same-connection interpreter, unlimited intentional requests with four-in-flight backpressure, and no fixed application duration.
- Fresh implementation worktree/branch: `/home/dev/orca/workspaces/django-ergo/voice-waiting-review-20261116`, `leewardbound/voice-waiting-review-20261116`, based on fresh `origin/main`. Original dirty `voice-premature-hangup` worktree and artifacts were preserved.

## Correct call identity and end cause

- Logical call: `dc31ccf0-e5f0-4cf6-be51-e77385ae29ff`.
- Bridge claim: `6a316fa7649d46e49ca4494c19765032`.
- Created `2026-11-16T15:31:05.430Z`; first bridge `15:31:07.566Z`; ended `15:41:34.569Z`; created-to-ended **629.139 seconds**.
- Bridge log summary: wall **626.703 seconds**, provider-reported usage **634.0 seconds**. These measure different boundaries; provider usage is not an invoice or exact wall duration.
- First retained closing boundary: application `session_ended`, `reason=explicit_end`, observed **15:41:34.563279Z**. Bridge exit followed at `.827432`; outbound provider close `.836016`; owner disconnect `.863416`; provider `close_requested` and SDK `user_initiated` were teardown at **15:41:35.604872Z** and `.611890Z`.
- This is the actual newest retained call before the owner's 16:15 report, not November 15's `77f4c16f` / `84e13349…` call. The owner's >90-second acceptance applies to this call. November 15's initiating cause remains unknown.

## Correlated timeline (UTC)

Only short relevant speech excerpts are included; no raw transcript or task/result body is published.

| Time | Durable/native observation | Speech/delivery evidence |
| --- | --- | --- |
| 15:31:12.062 | Provider session started. | Browser had subscribed to assistant audio; physical hearing is not inferred. |
| 15:31:15.410 | First native Responses delegation received. | Initial user intention was an actual current-work status lookup, not a status probe for an already accepted voice task. |
| 15:31:17.044 | Root message `964a6696…` accepted in the exact launch thread. | Normal bot execution, not interpreter action tools. |
| 15:31:18.062 | Root queued outbox transition claimed. | Commentary queued `.076`; speech begins “Task queued, but it may not have started.” |
| 15:31:36.117 | Normal task StructuredCall starts. | Root delivered transition claimed at `.881`, commentary queued `.887`; “Task delivered; no final result yet.” |
| 15:32:51.290 | First root answered. | Legitimate elapsed acceptance-to-answer **94.246 seconds**, including approximately 19 seconds before call start. |
| 15:32:52.061 | First result persisted and announcement claimed. | Commentary queued `.066`; finalized assistant text at `.860` reflects the result. |
| 15:40:24.933 | Second native Responses delegation received. | Distinct intentional read-only check; explicit no-change-without-approval constraint retained. |
| 15:40:26.682 | Root message `da178d5d…` accepted; StructuredCall starts `.912`. | Root delivered transition claimed **15:40:27.397**, commentary queued `.402`. |
| 15:40:38.051 | Normal root sends one linked task `c408a7c1…` to the existing thread. | Child queued transition claimed `.905`, commentary queued `.912`; generic queued speech after root delivered. |
| 15:40:44.756 | Second root answered with handoff receipt, not downstream investigation completion. | Root result claimed **15:40:45.258**, commentary queued `.263`; speech combines handoff with “Task delivered; no final result yet.” |
| 15:40:47.353 | Linked task delivered transition claimed. | Commentary queued `.362`; this pending state belongs to the child, not a regression of the answered root. |
| 15:41:34.563 | Explicit application End begins. | No provider-first fault evidenced. |
| 15:41:34.783 | Linked task answers **214 ms after logical End**. | Accepted work survives End. |
| 15:41:35.707 | Linked terminal result persists via owner polling. | Delivery remains `unknown`; no result announcement claim. Not lost outbox data, and not proof it was heard. |

Retained count: **39 voice events, 42 native ledger rows, two accepted root delegations**. There are exactly **four** native response records: two one-function initial responses and two completed, tool-free continuations. No repeated native status calls, automatic retries or additional function requests appear in this call's retained ledger.

## Native identity and argument audit

Exact arguments remain private in the indexed ledger. Both calls have `kind=new_request`, `target_intent_id=null`, empty clarification, and standalone intentional `request_text`; they are not caption-assembled. The second includes the owner's no-change-without-approval requirement. Each completed initial response has one matching durable submitted receipt; subsequent response completion has no function calls.

First request:

- Intent `status-2026-11-16-01`; root event `native:status-2026-11-16-01`.
- Delegation `item_Eksxn75evQ1axrQWhPREn`.
- Initial response `resp_0008625d7eb9a35b006afb21c33c8487d0afac95152131398b`.
- Function call `call_NoAVbgcr5eQUmCk6YW7egeRA`; item `fc_0008625d7eb9a35b006afb21c41d4087d0835e76d8e6184fac`.
- Continuation `resp_0008625d7eb9a35b006afb21c617b887d099ca70f69d438408`.
- Root ThreadMessage `964a6696-f91e-4162-a172-5593eb54195f`; StructuredCall `c40d50bf-77cb-4ec9-a737-dd5f7af77db9`, recorded engine `openai`, model `gpt-6.1-sol`, completed.

Second request:

- Intent `testflight-build-check-2026-11-16-01`; root event `native:testflight-build-check-2026-11-16-01`.
- Delegation `item_Ekt6ehlw5fUVGxCx5P7LZ`.
- Initial response `resp_0008625d7eb9a35b006afb23e8c0d087d0afe2022b7343a10a`.
- Function call `call_JksDHsggy62iA02QKtiWYc4m`; item `fc_0008625d7eb9a35b006afb23e9a87c87d086195e20a385c6db`.
- Continuation `resp_0008625d7eb9a35b006afb23ead4d487d0a53fc036c41d6c38`.
- Root ThreadMessage `da178d5d-c279-45b4-b840-9aeceaa0ad6d`; StructuredCall `18d6a943-e77c-4a8b-8e70-77dd6ea63343`, recorded engine `openai`, model `gpt-6.1-sol`, completed.
- Child `c408a7c1-83ea-45f2-912c-48b630b0fae9` retains `parent_thread_message=da178d5d…`, the same voice session/root event provenance, `model_retry_policy=never`, `tool_execution_policy=at_most_once`.

Both roots retain the exact owner/chat/bot normal-thread authorization, separate reviewed follow-up-cost grant, Django-user authorship, native claim/response/call/item identities, normal-thread execution mode and normal approval policy. The patch does not modify those paths.

## Root cause and guarantee boundaries

`thread_updates()` projects actual causal messages separately from roots. `thread_update_event()` correctly assigns transition-specific durable IDs, but renders all queued/delivered messages with the same generic text. Before this patch, `Assistant.deliver_results()` unconditionally claimed each such transition and called `append_commentary`, using only the shared root request ID in its silent explanation. Hence legitimate long work and a legitimate downstream handoff sounded like repeated synthetic waiting or state regression.

The actual pinned SDK defines `append_thinking` as knowledge without speech and `append_commentary` as content to say once. Neither append starts a managed backend request. Native function output independently sends `response.item.create` followed by `response.create`; the backend must continue even if a caller requests no reply. The new policy preserves that native durable transition instead of pretending it can eliminate mandatory continuations.

No result loss is evidenced: both root results were persisted, claimed and reflected in finalized assistant text. The child result completed after End and persisted unclaimed. Commentary-queued logs prove queueing, not remote provider consumption. Finalized assistant text is evidence of generated output, not exact commentary-to-speech correlation, complete playback or hearing.

**Exactly-once provider delivery/consumption is not an available guarantee.** Existing durable result identity is idempotent, and announcement claim is at most once. A crash between claim and send can lose the announcement; append acknowledgements identify estimated context injection, not playback. Adding retries or claiming exactly-once hearing would be unjustified scope and could duplicate speech. This patch neither hides nor widens that existing contract.

## Exercised offline verification

- Added regression first: failed before the fix on the actual SDK-emitted unsolicited `session.commentary.append` for queued work; passed afterward.
- Focused suite: **86 passed**, including native admission/replay/conflicting arguments, unknown-target status/continue/correct/cancel, interruption/failure rejection, normal approval approve/deny/uncertain, cross-owner ACL, no automatic failure retry, accepted work/results after End, and native continuation/standby behavior.
- New regressions exercise actual normal scripted bot handoff and real durable outbox with the installed SDK parser: queued root and queued child remain silent, repeated polling produces no repeated context, root terminal cannot regress to pending, both actual results get one announcement attempt, restored bridge cannot claim them twice, approval waits remain actionable once, failure remains sanitized once, and no task starts without normal approval.
- Separate throwaway runtime smoke used **actual 1.8.11 GPTLiveModel constructor, send/receive loops and parser over a real aiohttp TCP WebSocket bound only to 127.0.0.1**, plus disposable PostgreSQL and the changed `Assistant.deliver_results`. Forty routine observations produced **two thinking commands, zero commentary commands**; twenty terminal observations produced **one commentary command**. All four append commands reached the local socket server and were acknowledged. A matched native function output still produced exactly one `response.item.create` and one `response.create`. One connection; no remote provider/media/device or owner-task execution.
- Routine outbox records stayed `unknown`; terminal record became `announcement_claimed`. No SDK command claimed playback. The smoke's first two setup attempts failed on missing synthetic snapshot state/key; corrected setup passed. Disposable database/server resources were cleaned by `finally`.
- Targeted Ruff check/format passed using the installed `ruff` binary; the SDK virtualenv does not contain Ruff.

## Privacy, release and remaining acceptance

Raw snapshot/logs were retained privately under mode-0700 `/home/dev/.local/share/voice-waiting-20261116-task_e3fdeea2db77/`, individual evidence files mode-0600. They are not committed; this report contains only relevant short quotes and machine identities. No raw transcript, full request/result bodies, secrets, room/device capture or paid generation is published.

No merge, reload, deploy, shared configuration write, production database write, room creation, paid/provider test, owner-task test, cancellation/drain, devbox restart or RunPod mutation occurred. Main auto-promotes; exact-head independent review and CI, coordinator fresh five-minute notice and actual quiet call/turn/dependent-job gates remain mandatory before release. Real-device semantic quality and provider consumption/hearing remain unproved by this offline patch, and the older November 15 hangup remains unattributed.
