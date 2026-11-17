# Selectable text-agent voice pipeline

Checkpoint: November 17, 2026; baseline `7433edf`. Draft source only; no merge, deployment, media spending or physical-device test authorized here.

## Existing capabilities and access

| Path | Actual implementation | Access and costs |
| --- | --- | --- |
| Native live voice (default) | LiveKit Agents/OpenAI/Silero `1.8.11`, OpenAI SDK `3.49.0`; `GPTLiveModel(gpt-live-1, delegation="responses")`, managed interpreter `gpt-5.6-luna` | Existing realtime kill switch/account allowlist, LiveKit room credentials and OpenAI key; exact originating owner/chat/bot normal-task grant. Native duration/interpreter costs do not authorize separate STT/TTS spending. |
| Composer dictation | Existing OpenAI `openai_transcription` helper; configured `VOICE_TRANSCRIPTION_MODEL`, default `gpt-transcribe`; usage-only cost recording | Existing key, opt-in transcription flag, public endpoint checks, per-user transcription limits. Stop leaves editable draft without sending; Send submits once through the normal composer handler. Unchanged. |
| Standalone speech output | No existing application integration or authorization found | Pinned LiveKit OpenAI plugin supplies a TTS adapter, but installed capability is not paid-use authorization. Proposed reuse of the existing key requires explicit model/voice and a distinct reviewed exact-scope media-cost grant; no new provider/key provisioning. |
| Text assistant | Existing originating-thread bot/engine, messaging queue, tools, ACLs and approval UI | Retain normal costs, serialized turns, context, root/child identities and asynchronous task lifetime. Voice is not action approval. |

No protocol fallback: missing alternate media configuration denies alternate Start instead of silently using GPT-Live, changing models, creating keys or using browser speech APIs. Alternate selection is explicit before connecting and fixed for the logical session, including manual resume. Native remains default; choosing an alternate call must never mutate the native interpreter grant.

## Automatic turns and durable identity

Use the separate pipeline `owner microphone -> Silero-finalized audio utterance -> existing STT helper -> durably identified normal originating-thread request -> exact verified result -> TTS`. No LLM runs inside this voice adapter. The originating bot does all interpretation, conversation and tool use.

Assign each finalized audio utterance its own opaque identity before transcription. Bind that identity to the logical session and current bridge claim. Only that utterance's completed STT result can create its normal request. Interim captions, provider-native captions, transcript similarity, silence timers and neighboring speaker segments are never deduplication authority. Same-identity/same-text replay returns the original receipt; changed text conflicts rather than starting more work. A stale claim, End, failed STT or revoked authorization cannot admit new work. Preserve replay authority beyond bounded restore windows.

Admission and End serialize on the session lock. Once accepted, normal task execution survives voice interruption, disconnect and End. Distinct finalized turns use the existing thread queue, not concurrent model calls. Bounded in-flight work may exert visible backpressure; no cumulative call/request cap or fixed maximum call duration is added.

## Interruption, results and echo

User speech onset stops current and queued spoken output, not accepted backend work. Interrupted/unknown output is never relabeled heard. New finalized speech queues a new normal request automatically; there is no mandatory per-turn Send. Results remain durably accessible in the original thread/session even when they arrive after End or during another utterance. No automatic repeat of previously claimed speech on manual resume.

Every spoken outcome retains exact root request and child task identity. Queued/delivered transitions stay silent. Approval waits use existing approval controls; voice cannot approve them. Failed tasks use safe failure text, never invented success. Late callbacks from a replaced bridge cannot publish speech or dispatch work.

Only the initiating owner's microphone track enters STT, never room mixed output or the assistant's output track. Browser acoustic echo cancellation is explicit. This avoids digital feedback wiring; physical speaker echo rejection and human turn-taking quality remain unverified without separately authorized device tests. No claim of perfect acoustic echo rejection follows from offline fixtures.

## Verification and reversibility

Offline actual installed SDK/audio stream, disposable database and isolated broker fixtures: final/duplicate/late speech, identity conflicts, interruption and concurrent utterances, exact original context/tool/approval behavior, failures, End/disconnect, stale result handling and mode isolation. Browser proof exercises the actual mode selector without capture/provider/room connection. Native and composer regressions remain separate. Exact-head independent review and CI precede a draft PR; merge/deploy remain blocked on coordinator release checkpoint and fresh quiet notice.

Rollback is disabling the alternate gate and reverting draft source, never cancelling existing accepted normal tasks or rewriting native session policy. No shared runtime configuration or migration is performed by this draft.
