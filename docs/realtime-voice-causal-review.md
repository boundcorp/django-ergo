# Realtime voice delegation: offline causal review

## Android Brave input audit — 2026-11-13

The latest correlated realtime call was created **15:51:15.139 UTC**, claimed by
the bridge **15:51:18.774**, and ended **15:51:52.977**. Both web replicas retain
the realtime creation/token/End routes. A distinct composer transcription POST
and completed `gpt-transcribe` usage row followed at **15:52:28** on the same
owner/thread. This is route/session evidence, not an inference from “Whisper.”
Exact private identifiers and raw logs remain protected, not in this document.

Current inspected web/bridge source is `3c840afa507a8600797826bf97491d990a09b109`,
image `sha256:c7b5a1836246dddcb0b802ee8416d8b8cb566d37afab9c03747f328578cf1d3c`.
Public `/assets/index-BXufJawh.js` SHA256
`c40aa09dd6aab1c62d2bb1d294094dbc9332cc6574e10f1dde129e75a21952dd` matches
that image's actual asset. The isolated bridge reports agents/OpenAI/Silero
**1.8.11**. Historical #299 `97be7678` / `9bc56efa` is not the current release.
The handset's cached bundle and Brave version/tab-versus-PWA remain unknown.
No competing implementation PR was open during inspection; #298/#302 are
historical release-documentation drafts, not another active voice owner.

| UTC | Retained pipeline observation |
| --- | --- |
| 15:51:20.079 | Bridge listener ready; RoomIO linked participant at 20.264. |
| 15:51:20.644–20.868 | Browser subscribed to the assistant track and reported element playback. Neither proves generated sound or hearing. |
| 15:51:20.883–23.584 | First actual Silero speech start/end. |
| 15:51:25.235–27.683 | Second actual Silero start/end. |
| 15:51:36.382–38.334 | Third actual Silero start/end. |
| 15:51:52.998 | Explicit End POST 200; SDK then closed `CLIENT_INITIATED`. |
| 15:51:54.415 | Bridge wall time **34.438 s**, provider-reported usage **32.0 s**; not 90-second expiry. |

There were **zero retained user/assistant finals and zero delegations**. No
provider error/traceback/generated-assistant-audio observation appears in the
retained bridge window. **Earliest proven gap: incoming audio reached Silero
recognition, but no provider text/output was retained.** Speech detection does
not establish intelligibility, hardware microphone, nonzero provider input or
successful input forwarding. Provider usage duration is not input receipt.
There is no retained capture enabled/muted/ended/suspension history, permission/
devicechange record, local input level, outbound RTP counter or provider input
acknowledgment. Missing metadata means unknown, not user fault.

Lee confirmed trying **both phone microphone and Bluetooth headset** at 16:05 UTC.
Those are attempted physical inputs, not evidence the browser switched capture
source. Android, Brave and Bluetooth are user facts; no browser-specific cause
has been established. Input microphone, HTML output sink and Android
communication routing are different boundaries.
[Brave's Android permission controls](https://support.brave.app/hc/en-us/articles/35849239473037-How-do-I-change-or-manage-site-permissions-Android)
do not prove denial here.
[Track mute](https://developer.mozilla.org/en-US/docs/Web/API/MediaStreamTrack/muted)
differs from intentional disable;
[devicechange](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/devicechange_event)
does not establish physical routing.
[Android's native communication-device API](https://developer.android.com/develop/connectivity/bluetooth/ble-audio/audio-manager)
is not an HTML output selector. Sources checked November 13.

A separate actual component regression reproduced the proven UI defect before
the fix: a native ended track still displayed **Microphone: enabled**.
The patch observes capture/publication transitions, offers explicit same-room
recovery and a click-only local existing-track PCM probe, and records bounded
payload-free bridge/provider stages. No SDK forwarding defect was proven, so no
speculative forwarding/routing change is made. In SDK 1.8.11 the queued-audio
callback occurs before `send_str`; it explicitly does not prove remote receipt.
The original normal-task authorization, approvals, one root request and
90-second deadline remain unchanged. Physical Android input/transcription and
Bluetooth acceptance remain open; no paid/provider/room/hardware-capture test,
live-owner task, merge, deployment, restart or configuration change ran.

## Current release reconciliation — 2026-11-12

**PR #293 is deployed.** The prior `75234dc` evidence below described the earlier
18:09 UTC call, not Lee's later dedicated-page test. Lee's report of seeing the
new page is consistent with the deployment and retained session evidence.

| Release evidence | Read-only observation |
| --- | --- |
| Application source/build | `06284d4358fa6c3d17e2ac181a8b9485328a0c1a` (PR #293 merge); [Ergonaut run 43295864949](https://github.com/boundcorp/django-ergo/actions/runs/43295864949) succeeded. |
| GitOps pin | [infra-octo #167](https://github.com/boundcorp/infra-octo/pull/167), merged **20:13:34 UTC**, source pin commit `cd37e38806fa94470935cf593ef94d31d5925a23`, merge `9428406dd6a57df090fbd549eaca90f8d8aa8e7a`. |
| Initial audit runtime image | Six replacement roles resolved to `sha256:5c0ba2e4a8bb12ff6b662c3ee4ec83320c8a8741550703c3ca9ba6d6943a83de`, each reporting `ERGONAUT_VERSION=06284d4358fa6c3d17e2ac181a8b9485328a0c1a`. This is the later phone call's release, not the final audit's latest release. |
| Initial deployment revisions | `web=159`, `celery=163`, `bots=154`, `bot-tasks=162`, `beat=148`, `voice-bridge=2`; desired updated/ready replicas were present. Web progression completed **20:23:13 UTC**. Older terminating celery/bot-tasks pods still exposed `588993d3`: they were not the replacement release. |
| Actual public frontend | `/s/587014b6-bba3-4b8b-aec8-d8b4f13b4d70/voice` serves `/assets/index-C5FPd5cF.js`, SHA256 `80be02170e627ee2f7764eb405a0a88f82db1a2908752e14ea8782de0a344767`, identical to that file in the live web image; the bundle contains `/s/:id/voice`. |
| Subsequent unrelated auto-upgrade | [infra-octo #168](https://github.com/boundcorp/infra-octo/pull/168) merged **21:02:40 UTC**, pinning `09e5a6a8453697e777c2168516776259e519ce5c` / `sha256:40d09a01a5a8d2c2697775915cd678c2d0d64a3c200e9a67767655fb6e02b9a1` (PR #294, binary artifact delivery, inherits #293); [build 43304668336](https://github.com/boundcorp/django-ergo/actions/runs/43304668336) succeeded. All six current non-deleting roles' actual imageIDs and `ERGONAUT_VERSION` values match. The same public frontend bundle/hash remains in that image. |

The final audit still finds terminating celery/bot-tasks pods on the previous
`5c0ba2e4` release. **Ready desired replicas do not prove all old queue consumers
have exited.** Normal execution must remain unconfigured through source rollout;
verify old consumer exit before activating the reviewed scoped policy. Neither
observed auto-upgrade was initiated by this worker.

The runtime rollout did not require a new manually identified owner: the GitOps
PR records the existing quiet-window auto-upgrader. No rollout was initiated by
this investigation. Cache, CDN or stale-SPA skew are possible in general but
**not established here**; the currently served bytes match the image. A retained
access log is not proof of the exact URL or cached bytes on Lee's handset.

### Latest phone session: orchestration actually attempted, then blocked

Latest retained owner session `a9320316-5879-46d1-9d9e-ff62ecfebe7c` belongs to
the same `boundcorp` launch chat `587014b6-bba3-4b8b-aec8-d8b4f13b4d70`.
Created **20:34:10.107 UTC**, bridge claimed **20:34:12.123**, ended
**20:34:43.585**. This is distinct from the earlier 18:09 call and consistent
with Lee's subsequent report around 20:36; the report time is not the call time.

The latest session has two user finals, one assistant final, one delegation
signal and one bridge-action failure event, but **zero delegation rows/messages**.
Retained bridge timestamps establish:

1. Listener ready **20:34:13.198 UTC**.
2. Valid client-target wire event **20:34:27.473**, application signal **27.474**,
   payload-free correlation `f3daeed21b70ca13`.
3. Speech/transcript boundary ready **20:34:28.270**.
4. Callback failed with `ValueError` **20:34:28.308**; durable capability status
   explicitly rejects subscription/CLI per-request limits.

The actual engine resolves to **`CodexCLIEngine`, `gpt-6.1-sol`,
`supports_request_limits=False`**. It is not an OpenAI API engine merely because
the stored `engine_type` says `openai`. The registered launch-chat tools include
normal history, `ergo_bot_list`, `ergo_thread_list/send/forward/stop/resolve`,
`ergo_worker_list/cancel`, todos and wakes. These are real normal bot tools;
none is made callable inside GPT-Live's client-delegation mode. PR #293 restores
bounded exact-thread instructions/history/summary/state/attachment references,
but its read-only `gpt-6-luna` delegation profile cannot invoke the actual routed
engine. No task reached its model, tools, approval state or completion outbox.

Read-only reconstruction of the **current** registered launch context measured
**6,962 owning instruction characters** (below the 8,000 startup cap), five
`TextContextSource`s rendering **1,962 characters** (below the 2,200 configured
source slice), and the excluded `OverviewSource`. The launch thread currently
has **2,929 normal messages, four compactions and 83 attachment references**;
the latest session's two user finals and one assistant final each have a normal
history row. Startup still takes only a bounded recent-history/summary slice,
not all 2,929 messages. Exact provider-transmitted startup prompt bytes were not
retained, so reconstruction is not a claim of a recovered wire payload.

The actual bot's normal limit is **50 model turns**, not the experimental lookup's
four. Normal child tasks/compaction/tools can have additional costs, and there
is no claim that the prototype's dollar estimate bounds that work. The new
settings policy is absent from the live image. Lee approved distinct normal
originating-thread costs at **21:15:41 UTC**; source/policy review and a safe
announced consumer cutover remain prerequisites.

**Product acceptance failure:** the one-shot read-only profile is an experiment's
spend guard, not the requested Boundcorp product contract. Voice needs the same
authorized exact-thread orchestrator execution and normal action approvals as
text. Do not fix this by changing the bot route, registering invented LiveKit
tools, globally widening permissions, or removing the budget from automatic
model-selected requests. CLI engines cannot enforce the existing token budget.
The successor separates the bounded transport from a durable exact-chat/owner/bot
normal-execution grant, carrying Lee's approval reference, real normal tools and
approval reentry, per-invocation side-effect claims and correlated updates. It
retains one request and the 90-second voice cap. No source/policy rollout or
real-owner normal task was performed during this investigation.

Review handoff: [source #297](https://github.com/boundcorp/django-ergo/pull/297)
and [scoped activation policy infra-octo #170](https://github.com/boundcorp/infra-octo/pull/170).
The policy PR is deliberately held as draft until reviewed source is deployed
and every old queue consumer has exited. Neither PR is an Android/hosted
acceptance claim or permission for an automated paid test.

### Android route evidence and proven microphone callback defect

Android handset and the already-selected OS headset route are user-provided
facts. Browser/version/tab-versus-PWA remain unknown: the voice schema stores
none, and **34 retained latest-window voice access lines** contain no browser
tokens. No transcript text, raw device IDs or secrets were exported.

The deployed playback path remains HTML audio elements, LiveKit 2.23.0
`webAudioMix=false`, sink applied before remote track attachment. No forced
speaker, Web Audio mixer or changed capture constraints was found. Capture
defaults are default input, echo cancellation, auto gain, noise suppression and
voice isolation. System-default sink selection does not establish the active
Android communication route while capture is open.

Current [MDN compatibility source](https://github.com/mdn/browser-compat-data/blob/main/api/HTMLMediaElement.json)
marks **both Chrome Android and Firefox Android `setSinkId` unavailable**, linking
[Chromium 41276355](https://crbug.com/41276355) and
[Firefox 1473346](https://bugzil.la/1473346). This is a platform restriction,
not proof of Lee's browser or the cause on his handset. Android distinguishes
[Headset/Hands-Free and A2DP profiles](https://developer.android.com/develop/connectivity/bluetooth/profiles);
the native [communication-device API](https://developer.android.com/reference/android/media/AudioManager#setCommunicationDevice(android.media.AudioDeviceInfo))
is not exposed by an HTML sink selector. Do not blame the user's OS setup or
claim an applied default/selected sink proves physical Bluetooth playback.

An independent, proven defect appears at **20:34:39 UTC**: `track_muted` raised
`AttributeError: RemoteTrackPublication has no attribute identity`. Installed RTC
source emits **`(participant, publication)`**; the bridge handler reversed these
arguments. This breaks microphone-state invalidation, not proof of a Bluetooth
route cause. Correct the callback and retain real-SDK-emitter regression coverage.
User-triggered route diagnostics should report element state and existing
capture settings, browser family/major and platform, never device IDs/names;
physical output remains **unknown** and no collection may open a microphone/room.


## Hosted phone diagnosis — 2026-11-12

**Accepted:** Lee's real phone audio and basic conversation. **Not accepted:** Bluetooth/output routing, orchestrator context/tools, standby/resume, and the subsequently authorized dedicated-page/progressive-transcript UX. This supersedes historical statements below that the prototype was not hosted or that phone audio had not been tested. These follow-ups are draft-only; no merge, deployment, restart, room join, microphone session or inference is authorized in this investigation.

### Actual session and evidence boundary

Read-only Octo inspection confirmed that `web` and `voice-bridge` use image `sha256:588993d3ed4f695227b3676e08f701441fe50fded8df9e0b4349d3a2759f50d4` (source `75234dc`). The bridge command uses `/app/.voice-venv/bin/python -m ergonaut.realtime_voice_bridge start`. `envcheck` passed both cluster readiness checks and the documented SSH routes before inspection. Production ORM inspection ran inside `SET TRANSACTION READ ONLY`; no transcript text or credentials were needed.

The design discussion link (`3ae8f30a-1fc6-48cd-ab78-2fa7ed6c77ea`, bot `design`) is **not the phone session's launch chat**. The only voice session for that owner on November 12 was `b9248989-db55-43bc-b714-5199fe6c5970`, created at **18:09:21.514 UTC**, bridge claimed at **18:09:23.161 UTC**, ended at **18:10:11.719 UTC**. Its launch chat was `587014b6-bba3-4b8b-aec8-d8b4f13b4d70`, bot `boundcorp`, stored `engine_type=openai`, `transport_type=cli`, empty model override. A stored engine label is not proof of a budget-capable API engine: the resolved engine and model must be checked.

Before voice began, the launch `boundcorp` thread already held **6,962 instruction characters, 2,874 normal messages, four compactions and 83 attachment references**; its stored routed model was `chatgpt/gpt-6.1-sol` (subscription/CLI, not the budget's API `gpt-6-luna`). The separate design chat held 5,398 instruction characters, 1,731 messages, one compaction and 40 attachments. Counts are metadata, not claims that every item fits any one model request. None of that populated thread context was restored into the deployed GPT-Live startup.

That session has **eight user events, four assistant events, empty compact state, zero delegation rows and zero structured calls during the session**. Retained bridge logs show the delegation listener ready, VAD speech boundaries and four finalized assistant turns, but no wire-delegation, application-delegation, boundary-ready or commentary-correlated result event. Bridge elapsed runtime was **49.153 seconds**, below the 90-second limit; that log's cost estimate is not billing evidence. There is no message status, `stopped_by` or refused-tool record to diagnose: **no background request was submitted**, rather than a submitted request failing its budget. Absence of retained wire events does not establish why the model never delegated.

### Playback path

At `75234dc`, `RealtimeVoice.tsx` constructs `new Room()`, appends each subscribed remote audio track's `track.attach()` element, and enables the default microphone. The locked and installed frontend SDK is **`livekit-client` 2.23.0**. Its `roomOptionDefaults.webAudioMix` is **false**: playback is through attached **HTML audio elements**, not an application-created AudioContext mixer. There is no output selection, sink ID or explicit audio-session category in this version. Microphone defaults request default input, echo cancellation, automatic gain control, noise suppression and voice isolation. These are capture constraints, not a Bluetooth output selector.

The voice models store neither user agent nor physical output route. The retained web logs contain 68 voice endpoint access lines, with no browser/user-agent tokens; the bridge logs do not identify the handset/browser or Bluetooth profile. **Phone model, OS/browser version, installed-PWA versus browser tab, headset model and whether it supports call audio/HFP remain unknown.** Ask Lee for those details and the phone's selected call/media output while the microphone is active. His reported phone-speaker playback is accepted evidence; it does not identify its cause.

`Room.switchActiveDevice('audiooutput', id)` is a real SDK API, not a hardware Bluetooth switch. With the default element playback it applies sinks to subscribed tracks; with `webAudioMix` it needs the AudioContext sink API as well. Switching to Web Audio therefore does not repair a platform without output selection. Retain the current element playback rather than introduce a second, less compatible path.

### Browser and Bluetooth restrictions

| Platform | Output selection | Consequence |
| --- | --- | --- |
| Desktop Chromium | `HTMLMediaElement.setSinkId` supported; chooser API not generally implemented | Offer only exposed/permitted `audiooutput` entries; await switching and read the applied sink. |
| Desktop Firefox 116+ | `setSinkId` and `selectAudioOutput` | Native chooser requires a user gesture; permission is not proof playback was heard. |
| Safari on macOS 18.4+ | Apple explicitly added Speaker Selection on **macOS** | Feature-detect; do not generalize desktop support to an iPhone. |
| iOS Safari / WebKit | No dependable phone speaker-selection API established by Apple's release notes | OS controls the route unless the actual runtime successfully applies a sink; give phone output-picker guidance, not a fake toggle. |
| Android Chrome | Current compatibility data marks element `setSinkId` unavailable; no chooser API | OS/call-audio routing; feature detection remains authoritative for future releases. `devicechange` support alone does not confer routing support. |

Sources: [MDN `setSinkId`](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/setSinkId), [output chooser](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/selectAudioOutput), [enumeration](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/enumerateDevices), [device changes](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/devicechange_event), [browser compatibility data](https://github.com/mdn/browser-compat-data/blob/main/api/HTMLMediaElement.json), [chooser compatibility](https://github.com/mdn/browser-compat-data/blob/main/api/MediaDevices.json), [Apple Safari 18.4 release notes](https://developer.apple.com/documentation/safari-release-notes/safari-18_4-release-notes), and [LiveKit 2.23.0 Room implementation](https://github.com/livekit/client-sdk-js/blob/v2.23.0/src/room/Room.ts). Compatibility data's Safari-iOS mirror is not independent proof of phone routing; Apple's release note explicitly scopes the addition to macOS.

The output APIs require HTTPS and may be blocked by the `speaker-selection` Permissions Policy. Enumeration can omit devices or labels without permission. A chooser grants permission, not playback; `setSinkId` can reject for permission, device loss or switching failure. Device IDs are browser-scoped, not stable hardware identities. `devicechange` needs re-enumeration; a missing device should fall back to system default without claiming that default means speakerphone.

Newer iOS behavior must not be reduced to an obsolete blanket “no `setSinkId`” claim: [WebKit report 320087](https://bugs.webkit.org/show_bug.cgi?id=320087) describes iPhone 15 / iOS 26.5 accepting a sink and updating `sinkId` while a single-track remote WebRTC stream stays on the old physical route. This is a vendor bug report, not our reproduction or Lee's identified device. The UI can truthfully confirm **browser-applied sink state**, not which speaker a human heard; retain OS guidance and a device acceptance gate even when the browser promise resolves. Do not duplicate remote tracks as an unverified workaround.

[Apple documents A2DP](https://developer.apple.com/documentation/avfaudio/avaudiosession/categoryoptions-swift.struct/allowbluetootha2dp) as stereo **output-only**, while HFP supports call input/output and takes precedence when both profiles are allowed. Microphone capture can cause a communication/`play-and-record` session, changing eligible routes and making a media-only A2DP connection insufficient. [WebKit report 295889](https://bugs.webkit.org/show_bug.cgi?id=295889) records microphone capture shifting playback to the earpiece and not restoring the speaker; it demonstrates a platform failure mode, **not Lee's device diagnosis**. The [Audio Session API](https://developer.mozilla.org/en-US/docs/Web/API/AudioSession/type) describes category intent, not a chosen physical sink. Turning off echo cancellation or forcing `playback` during duplex capture would risk feedback/capture regressions without proving a Bluetooth fix; no such change is justified by the retained evidence.

### Why it was not the orchestrator

`75234dc` isolates GPT-Live from the **launch thread's model context**, even though the durable voice session holds the correct chat foreign key. Startup supplies only voice compact state, at most 40 finalized voice events and already-answered voice delegations. On this phone test, those were initially empty. Per-user-turn silent context supplies server UTC and elapsed time for up to three user-reported activities, not project results.

In contrast, a normal bot turn uses `Bot._prepare` / `Bot.context_builder`, current bot/chat instructions, chat identity, skill instructions and authorized toolkits, configured context sources and recent-message policy, plus the engine's conversation history/compaction and attachments. None of the launch thread's bot instructions, recent normal messages, durable compaction, attachment references, task state or tool definitions was supplied to GPT-Live by the deployed startup path. It receives generic voice-interface instructions and **no callable voice tools** by design.

Tool availability, routing, context and behavior are separate defects/constraints:

- **Tools:** deployed budget allows only `ergo_chat_history_read` and `ergo_chat_history_search`, not bot/thread/worker/approval management. Existing budget guards prohibit writes, approvals, workers, sends and retries.
- **Routing:** client delegation would create a message in the **same launch chat**, not discover or switch to a different orchestrator. A resolved subscription/CLI engine cannot satisfy request limits; pinned `gpt-6-luna` remains required. No routing happened in the phone test.
- **Context:** voice's own empty restore state is not the populated launch thread. Even a successful read-only history lookup would not automatically carry the full orchestrator identity or current-work overview.
- **Model behavior:** no delegation was observed. Instructions cannot guarantee provider delegation or truthful speech; this test does not establish whether an authorized lookup would have helped.

The approved next slice inherits the exact launch thread, not a global owner briefing. Cross-thread status is on demand through that thread's already-authorized read-only tools. A safe future route for a subscription-backed launch thread is an explicitly reviewed budgeted API-engine profile reusing its identity/context and only its authorized read-only tool subset; silently replacing its engine or widening its permissions is not authorized. Forwarding to another owner thread is a write and remains disabled pending a separately reviewed authorization/approval design.

## Hands-free offline correction — no fourth attempt

Lee clarified that the live agent should choose delegation while the user speaks naturally; the mandatory typed confirmation at `4879414` was temporary and has been removed. Python wake-word detection was deferred and is not implemented. The model's client-delegation event remains the only trigger for a task; no fake tool is registered.

The pinned LiveKit 1.8.11 source shows that an explicitly supplied session VAD is passed to `AudioRecognition`, while `AgentActivity.push_audio` forwards the input frame both to realtime and recognition. The recognition VAD stream drives session user speech states; GPT-Live's own transcript-derived speech events are not treated as acoustic evidence. A no-room offline test used the actual `AgentSession`, `AudioInput`, recognition pipeline and Silero ONNX model with FFmpeg/flite synthetic PCM; the GPT-Live network task was parked. Speech and silence produced real speaking/listening transitions, and no model signal meant no task. This validates the local audio path, not the hosted microphone or network.

On the first valid client signal the bridge retains its ID and gathers ordered interim/final SDK fragments. A detected end of speech arms a configurable grace (default 2.0 s after Silero's 0.55 s silence threshold); resumed speech clears it. Dispatch additionally requires all observed fragments finalized and 0.8 s of transcript quiet, within a 60-second deadline. Timeout, a missing final, session end or bridge stop fail closed; duplicate signals do not create another task. A boundary or transcription can still miss later words, and a pause longer than the grace can be mistaken for completion. Neither acoustic silence, final text nor model instructions prove semantic completeness or spoken truthfulness.

Offline verification: 36 targeted backend cases passed, including the actual-SDK early delegation, internal pause, resumed speech, late transcript tail, duplicate signal, missing final, stop and correlated durable result; 34 library budget cases passed. The frontend build and 240 frontend tests passed, and changed-Python Ruff checks passed; React act and Vite chunk-size warnings remain. A local Chromium fixture displayed the real room and session card in the collecting state without a confirmation form, room connection or model. A durable result still precedes same-ID commentary as silent context, not as a guarantee of generated wording, playback or hearing.

The three historical attempts, roots, queued messages, one-shot markers and isolated SDK environment were preserved. The prototype remains disabled and not ready to merge or deploy; no new inference, room join, replay, shared restart or configuration/secret mutation was performed.


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

## Historical offline confirmation correction after the third attempt

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
5. Do not type a request or press confirmation. Verify that nothing was submitted while the speaker was still talking or resumed within the grace. Correlate: valid wire event → application signal → durable `delegation_signal` → one hands-free delegation containing the complete first sentence and late constraint → real Celery receipt → bounded structured call → actual allowed history search/read tool calls → seeded answer → same-session durable result → silent context and commentary using the SAME delegation ID. Record speech, VAD, submission and result timestamps and budget metadata; a missing constraint, early submission or spoken claim unsupported by durable work fails acceptance.
6. If no valid event arrives, no request must be auto-dispatched. The UI/history must show no submitted request/no verified result, and any generated “I checked” claim is a failed semantic acceptance even with the unverified label. Preserve evidence, stop at the cap and report upstream/model behavior as unresolved; do not spend another attempt automatically.
7. Stop only owned isolated processes; retain consumed markers and any queued messages, perform approved read-only room absence and billing checks, and report actual usage or its absence. Browser hearing/playback, interruption/reconnect and migration/retention remain separate gates; do not infer them from synthetic track receipt.

**Open behavior:** the hosted model's decision to emit delegation and its spoken truthfulness cannot be guaranteed by offline tests or instructions. This patch fixes the proven application-contract/status defects and adds evidence boundaries; it does not claim the second attempt's unknown wire/model cause is solved or authorize further spending.
