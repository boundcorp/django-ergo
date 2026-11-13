# Realtime voice progress

## Continuation — newest live session and UX, 2026-11-12

**Current deployed UX release, verified 2026-11-13 00:17:55–00:20:38 UTC:**
[PR #299](https://github.com/boundcorp/django-ergo/pull/299) merged at 00:00:01
as `97be767870f0e879c5839176ddbbe548e4fbaf71`, after exact-head review/CI and
an adjacent native 20-second quiet proof. One earlier gate timed out and another
fresh check found a resumed turn; both aborted without merging or stopping work.
The separately announced source window began at 23:49, not before 23:54;
a fresh delayed actual-rollout notice required not before 00:06.

[Image run 43333462081](https://github.com/boundcorp/django-ergo/actions/runs/43333462081)
built and smoke-started the release; publication completed **00:08:35**.
Registry `sha-97be7678…` resolved to
`sha256:9bc56efa94422eedf0f016ec4cc6cc9c8ad101c39331bb79174d095f4a39d7c8`.
At 00:09, live roles still ran `6b33f825` / `ffa206f6`: publication was not deployment.
After zero active voice sessions and the existing native quiet/single-flight
gate, the normal Octo upgrader merged [infra-octo #172](https://github.com/boundcorp/infra-octo/pull/172)
at **00:12:11**, commit `2c87f64534a039a5829216a2ce07dd0c04bc8ea5`.
Its entire delta is deployment image pins; exact #170 policy bytes are unchanged.
Targeted Flux applied that revision, and all seven desired role pods became
ready with actual image IDs and `ERGONAUT_VERSION` matching the values above.

Both web replicas and the actual bridge Python repeated the READ ONLY/no-dispatch
scope checks on source #299: exact policy, unsaved new one-request grant,
old ungranted call unchanged, other owner/thread/bot denied, no persisted session.
The actual exact-launch native status handler also passed with hard-failing
engine/send/Celery guards. Health/root/exact voice page and bridge readiness
returned 200, the bridge registered, and checked new-web logs had no HTTP 500–504.
Public `/assets/index-B5KbX8KW.js`, SHA256
`579b4df9967675caad197f9b117888346e18fb1a4473d8b4167b407bf25cf5cf`,
matched the file inside the deployed image.

The production App bundle was also rendered with synthetic intercepted API
responses: desktop/mobile drawer and voice/Diagnostics screenshots, zero write,
capture or RTC attempts, no browser errors or horizontal overflow. This made
no real API task, voice session, room, microphone or provider request.
Two compatible source #297 queue pods remained naturally terminating after
the UI-only source rollout; neither was force-killed or deprived of work.
They are not the older pre-#297 consumers that blocked initial activation.
The all-role source rollout preserved normal external Orca work.

**Known operational gap:** the existing native upgrade idle gate observes turns
and, under this deployment's configured policy, local jobs rather than external
Orca workers. It does not itself observe active voice sessions. This release
used explicit live-session checks and announced quiet windows; do not claim
that the native busy gate automatically protects calls. Physical Android/
Bluetooth and hosted normal orchestration acceptance remain open.

**Initial policy activation, verified 2026-11-12 23:38:39 UTC:** all old queue consumers were gone
at 23:28:46. The fresh policy-specific notice was acknowledged, with a quiet
window not before 23:34; final 23:34:36 checks found source/image/scope unchanged,
all desired roles ready, no active voice session and policy absent.
Infra-octo #170 merged **23:34:50**, commit
`eff442d9d3d0568c43b9dcdc026e2c1685af1641`, from reviewed head `7c69a9a`.
Flux `home-kubernetes` / `cluster-apps-ergonaut` applied that commit; only
web and voice-bridge were gracefully reloaded at **23:36:58**.
At that activation snapshot, both web replicas and the actual isolated bridge
runtime loaded the exact reviewed policy on source `6b33f825` / image `ffa206f6`.
All four queue-role pod UIDs were unchanged; no old-policy web/bridge pod remained.

READ ONLY, hard-failing engine/send/Celery guards verified the exact loaded scope
in all three replacement service pods: an unsaved new session received the
one-request grant, the retained ungranted call was not upgraded, and other
owner/thread/bot identities were denied. **Zero new persisted voice sessions**
or owner tasks were created. Public health/root/exact voice page returned 200;
bridge registration/readiness was 200; checked new-web startup logs contained
no HTTP 500–504 lines. This is configuration/handler proof without paid inference,
not hosted orchestration or physical Android/Bluetooth acceptance.
The UX changes in [PR #299](https://github.com/boundcorp/django-ergo/pull/299)
are a separate source release; policy activation did not deploy that frontend.
The earlier held-state and source-preparation snapshots below are historical.

The newest actual owner-bound call was created **22:46:28 UTC** and ended
**22:47:41**, on the exact approved Boundcorp launch thread. It ran after source
PR #297 (`6b33f825` / `ffa206f6`) was deployed through infra-octo #171, but
the normal-execution policy was still **absent**. Its durable mode was
`read_only`, with delegation unavailable because the existing CLI engine cannot
enforce the experimental API request limits; it had no normal-thread grant.
The older 20:34 call below is historical, not the latest-session diagnosis.

Sanitized failure chain: client protocol signal at **22:47:06.804 UTC**,
bridge receipt at **06.806**, acoustic/transcript boundary at **08.397**,
callback `ValueError` at **08.418**, and generic durable status at **08.423**.
There were **zero durable delegations/messages**, so no native orchestration
handler ran. Production logs retain only the exception type; **[INFERENCE]**
the underlying rejection was the admission engine-limit validation. The
identical missing-grant/unsupported-engine configuration reproduced that exact
validation before any message, including the actual callback's generic status.
Outer Kubernetes timestamps use `+01:00`; embedded UTC and database times
agree. This is timezone formatting, not an established clock fault.

The deployed bridge source supplies bounded owning-bot instructions, exact
launch-thread context/history and restrictions. GPT-Live has no callable voice
tools: it emits the client signal, and native tools execute only in the
delegated normal thread. The complete historical rendered provider prompt was
not retained, so full prompt delivery is not independently proven. Raw
transcripts/logs are protected outside the repository; only this sanitized
chain is published.

The deployed audio button calls `Room.startAudio()` only: installed LiveKit
2.23.0 resumes its playback AudioContext and calls `play()` on attached remote
HTML audio elements. It does **not** join a room or enable capture.
**Start call** separately connects the room and enables the microphone.
The UX continuation names the playback action **Enable sound**, shows it only
when the SDK reports blocked playback, and tracks SDK playback-status events.

Offline proof used actual native owned-thread/worker reads plus disposable
PostgreSQL, isolated Redis and non-eager Celery: owner isolation, zero child
sends before normal approval, stale/replayed approval 409, one authorized
native send, correlated child/reply and results after End all passed with
scripted model transport. No provider, room, microphone or real-owner task ran.
The actual installed browser `Room.startAudio()` also played a synthetic silent
HTML stream and reported playback allowed while its room stayed disconnected.
One scoped live native `ergo_thread_list` check also succeeded for only the
exact launch UUID (limit one), inside a PostgreSQL **READ ONLY** transaction.
Engine construction and native message/Celery dispatch were hard-failing
guards, not mocked successful results. This established the actual read
handler's accessibility without inference, task dispatch or production writes;
it did not test hosted voice end-to-end.

Current public frontend asset `/assets/index-Dn5F36IK.js` had SHA256
`51c934ad586fdb20944bef33f21c248544452d26cde7955266607d738d981d07`;
web and bridge both reported source `6b33f825`, with policy absent.
This current served-bundle observation does not prove the handset's actual
loaded bundle or browser/version during the retained call.

Desktop/mobile thread options now share purpose groups: Conversation (voice,
files), Model & reasoning, Thread data (export), and Thread lifecycle (resolve).
Rename, metadata, pins, model/effort, files, export and resolve remain available;
resolve is separated with a danger label and explicit queued/running-worker
cancellation warning. A new message reopens the thread, not cancelled work.
The voice conversation/observed states and essential controls remain upfront.
IDs, transport/provider/playback details, route configuration, runtime events
and local snapshots live under **Diagnostics**. Unavailable tools, callback
status, failed background work and normal approval links remain visible.

Full actual App/Chat/VoicePage offline-fixture screenshots, before/after at
390×844 and 1280×900, are retained in
`artifacts/voice-ux-20261112/` inside the worktree. Browser checks exercised
drawer focus/Escape, mobile modal focus containment, files/export/resolve,
navigation without capture, playback-only unlock, stable partial/final DOM and
manual-scroll preservation. Expanded mobile diagnostics left a 201 px transcript,
44 px controls, footer bottom 840 within an 844 px viewport, and no horizontal
overflow. These are offline browser observations, not physical Android or
Bluetooth acceptance; that output route remains **unknown**.

Final frontend verification: **287 passed**, TypeScript/Vite production build
passed. Existing React `act` and Vite chunk-size warnings remain. Obsolete
copy-only assertions were removed rather than pinning the new disclosure text;
navigation/capture-isolation, partial/final and routing behavior tests remain.
Temporary fixture/snapshot files were removed, the tab released and Vite stopped.

At the earlier **23:00 UTC** snapshot, policy #170 remained held for consumer exit.
At **23:00 UTC**, both `40d09a01` queue pods were gone; the two `3962d452`
queue pods still had live Celery processes and self-matching preStop processes.
Desired-ready replicas and an empty reserved queue cannot establish drain.
Source-release notice is not policy-activation notice: a fresh heads-up and
quiet window are required before a later web/bridge-only reload.

Policy review found that GitHub's old merge preview still used the pre-171
base and would reverse image pins if treated as a resulting tree. The held
draft branch was updated from current main, without activation. Refreshed head
`7c69a9a55402f63a881547ee278ed81f8d660174`, merge preview
`b0d9a4e850c38d50809715c0ae2bccd4eddadd40`, incorporates actual infra main
`1caa104293533c08a1978746a1fb1068406cb37a`; its complete resulting-tree delta
is only the five-line exact authorization addition, with no deployment/image
delta. Independent review approved these exact objects; Kustomize rendered
successfully with the unchanged `ffa206f6` application pins and exact JSON scope.
No stale policy checkout was applied wholesale. The reviewed refreshed policy
was activated only after the fresh drain/readiness/scope/quiet-window gates above.

## Hosted phone acceptance and follow-ups — 2026-11-12

Historical pre-activation preparation; current deployment/activation is recorded above.

Lee accepted **basic phone audio and conversation**, not Android Bluetooth/output routing or orchestrator capabilities. PR #293 first shipped as `06284d4` / image `5c0ba2e4` through infra-octo #167 at 20:13:34 UTC, with web ready at 20:23:13. A later unrelated auto-upgrade, infra-octo #168 at 21:02:40, moved all six current roles to **`09e5a6a` / `40d09a01`**, inheriting #293. Lee's dedicated-page observation is valid; previous “not deployed” statements described an older snapshot. See [time-separated evidence](realtime-voice-causal-review.md#current-release-reconciliation--2026-11-12).

Two phone sessions must not be conflated. The 18:09 `boundcorp` session on `75234dc` lacked launch-thread context and submitted no delegation. The latest retained session, created **20:34:10 UTC** on PR #293, emitted a valid client delegation and reached its acoustic/transcript boundary, then failed engine validation before creating any message: its actual `CodexCLIEngine gpt-6.1-sol` cannot enforce the API-only voice budget. Bounded exact-thread context is now implemented, but the read-only profile is not normal Boundcorp orchestration.

Lee requires the **same authorized tools, identity, exact-thread context and normal action approvals as text**, with existing threads/workers followed while voice continues. A global owner briefing, a new assistant, read-only listings or a mandatory Send button do not satisfy that contract. At **21:15:41 UTC** Lee explicitly approved **normal originating-thread execution costs**; the coordinator relayed that scoped decision. The library stays off by default, with a reviewed exact-chat/owner/bot GitOps policy prepared for this approval. No extra voice runtime, provider reroute, action-approval bypass or paid automated test is authorized.

Implemented and deployed in PR #293, building on standby:

- **Routing:** silent sink capability probe; truthful browser-applied output selector and optional native chooser; permission/ineffective-switch feedback, partial-switch rollback, device-loss fallback, fresh-track preparation, reconnect/standby-resume and listener teardown. Capture constraints and element playback are unchanged; unsupported phones get OS guidance, not a cosmetic Bluetooth toggle.
- **Exact-thread interface:** bounded owning instructions and normal context sources, saved compaction, recent launch-thread history, decisions/task state and attachment references. Voice events persist once into that thread's history with identity/timestamps and assistant delivery uncertainty. Global overview is excluded; existing authorized read-only bot/thread/worker/history tools serve on-demand lookups. Actual engine/model preflight, bridge-published restricted-mode UI status and same-session/outbox correlation preserve limits. No forwarding, write path or subscription-engine reroute is enabled.
- **Dedicated page/progressive text:** thread top-bar dropdown/mobile sheet opens `/s/:id/voice`, with exact identity/context and return navigation. A single app-level call owner survives SPA route/back/page unmount; a different thread cannot silently replace it. Explicit End call leaves submitted work alone. Stable SDK message IDs, coalesced bounded drafts and atomic final replacement prevent duplicate/reordered/disappearing turns; only finals enter normal history. Observed SDK state is timestamped and gated by live local transport, never guessed from queued work.

### Successor implementation — normal costs approved; source/policy rollout not performed

The successor adds an exact-chat/owner/bot authorization policy for normal originating-thread execution, separate from the priced API lookup. It is empty by default, cannot be granted by a browser or blanket flag, and requires a scoped JSON policy with an explicit approval reference. New sessions receive a matching durable grant only from that reviewed configuration. The proposed policy records Lee's 21:15:41 decision for the actual Boundcorp launch chat; no live policy was changed and no normal owner task was dispatched.

Reviewable changes: [django-ergo #297](https://github.com/boundcorp/django-ergo/pull/297)
and [infra-octo #170](https://github.com/boundcorp/infra-octo/pull/170). The policy
is held as draft for the explicit source-first/old-consumer-exit activation gate.

With a disposable offline policy, model-selected delegation uses the same real normal bot/queue/tools/context/approval paths, not invented voice tool schemas. One root request remains the limit; session and provider-ID claims prevent duplicate root dispatch, tool replay and stale approval reentry. Durable causal sends/replies and exact-thread worker reports are visible as separate status/result updates correlated to the original voice delegation. Normal approvals stay in the normal thread UI; speech is not approval. End/transport loss does not cancel accepted work, and ended-session polling persists late results without opening a provider.

The Android follow-up fixes the proven reversed RTC mute callback arguments and adds user-triggered, local, non-identifying route diagnostics. Core call controls stay visible while the diagnostic/footer area scrolls. No forced speaker, Web Audio playback migration, capture-constraint change or physical routing claim was introduced.

Observed successor verification:

| Scenario | Observed result |
| --- | --- |
| Standalone disposable PostgreSQL + actual isolated Redis Unix socket + actual Celery solo worker | Scripted model made five requests; actual authorized thread send ran once after the existing normal approval endpoint; voice text persisted while waiting and while child work ran; End preserved child completion and normal parent follow-up. No live owner task, provider, room or microphone. |
| Actual installed GPT-Live serialization, no transport | Six silent-context/commentary events correlated to the original delegation; a reconstructed assistant could not claim them a second time; durable result and populated thread context restored. |
| Final full Ergonaut suite, isolated PostgreSQL | **338 passed, 6 skipped**, including authorized normal delegation, owner isolation, approval/denial/uncertain-write claims, duplicate/concurrent delivery, reconnect mode, no automatic retry and refusal after the original 90-second deadline. |
| Library regression checks | Full suite **1,700 passed, 15 skipped** before the final retry-scope refinement; final **80 passed** across real-Codex/local-endpoint, budget and structured-call suites after that refinement. The actual CLI produced one request against a disposable local stand-in, not a real provider. |
| Final frontend suite/build, changed Python style, migration drift | **288 passed**, TypeScript/Vite build passed, Ruff passed and no model migration changes. Existing React `act`, pytest configuration and Vite chunk-size warnings remain. |
| Actual Chromium HTML element + LiveKit `RemoteAudioTrack`, synthetic silent source | Default sink applied before attachment; source track attached on fresh and reattached elements; disabled existing synthetic track reported honestly; zero capture/token/End calls. Not a physical Android/Bluetooth test. |
| Actual dedicated page, mobile 390×844 and desktop 1280×900 | Click-triggered local snapshot, physical route unknown, no identifiers/capture/token/End calls; sticky call controls remain visible while diagnostics scroll, footer fits and no horizontal overflow. Screenshots `/tmp/omp-sshots-15cbb5c634f46a86.webp` (mobile), `/tmp/omp-sshots-15cbb5dcb4346a87.webp` (desktop). |

The first standalone approval attempt was correctly refused for a non-admin fixture with admin-only default plugins. The successful isolated repeat used a disposable admin, not a permission bypass. A preliminary attempt to add mute coverage to the complex standby teardown fixture timed out; coverage was moved to the production microphone registration helper with the actual RTC emitter and real Assistant runtime, which passed. These are offline implementation checks, not hosted voice or Bluetooth acceptance.

Independent successor review reproduced one blocking continuation defect:
a provider response containing both an approval-required action and a valid
structured reply cached the reply receipt before pausing. Repeating that reply
ID after approval or denial lost the fresh output toolkit's acceptance state and
exhausted the normal turn limit. The fix revalidates only the local output
validator; external-action claims remain durable. Both regression cases failed
before the fix. A disposable standalone normal-thread smoke then completed the
same batched-action/repeated-reply path with one real thread send and four
scripted requests, preserving the non-admin 403, stale-answer 409 and correlated
child result after End. No provider, room, microphone or real-owner task ran.
Asynchronous worker/task handoffs retain normal controls, not the scoped
ThreadMessage receipt/retry policy.
Independent library verification passed **82 tests** across the real-Codex/local
stand-in, budget and structured-call suites; the two continuation regressions
are included. Scoped frontend verification passed **34 tests**. Changed Python
formatting and Ruff checks passed.

A concurrent unrelated auto-upgrade, [infra-octo #169](https://github.com/boundcorp/infra-octo/pull/169),
moved all six replacement roles to `dbf5ce7` / `3962d452` during this read-only
review. Older `5c0ba2e4` and `40d09a01` celery/bot-tasks pods were still
terminating. This is another release snapshot, not a PR #297 rollout or an
old-consumer-exit proof; the normal policy remained absent and #170 stayed held.


### Offline verification of follow-ups

| Check | Observed result |
| --- | --- |
| Full Ergonaut suite, isolated local `DATA_DIR` | **330 passed, 6 skipped**, including progressive SDK/correlation/lifecycle tests; scripted models only. |
| Full library suite, `ERGO_TEST_ISOLATED=1` | **1,700 passed, 15 skipped**, including budget/orchestration regressions. |
| Full frontend suite | **286 passed, 0 failed**, including routing, navigation, progressive transcript, scroll and visible safety messaging. Existing React `act` warnings remain. |
| Production frontend build | Passed TypeScript and Vite; existing >500 kB chunk warning remains. |
| Changed Python Ruff and formatting; migration drift | Passed; no model migrations generated. |
| Disposable standalone runtime (not pytest) | Populated-thread task/summary restored, user/assistant voice text persisted idempotently, two-turn scripted history read answered under budget, same-session outbox claimed once with original delegation ID, reconnect context retained. No provider/room connection. |
| Chromium earlier modal routing fixture (surface superseded by dedicated page) | Selected mock headset, observed device-loss default fallback, removed sink API and observed no selector plus phone guidance; no microphone or room. |
| Chromium actual App/Chat/voice page: desktop 1280×900 and mobile 390×844 | Top-bar/mobile-options entry, exact identity/context, same DOM bubble across partial/final, manual scroll preserved and jump to latest, return navigation with zero End/stop/token/capture calls. Explicit End incremented only the voice-end counter; background stayed queued. |
| Standalone progressive runtime reproduction and correction | Opposite-order overlapping finals initially reordered turns; after correction IDs, order and first-observed timestamps stayed stable. Drafts never entered thread history, final rows appeared once, End reset live state without deleting finalized text. |
| Late final after End regression | Reproduced a finalized turn missing from the ended page after End's first response; the viewed ended session now continues slow read-only observation, and the same regression passes without opening transport. |

Browser screenshots: `/tmp/omp-sshots-15cb8fb07d03dda0.webp` (selected), `/tmp/omp-sshots-15cb8fc06a43dda1.webp` (loss/fallback), `/tmp/omp-sshots-15cb8fc0c283dda2.webp` (unsupported). Temporary fixture files/scripts and the Vite service were removed/stopped after proof. These are browser-state and scripted application checks, **not physical Bluetooth routing or hosted semantic acceptance**.

Dedicated-page evidence supersedes the modal screenshots for layout: `/tmp/omp-sshots-15cb98f9daf1c163.webp` (desktop), `/tmp/omp-sshots-15cb98f94dd87f75.webp` (mobile), `/tmp/omp-sshots-15cb9953aa587f76.webp` (mobile thread-options entry). Actual mobile measurement after fixing clipping: transcript **279 px** tall, footer bottom **836 px** within an **844 px** viewport. Long context/limits and background details are collapsible, but restricted mode/unavailable reasons, closing-boundary warnings and routing failures remain upfront. The fixture made no room/microphone calls; scripts, temporary entries and Vite service were removed/stopped.

### Device acceptance checklist — Lee, after approved rollout only

1. Record handset, OS/browser version, tab/PWA mode and Bluetooth headset model, including call-audio/HFP support. Connect Bluetooth before opening voice. Check the phone's actual call/media output while the microphone is active.
2. Where the browser supports output sinks, select a permitted output and confirm the **applied** output label changes only after success. Listen on Bluetooth and system default separately; default is not a promise of speakerphone. Decline the output permission once and confirm honest error/phone-controlled guidance, not a false successful toggle.
3. With a sink selected, disconnect/reconnect Bluetooth. Confirm the list updates, device loss falls back truthfully and audio remains usable. On a phone without selection, use its OS call/output picker; record whether Bluetooth offers call audio or only media audio.
4. Start in a populated thread with a known current task/decision, summary and attachment reference. Ask about that task without restating it; verify voice uses the launch thread, not another bot's private chat. In restricted mode confirm the UI and spoken interface explain which lookups can run, or why this thread cannot delegate.
5. Only after separate normal-task-cost approval and reviewed gate enablement, request one hands-free task in the actual Boundcorp launch thread. Verify the original delegation ID, same owner/context/real engine, actual registered status/send/manage handlers, normal approval/deny behavior, durable child progress/completion and no repeated external write on reconnect. Keep talking while work runs. API-only read lookups do not substitute for this criterion; a prompt promise or generated recap is not successful orchestration.
6. If standby was separately enabled, request standby at a safe boundary, resume within the original deadline and verify the same applied output, task, decisions and results persist. Reopen/reconnect as allowed and check voice text appears exactly once in thread history. The 90 seconds still include standby; a second request/paid retry is not authorized.
7. Enter voice from the originating thread's top-bar options on desktop and mobile. Confirm its bot, thread title and return link stay visible; navigating back within the app must not silently end the call or cancel delegated work. Explicit **End call** stops voice, not already-submitted background work. Check the continuing-call indicator away from the page; a second thread must not silently replace it.
8. During a separately authorized device call, watch partial user/assistant speech update the same turn, then finalize without a second bubble. Scroll up manually while more text arrives: position must remain stable until choosing latest again. Compare displayed observed voice states with the actual SDK/microphone behavior; unknown state and queued work must not be relabeled “thinking” or “running.”

**Release gate:** Lee's distinct normal-task-cost authorization is recorded, but source and exact-thread GitOps policy still require review and passing checks before announced rollout. Deploy source with policy absent first; verify every old task consumer has exited, including terminating pods, before policy activation and a quiet announced web/bridge reload. Do not activate across mixed-version consumers. No mandatory Send button or speech-as-approval behavior is accepted. Standby enablement and paid/provider/room/microphone tests remain separately scoped. This worker made no merge, rollout, restart, live infra/config/secret mutation or paid call; offline checks are not physical Android or hosted model acceptance.

## Current branch

The off-by-default prototype uses Django-owned logical sessions, finalized events, bounded extractive compact state and reported-activity anchors, stable delegation IDs, a durable completion outbox, a single chat card per voice session and a minimal Stage. User speech times supplied by a client are separate and have unknown clock error. Elapsed time is calculated since server receipt of the user's report, not since a verified physical action. The rules are not a semantic summarizer; unmatched statements can fall outside the bounded transcript window. The model's actual live use of silent time context remains unverified.

## Live attempts and offline follow-up

The first approved paid attempt produced hosted voice audio 1.677 seconds after synthetic speech ended, but its delegation never ran. The bridge lacked Redis's Python transport dependency and its post-commit enqueue failed. The disposable database still has the message `queued` with an empty error; it has not been replayed, and both first-attempt markers remain.

The isolated bridge now pins `redis==8.1.0`. Offline preflight publishes and consumes a harmless Celery probe on a unique temporary queue using the actual Redis transport; it never invokes a model or a worker. Voice dispatch failures conditionally mark a still-queued message failed with a library-authored not-run reason, visible through the same session's durable result outbox. A message already claimed by a worker is not overwritten. There is no automatic paid retry; a new request requires explicit action and, for this experiment, fresh approval.

Lee explicitly approved one second attempt after the fix, with the same conditional $0.15 plus tax planning reserve, not a financial cap; shared allowance was unknown and disclosed. On 2026-11-12 a fresh isolated database, broker, worker, chat, voice session, dispatch, room prefix and markers were used. Before connecting, the bridge environment published and consumed the actual Django Celery delivery task on isolated Redis without a worker or model, the queue was empty, and the worker's routes and `gpt-6-luna` budget were verified. The first queued message was never replayed.

The second synthetic participant received non-silent assistant audio 1.730 seconds after speech ended, but no delegation was created, queued or executed: the final voice session had zero delegations and the worker received no task. The assistant nevertheless said it had checked the history, which was not verified background work. There was no durable delegated result. The supervisor stopped after 71.451 seconds; the bridge and client exited, both second markers remain, and a subsequent read-only room listing found no matching room. Track receipt is not browser playback or human hearing.

Actual provider spend and billed voice duration remain unknown: the runtime key received HTTP 403 from OpenAI usage and cost endpoints. Both historical one-shot marker pairs and the first queued message remain untouched.

Offline causal review: [the evidence report](realtime-voice-causal-review.md) documents the installed SDK contract and timestamped transcript. Client delegation has no tools to register. `pending_transcript` and SDK final segments are not reliable complete utterances. The initially added mandatory owner confirmation is superseded by the hands-free correction below. A durable outcome is supplied as silent authoritative context before ID-correlated commentary; generated speech remains unverified.

The second attempt did not retain a complete wire-event trace, so upstream non-emission, target mismatch or callback failure cannot be distinguished retrospectively. An instruction gap was found, but its role in the model's choice is a hypothesis. Silent time/state context was not supplied as fulfilled history results.

Follow-up verification: 29 targeted backend tests passed (seven actual installed-SDK event-contract cases), 34 library budget tests passed, 240 frontend tests passed, production build and changed-Python Ruff checks passed. A standalone real no-room `AgentSession` smoke exercised actual framework wiring, finalized speech with empty pending text, a queued bounded scripted two-turn model, same-session durable answer and ID-correlated commentary; its provider transport was parked, not connected. Chromium visually verified the actual card's no-request, queued and answered states and expanded unverified transcript. This proves offline application behavior, not real hosted model behavior. The report includes a concrete future acceptance procedure requiring fresh approval.

Lee subsequently approved exactly one third attempt. A fresh isolated broker, worker and owner-scoped hidden pantry fact were preflighted without inference; real search returned the fact and excluded another owner. The live run produced a valid client delegation, one same-chat Celery delivery, a bounded two-turn `gpt-6-luna` history search and a durable answer with the seeded fact, followed by SDK-queued commentary correlated to the original delegation ID. The synthetic audio track received a first non-silent frame 5.977 seconds after speech began, while speech was still ongoing; this is not browser playback or human hearing. The detailed [causal report](realtime-voice-causal-review.md) records timestamps, tokens, the early partial-speech delegation, missing later request fragments and a stale contradictory generated status after the result. Billing remains unknown, the third markers are preserved, the room subsequently disappeared, and no fourth attempt is authorized.

The post-third offline confirmation correction initially required the owner to type and confirm the complete request; that was temporary and is now removed. The current bridge waits for a model-selected delegation, real Python Silero speech/end events, a configurable 2-second default pause grace, and quiet finalized transcript fragments before submitting. A resumed speech event extends the request. A longer pause, VAD miss or omitted/late transcript can still cut off instructions; acoustic silence is not semantic completion. No room join, inference, replay, production mutation or new authorization was used for this correction.

Offline verification: 36 targeted backend cases passed, including the installed GPT-Live parser and an actual no-room `AgentSession`/Silero ONNX pipeline fed FFmpeg/flite PCM with the provider transport parked. The SDK/queue regression covers early delegation, an internal pause and resumed speech, a late finalized tail, duplicate events, missing-final timeout, stop and silent result context before correlated commentary. The library budget suite passed 34 cases; frontend build and 240 tests passed; changed-Python Ruff checks passed. A local Chromium fixture showed the actual room and history card collecting without a confirmation form. These checks do not establish hosted audio, transcript completeness, playback or model truthfulness.

Browser callers can create, read, end and obtain room-scoped microphone-only tokens for their own voice sessions, but cannot write arbitrary events, state or delegations. A signal is handled hands-free without owner confirmation; the session may be ended to stop unsubmitted work. Background delegation is off unless `REALTIME_VOICE_DELEGATION_BUDGET` is set. A delegation is a thread message that carries that budget, and the turn runner enforces it server-side: model turns, input and output tokens, a wall-clock deadline, an allowlist of read-only tools, no worker, agent or background-task starts, no thread sends, no retries, no approvals. A limit ends the turn as a truthful failed result that the same voice session receives through the durable outbox; a turn without a budget is unchanged. A locked durable claim allows at most one commentary attempt per result across connections; a crash after claim may lose that announcement, but the linked result remains visible. The claim is not evidence of playback or hearing. The inspected GPT-Live adapter's `append_commentary` returns no speech identifier for correlating an announcement to browser playback. Model behavior on restored context is not deterministic.

## Adapter and setup

The bridge pins `livekit-agents`, `livekit-plugins-openai`, and `livekit-plugins-silero` at **1.8.11** and uses the distinct `GPTLiveModel(model="gpt-live-1", delegation="client")`. Its OpenAI SDK 3 dependency conflicts with this repository's SDK <3, so the bridge environment is isolated. See [the released adapter source](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/realtime/gpt_live_model.py) and [LiveKit GPT-Live guide](https://docs.livekit.io/agents/models/realtime/gpt-live.md). Documentation and local imports do not establish hosted entitlement.

The existing Ergonaut LiveKit project is linked in a protected CLI profile and its read-only rooms API succeeded. The local app configuration is encrypted with SOPS and has mode 600. An isolated process received the decrypted fields only in its environment; it imported the bridge, loaded the test settings, and produced and verified a real-key signed room token with the expected identity, five-minute TTL, microphone-only publishing, no data/admin permission and `ergo-voice` dispatch. This did not connect a room or start inference. A read-only hosted agent list showed no deployed agent with that name, but cannot exclude external workers.

The live bot registry defaults to `auto/medium`; its OpenAI provider lists `gpt-6-luna`, and the design bot explicitly pins the API model. The Octo runtime OpenAI key returned HTTP 200 with exact IDs for the `gpt-6-luna` and `gpt-live-1` model metadata endpoints, while the local shell key returned 401. Metadata alone is not proof of paid inference entitlement.

## Historical first-attempt preparation

The first disposable instance was prepared at `/tmp/ergo-voice-isolated-e1cy2fyd` (ephemeral, not a deployment). Its PostgreSQL uses `pgdata` under that root; its Redis was isolated on `127.0.0.1:46233`, DB 0. The `celery` queue was empty before the isolated solo worker started. The `voice-local` account, `voice-isolated` bot, a disposable chat and voice session exist only in this DB. `environment.sh` and `account.txt` are mode 600; do not print their contents. The worker received the Octo runtime key in its process environment, not a file. Django HTTP on `127.0.0.1:60589` returned 200 at the time.

The unique dispatch is `ergo-voice-test-ea790bf1705afa63c80ff89e9ee2d455`; the room prefix is `voice-test-ea790bf1705afa63c80ff89e9ee2d455-`. An authenticated local API session created a signed token with that exact dispatch and room, microphone-only publishing and no data publishing; it did not join a room. The bridge rejected a wrong room before connecting. The pinned LiveKit SDK defaults recording to the server setting unless explicitly overridden; this bridge passes `record=False` to `AgentSession.start`. This is code and local SDK inspection, not an observed hosted audio session.

Offline preflight passed with the real local token credentials and rejected a wrong LiveKit key, bot model, dispatch, database route, broker route and provider endpoint override. An unapproved `realtime_voice_local` invocation exited without starting the bridge. That wrapper separately reads Octo credentials, so it is not offline. Online read-only checks returned HTTP 200 and matching IDs for both OpenAI model metadata endpoints and successfully listed LiveKit rooms; none proves inference entitlement. A wrong OpenAI key cannot be authenticated offline: the approved launch checks model metadata before starting. No paid calls or room joins were made.

The first attempt's PostgreSQL remains available for read-only inspection; its original Django, worker and Redis processes are not running. Preserve its database and consumed markers. The second attempt used `/tmp/ergo-voice-isolated-dy_xdck4` with a separate PostgreSQL, Redis and worker; its bridge and client have stopped. Do not replay either attempt or delete their markers.

## Local evidence

- Backend, Ergonaut: `cd ergonaut && PYTHONPATH=.:../src /tmp/ergo-voice-test/bin/pytest -q --disable-warnings` — 278 passed, 5 skipped (264 before the budget work). Library: `ERGO_TEST_ISOLATED=1 /tmp/ergo-voice-test/bin/pytest tests -q` — 1693 passed, 15 skipped on a throwaway database (the test venv needed `python-jose` for the legacy knowledge example app; the default reused embedded database errors on a second run with a leftover `ergo_bot_realty_house` table, which is existing test hygiene and not related to this work). Budget tests: 34 in `tests/test_conversation_budget.py` (each limit, the allowlist, approvals refused, worker, agent, task, wake and thread-send guards, setup work and nested calls under the budget, no retries, no compaction, engine and model gating, malformed budgets, a call without a budget unchanged); 10 in `apps/bots/tests/test_turn_budget.py` through the real queue and turn runner (each limit failing the message truthfully, a disallowed tool, `ergo_worker_start` and a worker start from an allowed tool, no approval pause, invalid and expired budgets, an unbudgeted request unchanged including its approval gate); 4 in `api/tests/test_realtime_voice_budget.py` (the cooking and elapsed-time scenario returning to the same voice session, a budget stop reported to it, an exception text not relayed, the priced profile valid and its tools offered). The model is a scripted fake client, so no provider inference or live dispatch occurs. A read-only review of the diff found no unbudgeted path and no retry path. It found that the guards and deadline started only in the model loop, that the wake and orchestrator send paths were unguarded, and that the deadline backstop wrapped response storage; all three were fixed. The first two have tests; the third is only exercised by the deadline test, which cannot show a storage write being cut off.
- Frontend: `cd ergonaut/frontend && bun test` — 233 passed; React emitted existing act warnings. `bun run build` succeeded; Vite reported a chunk larger than 500 kB.
- `cd ergonaut && PYTHONPATH=.:../src DJANGO_SETTINGS_MODULE=ergonaut.settings.test_settings /tmp/ergo-voice-test/bin/python manage.py makemigrations --check --dry-run` — no changes detected; the same for `makemigrations django_ergo --check`. The budget adds no model field.
- Chromium rendered the actual `VoiceSessionCard` component with a local cooking fixture: current flip, ingredients, missing ingredients and completed marinade appeared. This was not hosted audio or a full authenticated UI journey.
- Hosted synthetic track receipt was measured for each attempt; in the third, the durable result arrived 4.773 seconds after the wire signal. Interruption, reconnect and browser playback were not measured.

## Remaining gates and rollout

The third attempt demonstrated a real delegated history result, not browser playback acknowledgment or human hearing. Provider billing and measurement of how conservative the input estimator is remain unresolved. The $0.15 plus tax reserve per approved attempt was conditional, not an enforceable spend ceiling. The shared Build allowance was unknown and disclosed at approval; post-attempt billing remains unreconciled.

No further paid attempt is authorized. Before any future test, separately approve the shared project and spending assumptions and confirm isolation and fresh one-shot state. The offline hands-free correction has not been exercised with hosted speech; browser microphone/playback, interruption, reconnect and spoken truthfulness remain separate gates.

No documented read-only API for the shared remaining Build allowance was found: the Cloud API requires a separate user session and its documented project object has no plan or remaining-allowance fields. On the project's **Billing** dashboard (`https://cloud.livekit.io/projects/<project-id>/billing`), inspect plan and current-cycle WebRTC participant-minutes and downstream data usage. The exact dashboard remaining-allowance field was not observed; because Build quotas are shared across the user's free projects, project-level usage alone may not establish the remainder. Resolve that before approval, including with LiveKit support if necessary.

A one-shot bridge cap and outer timeout are not hard provider-billing limits. Before any shared rollout: separately review migration and retention, compare reported usage with the estimator, measure live behavior and billing, then separately approve deployment and migration. Rollback stops only the isolated processes; persisted history and already queued work require explicit handling.
