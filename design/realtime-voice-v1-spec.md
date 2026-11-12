# Ergonaut realtime voice — v1 specification

Status: proposed implementation spec, November 10, 2026. No implementation or deployment authorized by this document alone.

## Current isolated first-slice contract

The original target below describes a later product, not the scope authorized
for the current branch. This slice is feature-disabled by default and uses a
minimal Stage; wake-word, standby and a general Stage are deferred. No shared
deployment, production migration or paid provider call is authorized by this
document. The bridge uses the separately installed LiveKit 1.8.11 GPT-Live
adapter because its OpenAI SDK 3 requirement conflicts with Ergo's SDK <3.

Django is authoritative for a logical session across reconnects. Persist
finalized turns, compact decisions, constraints, current step, task references
and user-reported activity anchors independently of the bounded transcript
restore window. A completion remains recoverable until acknowledged; duplicate
delegation IDs must not cause duplicate work. A playback acknowledgment is
evidence of a client event, not evidence that a person physically heard audio.
Unknown and interrupted output must remain distinguishable from generated text.
Chat has one card per logical session, not a chat message for each utterance.

Use UTC server receipt and processing/event timestamps, separately from any
client speech-event timestamp. Client timestamps can have skew and are not
authoritative without clock synchronization and an uncertainty bound. An
activity anchor records what the user reported, not proof the physical activity
occurred. Expose server current time and calculate elapsed time deterministically
from a recorded anchor; never invent missing timestamps, recipe durations,
timers or reminders. The cooking regression must retain the reported creamy
gochujang coconut chicken, boneless skinless thighs, missing gochugaru, MSG,
lemongrass and lime leaves, completed 30-minute marinade and ready-to-pan-fry
state through unrelated conversation, delegation and reconnect.

The normal background queue cannot be treated as a cost-capped read-only live
harness merely because the voice connection is capped. A real test requires
an enforced nonmutating tool allowlist, bounded input and output, a deadline,
no recursive workers or automatic retries, exact configured model prices and
a defensible maximum total cost calculated before any paid call. Until then,
mock results are not live proof.

## 1. Goal

Talk naturally with Ergonaut while its existing bots perform background work. Voice is the core experience, not dictation pasted into chat. A visual Stage shows useful shared state; the conversation explains and controls it.

### Decisions agreed with Lee

- Hosted LiveKit for v1; no self-hosted WebRTC/TURN infrastructure.
- OpenAI GPT-Live-1 for realtime conversation, using client delegation to existing Ergonaut orchestration.
- Python/backend wake-word detection is acceptable; browser audio must therefore reach the backend while standby is armed.
- A Django-backed session log and one summary card in chat, not a pending chat message per utterance.
- Browser/mobile-web first. Android and watch support are future work, not v1 prerequisites.
- No alternate STT-only mode in v1.

## 2. User experience

Start a voice session from a chat. Explicitly grant microphone access and start audio playback through a user gesture. Show joining, connected, reconnecting and ended states.

Use Design's mobile Voice Room mockups as the visual target: title and participant status; large interactive Stage; compact live caption strip opening a history sheet; bottom typed-input, microphone and bot-pause controls. Overview and drill-in are two views of the same state, reachable by speech or touch.

Initial Stage scope: a small allowlisted overview of active work, reviews/approvals and blockers for the current authorized context. A topic drill-in shows related work and links. Do not expand into a general dashboard builder or arbitrary model-generated UI.

Examples:
- “Show reviews” opens the same Stage view as tapping Reviews.
- “Research that in the background” creates durable delegated work; conversation remains responsive.
- “Any update?” retrieves verified task state, not a model guess.
- “Stop listening” enters wake-word standby after acknowledgement.
- A configured wake phrase returns to live conversation with an audible/visual cue.

Typed input in the room joins the session conversation and transcript; it does not independently trigger a normal chat bot turn. After ending the session, regular chat input works as usual.

## 3. Architecture

Browser microphone/speaker and UI ↔ hosted LiveKit room ↔ Python voice bridge ↔ GPT-Live.

Python voice bridge ↔ authenticated Ergonaut session/delegation API ↔ existing bot orchestration and tools.

Django owns session identity, authorization, history, task links and durable UI state. LiveKit carries media and transient control/text events; it is not the history database. The exact LiveKit/GPT-Live adapter and supported event semantics must be verified in the first technical spike, not assumed interchangeable with legacy Realtime adapters.

Use client delegation so existing Ergonaut agents retain responsibility for reasoning, detailed business rules and tools. Avoid a second competing OpenAI-managed backend agent.

Keep the voice bridge in a separately managed long-lived process/service, not an HTTP request or an occupied long-running chat turn. Deploy placement is an implementation decision after validating SDK/runtime requirements.

### Responsibility boundaries

- GPT-Live: natural spoken interaction, clarifications, concise narration, delegation intent.
- Voice bridge: provider connections, audio routing, wake detection, lifecycle and event translation.
- Django: authoritative session state, permissions, durable transcript/events, idempotency and usage ledger.
- Existing bots: background reasoning, tools, durable task execution and verified results.
- Browser: capture/playback, visible privacy state, Stage rendering and reconnect UX.

## 4. Audio and attention state

Mic state and bot speech state are independent.

| Mic state | Capture | Backend detector | GPT-Live input |
|---|---|---|---|
| Live | On | Optional | On |
| Wake-word standby | On | On | Off |
| Mic off | Stopped | Off | Off |

Standby is explicitly labeled “Listening for wake phrase,” never presented as hardware/private mute. Audio leaves the device. Standby audio is transient: no persisted recording, transcription or model submission. Hard mic-off stops local capture, not merely network publication; only a tap can reactivate it.

The bot may request Live → Standby through an authorized control action. A model/tool may not override Mic off or unilaterally unmute a user. Wake activation requires the user to have opted into standby. Initial implementation controls only the initiating user's microphone stream.

On standby, stop forwarding audio to GPT-Live immediately. Close the paid GPT-Live connection after a configurable short grace period; reopen on wake and restore bounded context. Benchmark the grace period and reconnection latency before choosing defaults. Keep one logical application session across multiple provider connections.

Play an activation cue and show readiness; v1 may require users to wait for that cue before speaking their request. Do not upload pre-wake audio as a workaround for clipped speech. Test detector cooldown, echo cancellation and bot-playback false activations.

Bot pause stops current speech and suppresses future spoken output until resumed. It does not stop background work, captions, Stage updates or task persistence. No automatic unpause or queued-message dump.

Browser suspension, lock screen and permission loss must be shown truthfully. Do not claim continuous wake detection when capture is interrupted. Reliable background/locked-screen operation is not a v1 guarantee.

## 5. Delegation and background events

Do not send each utterance through the normal incoming-chat-message/pending-turn pipeline. GPT-Live maintains its conversational context directly.

Explicit delegated work becomes a durable request linked to the voice session and, where appropriate, an existing/new task thread. Record request ID, author, authorized scope, input, status and result references. Preserve existing turn serialization within each destination conversation; do not bypass the orchestration queue.

Delegation lifecycle: requested → accepted/queued → running → completed/failed/cancelled. Duplicate events and reconnects must not execute work twice. Interrupted speech does not imply task cancellation. Cancellation is explicit and only offered where the task supports it.

Background results update shared state immediately, but speech follows an attention policy:
- Routine progress: silent Stage update.
- Useful completion: brief mention at a conversational gap.
- User-action blocker: visible emphasis and concise announcement when speech is enabled.
- Interruptions: only for narrowly defined urgent events, not arbitrary model judgment.

All spoken claims about actions should derive from verified backend outcomes. Permissions and required approvals remain enforced server-side, including actions invoked by voice.

## 6. Durable data model

Names are conceptual; adapt to actual repository conventions during implementation.

### VoiceSession

UUID; linked chat; initiating user; participant memberships; lifecycle status; started/ended timestamps; finalization status; title; summary; decisions; open items; Stage snapshot/version. Save partial state during the call. Store no provider secrets here.

### VoiceSessionConnection

Links one logical session to each LiveKit/provider connection: opaque external identifiers, start/end reason and time, provider/model, billable duration and usage reconciliation status. Supports standby reconnection without creating duplicate chat cards.

### VoiceSessionTurn

Session ID, stable turn ID/sequence, participant or assistant identity, spoken/typed modality, timestamps, finalized text, interrupted/completion state and delivery status. Keep interim captions ephemeral. Preserve distinction between generated output and audio actually played; if exact playback delivery cannot be proven, mark it unknown instead of claiming the whole response was heard.

### VoiceSessionEvent

Ordered, versioned event envelope with event ID, timestamp, actor, type and bounded payload. Events include delegation status, task result references, Stage changes and audio-state transitions. Deduplicate retried events. Do not store raw audio frames, credentials or complete sensitive tool payloads in event logs.

### Chat session card

One chat-visible session reference, rendered as “Voice session in progress” while active. On completion, show summary, decisions, outstanding work and transcript link. The card must not trigger the ordinary bot-message turn runner. Ending/crashing must not leave it permanently in progress; run stale-session reconciliation.

Summaries are derived artifacts, retriable without duplicating the card or rerunning tools. They must distinguish requests, confirmed actions and unfinished work. Store summary model usage separately.

## 7. Interfaces and security

Provide authenticated operations to create/join/end/read sessions, obtain short-lived room-scoped LiveKit tokens, retrieve paginated transcript/events, and submit allowed session controls. Define the concrete REST routes against repository conventions in implementation.

Validate chat access and participant membership for every operation. Scope room tokens to a single room and required media permissions. Keep OpenAI and LiveKit signing keys server-side. Rate-limit joins and session creation; bound concurrent sessions and provider retries.

Prototype status: room tokens are scoped to the owning user's session and microphone publishing; the browser cannot write events, state, or delegation records. This does not yet provide join rate limits, session concurrency limits, or an independently verified hosted identity/transport boundary.

Until a playback-correlated protocol is implemented, a durable completion is visible in the session card and is claimed for at most one spoken commentary attempt. This avoids deterministic reconnect loops at the cost of possibly losing an announcement after a crash. A claim is not playback confirmation, and playback confirmation would not prove human hearing.

Use versioned control events with IDs and acknowledgements. Reconnect using authoritative state/version plus missing durable events; transient LiveKit delivery alone is insufficient for correctness.

Prefer a single initiating human plus Ergonaut for the first vertical slice. Design identity and transcript schemas for multiple participants, but defer general multi-human conversation management, diarization of people sharing one mic, and speaker-specific voice permissions.

Recording is off by default. The transcript is stored under existing chat authorization. Deleting or restricting a chat must also cover linked session history according to application retention policy. Define retention for provider logs and operational traces before enabling production use.

## 8. Costs and controls

Verified public pricing on November 10, 2026:
- GPT-Live-1: $0.05 per connected minute ($3/hour), billed per second. Silence, backend waits and muted-but-open sessions count. Backend models/tools are additional.
- WebRTC session initialization bills 15 seconds credited against running duration; do not add 15 seconds again to normal session estimates. Failed/repeated creations need accounting.
- LiveKit Build: $0/month, 1,000 hosted-agent minutes and 5,000 WebRTC user minutes included. Ship starts at $50/month, with 5,000 hosted-agent minutes then $0.01/minute; transport, bandwidth and optional observability/recording have independent allowances. Hosting the bridge ourselves versus on LiveKit affects which charges apply.

For 30 minutes/day over 30 days, GPT-Live alone is $45/month. This is not a total-stack quote. Wake standby should keep GPT-Live closed; backend compute and room transport may still accrue.

Track logical session duration separately from paid voice duration, provider initialization/retry costs, background token/tool usage, LiveKit usage and summary generation. Record cumulative provider usage as snapshots, not additive deltas; reconcile final usage on graceful close. Avoid double-counting inference charged through a reseller and direct provider.

Implement configurable maximum paid-session duration, inactivity close, per-user concurrency, daily/monthly application budgets and alerts. Budget enforcement must be application-side, not just provider-dashboard notifications. Warn before cutoff and retain transcript/task state. No raw-audio recording or extra transcription service by default; first validate whether GPT-Live transcript events meet the history requirement.

## 9. Delivery plan

### A. Compatibility and experience spike

Verify hosted LiveKit ↔ Python bridge ↔ GPT-Live-1 with client delegation; audio, interruption, transcript events, token issuance and graceful close. Exercise one background request without blocking conversation. Measure first-response latency, reconnection latency and billed duration. Validate Python wake detector integration and assess a custom wake phrase's model/runtime licensing. Produce findings before committing to an adapter.

### B. Minimal usable voice room

Implement persisted sessions/turns/events and a single chat card; minimal Stage overview/drill-in; captions/history; typed input; speech pause; Live/Standby/Mic-off controls; background request and result routing. Start with one human and one assistant.

### C. Reliability and controlled rollout

Reconnect/deduplication, interrupted transcript handling, stale-session finalization, spend limits, authorization tests and mobile-browser QA. Roll out behind a feature flag to Lee first. Measure actual daily cost and task usefulness before broadening scope.

Ergonaut is load-bearing infrastructure. No devbox restart. Durable changes go through git/CI; database migrations, configuration/secrets and shared-service changes need an explicit rollout/rollback plan and applicable approvals. Do not restart shared services casually during the spike.

## 10. Acceptance criteria

- Speak and receive natural interruptible audio through hosted LiveKit and GPT-Live.
- Delegate a real background task while conversation continues; its result updates the Stage and is spoken according to attention state.
- No normal chat message/pending turn for each spoken utterance; exactly one session card with summary and browsable transcript.
- Voice and touch navigate the same authoritative Stage state.
- Standby forwards no audio to GPT-Live or transcription, stores no ambient audio, and wakes through the Python detector.
- Mic off stops capture and cannot be overridden by a model/tool or wake event.
- Bot pause stops speech without stopping work; a result arriving while paused is not spoken.
- Reconnection neither duplicates tasks/cards nor loses finalized persisted turns. Lost/unconfirmed audio is represented honestly.
- Unauthorized room joins, transcript reads and tool requests are rejected.
- Provider disconnects and budget cutoffs leave recoverable history and running-task references.
- Test desktop Chrome and Android Chrome foreground operation, permission denial/revocation, headphones and speaker playback, network switching and screen/background transitions. Document Safari/iOS behavior if included in rollout; no untested background guarantee.
- Capture representative latency, interruption success, wake false-accept/reject rates and per-session cost before declaring readiness. Set numeric performance targets from the spike, not invented SDK assumptions.

## 11. Explicitly out of scope

Self-hosted LiveKit; alternate STT-only pipeline; full Android or Wear OS apps; guaranteed always-on mobile background listening; general multi-human room moderation; automatic speaker identification from shared microphones; raw audio recording/export; arbitrary model-authored Stage UI; replacement of existing bot/task orchestration.

## References

- OpenAI GPT-Live architecture/client delegation: https://developers.openai.com/api/docs/guides/live
- OpenAI rates: https://developers.openai.com/api/docs/pricing
- Duration accounting and idle close: https://developers.openai.com/api/docs/guides/voice-latency-cost
- LiveKit pricing: https://livekit.com/pricing
- LiveKit data transport: https://docs.livekit.io/transport/data/
- LiveKit Python wake-word toolkit: https://docs.livekit.io/agents/multimodality/audio/wakeword/
- Visual targets: Design's Ergonaut-Mobile-Voice-Room.png, Ergonaut-Mobile-Voice-Room-Drill-in.png and Ergonaut-Mobile-Voice-Room-History.png from the mobile voice-room design thread.
