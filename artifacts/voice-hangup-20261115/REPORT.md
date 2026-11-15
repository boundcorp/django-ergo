# November 15 voice termination: sanitized investigation and diagnostic delivery

## Decision

Historical initiating cause is **UNKNOWN**. This branch adds close-cause observability; it is **not a demonstrated repair of the reported hangup**. No timer, provider reconnect, new socket, request cap, approval policy, infrastructure configuration or accepted-work cancellation is introduced.

The coordinator explicitly authorized narrow sanitized durable diagnostics after confirming that the persisted dispatch specification was itself truncated. Required release checkpoint remains before any image-triggering merge; main publication auto-promotes.

## Sanitized timeline (UTC)

Logical session prefix `77f4c16f`; supplied `84e13349…` identifies its bridge claim, not the logical session. Originating thread identity matched the task record; no spoken/request/result text is reproduced here.

| Time | Observed fact |
| --- | --- |
| 15:53:47.674 | Logical session created. |
| 15:53:50.252 | Initial bridge claimed. |
| 15:53:52.500 | Browser assistant-audio subscription observed. |
| 15:53:54.583 | Provider session start observed. |
| 15:54:11.276 | Native Responses delegation received. |
| 15:54:13.106 | Durable task created in the exact originating thread. |
| 15:54:14.239 | Task-status announcement claimed. |
| 15:54:31.052 | Durable completed-result announcement claimed; linked task is `answered`. |
| 15:54:32.980 | Last renewable bridge lease timestamp. |
| 15:54:37.459 | Last finalized assistant segment received, marked interrupted. |
| 15:54:38.872 | Logical session ended; standby remained `live`. |
| 15:54:38.876 | Bridge summary: 47.037 wall seconds, provider usage 45.0 seconds. |

Created-to-ended interval is approximately 51.198 seconds, not a measured 60–90 seconds. This timing refinement does not contradict the owner's report of unexpected automatic termination mid-sentence.

No End or Standby POST appears in the inspected complete API log window. Provider error-stage evidence and provider/SDK exception logs were absent. This does **not** establish which actor disconnected first: old provider/room/AgentSession callbacks discarded their reasons, and usage/interruption may have been produced by teardown. Browser page departure, OS/network loss, room lifecycle, provider close and SDK shutdown cannot be distinguished retrospectively from the retained data.

## Exact served runtime

- Source: `2a29a3d769e77a22f751e9ed0daeb0455d93f16d` (web and bridge); same current main baseline.
- All seven application roles ready on image `sha256:0df7706c6e2a53a18c394fbf3e25ada2af83613d09ac459a42c2c71e2cffe6ba`; web 2/2, bridge 1/1.
- Bridge replica set created 05:27:02Z, with no container restarts; no contemporaneous rollout established. No Kubernetes pod events retained in the namespace at inspection.
- Agents/OpenAI plugin `1.8.11`, RTC `1.2.0`; exact served GPT-Live engine, AgentSession and RoomIO bytes match the isolated verification environment.
- Engine SHA256 `eca187493acd43ea5e5e6c3c1fb8c305efbc841de303271a4dfae76677b101f5`.
- AgentSession SHA256 `27f8860066556f4d1f2ae1d61e1c84666cbbade821ddcf8dd8a17b7303f95e17`.
- RoomIO SHA256 `2ea9d87c4a5df47cbd5d1ad3118980d9ba2047624002c5c315951bfae5366558`.
- Provider host is `api.openai.com`, no Azure override, model `gpt-live-1`, same-connection `gpt-5.6-luna` Responses delegation, default backend tier, reasoning none, 1,024 output tokens. No production one-shot cap file configured.
- `max_session_duration=None`, no SDK retry, one application socket guard. Renewable 120-second lease every five seconds; last lease was about six seconds old at termination.
- AgentSession `user_away_timeout=15` only changes state; its five-second tool-reply wait warns, not closes. SDK shutdown-drain five seconds is not a call lifetime.
- Browser token TTL is five minutes, not this observed lifetime. No app-level 45/60/90-second cutoff found. Mobile/browser background behavior is not proven by source inspection.
- Voice owner is mounted above routing. Navigation changes polling cadence (750 ms foreground voice page / 3 seconds otherwise), not transport ownership. Thread completion does not itself hang up.

No numerical provider lifetime is inferred. Prior PR #335 removed application limits and deployed as `b8053d0`; PR #321 was label-only. Unlimited intentional requests are separate from transport lifetime.

## New evidence contract

`RealtimeVoiceEvent.kind=lifecycle` stores allowlisted boundary/source/reason, server `observed_at`, elapsed seconds, bridge correlation where known, teardown marker, optional numeric WebSocket code and known exception class/recoverability. No audio, transcript, provider payload, exception message, credential or device identifier is retained.

- Provider start, outbound close, inbound close reason, wire close/error, SDK close/error, owner/room disconnect and bridge-loop exit are distinguishable.
- Explicit End, bridge termination and expired abandoned-worker leases record their application transition.
- First observation per bridge boundary wins; stale replaced claims cannot append. Post-End teardown can still be retained for the accepted claim.
- Browser disconnect/visibility/pagehide facts use authenticated same-origin/CSRF keepalive requests. Browser reports have **unknown bridge-claim attribution**, because an issued request can race resume.
- At most 39 non-disconnect browser observations reserve slot 40 for disconnect; owner reports may arrive for 120 seconds after End. These are evidence retention bounds, not admission/lifetime limits.
- `CLIENT_INITIATED` is an SDK reason, never a claim that the user clicked End.
- Diagnostic pool-thread writes cannot occupy the turn/End database lane. PostgreSQL writes have savepoint-local lock/statement timeouts, restored before returning; terminal persistence precedes a bounded one-second diagnostic drain. Diagnostic storage failures do not stop End.

## Exercised verification

- Before-change actual `Assistant.on_protocol_event(session.closed(reason=expired))` smoke retained only usage, no close fields or diagnostic jobs: reproduced the evidence gap without provider access.
- Actual recorder against disposable PostgreSQL retained a sanitized provider-close event at virtual elapsed 601 seconds, rejected a stale claim, and wrote zero originating-thread transcript messages.
- Actual pinned GPT-Live constructor/receive/send loop, duplex adapter, AgentSession and `voice_session` orchestration smoke: **8 passed in 217.11 seconds**. One connection stayed healthy for **180.02 real seconds**, then the same logical bridge passed **virtual 601-second** lease observations. Native tool continuation and a durable result announcement occurred; a second accepted task answered after End. Provider sockets: **1**, reconnects: **0**.
- Separate explicit End, room, owner, SDK, provider and wire-close scenarios retained distinguishable causes/teardown. A deliberately stalled diagnostic writer did not prevent logical End or worker shutdown. Privacy, stale-claim, duplicate, post-End and browser-bound checks passed. `SMOKE.json` preserves bounded sanitized outcomes.
- Smoke transport was an in-memory fake WebSocket; RoomIO/VAD were omitted. PostgreSQL and the Kombu memory broker were disposable. This is not real provider/device/media, Redis or production Celery-worker proof, and does not attribute the historical incident.
- Isolated voice regression suite: **149 passed, 1 skipped**, 130.06 seconds. Skip is the separately configured Redis bridge broker integration; its environment variables were deliberately absent locally.
- Frontend component suite: **49 passed**, zero failures; TypeScript project check passed.
- Python formatter/linter passed on the affected Python files.
- Actual Chromium mobile-width rendering of `VoicePage` showed the lifecycle JSON under existing Diagnostics, with zero browser error entries. Synthetic session only; no Start, token, provider, microphone or device capture. Screenshot: `/tmp/omp-sshots-15cf4e3a3636f77d.webp`.
- Independent lifecycle and privacy source reviews found real issues in the first draft (unbounded diagnostic shutdown drain, resume claim misattribution, unload delivery). All were corrected and both rereviews returned no remaining findings.

## Evidence privacy and operational actions

Authoritative environment map was read first and `envcheck` succeeded on documented routes. Production interaction was read-only: metadata, settings names/safe values, exact deployed SDK bytes and bounded logs. Raw bridge/API logs are private under mode-0700 directory `/home/dev/.local/share/voice-hangup-20261115-task_6658c8e84051/`; they are not committed.

No paid inference, real provider test, room creation, device/browser capture, owner task, production migration/config write, deployment/restart, main merge, accepted-work cancellation or RunPod mutation was performed. Local fixture browser/service stopped and throwaway frontend entries removed.
