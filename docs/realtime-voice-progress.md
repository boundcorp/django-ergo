# Realtime voice progress

## Current branch

The off-by-default prototype uses Django-owned logical sessions, finalized events, bounded extractive compact state and reported-activity anchors, stable delegation IDs, a durable completion outbox, a single chat card per voice session and a minimal Stage. User speech times supplied by a client are separate and have unknown clock error. Elapsed time is calculated since server receipt of the user's report, not since a verified physical action. The rules are not a semantic summarizer; unmatched statements can fall outside the bounded transcript window. The model's actual live use of silent time context remains unverified.

Browser callers can create, read, end and obtain room-scoped microphone-only tokens for their own voice sessions, but cannot write events, state or delegations. Background delegation is separately disabled by default: the ordinary Ergonaut queue has neither a one-turn/read-only guarantee nor a total delegated-work spend bound. The test-only settings override enables that unbounded queue and must not be used for paid testing. A locked durable claim allows at most one commentary attempt per result across connections; a crash after claim may lose that announcement, but the linked result remains visible. The claim is not evidence of playback or hearing. The inspected GPT-Live adapter's `append_commentary` returns no speech identifier for correlating an announcement to browser playback. Model behavior on restored context is not deterministic.

## Adapter and setup

The bridge pins `livekit-agents`, `livekit-plugins-openai`, and `livekit-plugins-silero` at **1.8.11** and uses the distinct `GPTLiveModel(model="gpt-live-1", delegation="client")`. Its OpenAI SDK 3 dependency conflicts with this repository's SDK <3, so the bridge environment is isolated. See [the released adapter source](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/realtime/gpt_live_model.py) and [LiveKit GPT-Live guide](https://docs.livekit.io/agents/models/realtime/gpt-live.md). Documentation and local imports do not establish hosted entitlement.

Prior setup checks: `envcheck` reported Octo and home Kubernetes APIs ready and documented routes to rigel, macaroni, vega and enterprise reachable. LiveKit URL/key/secret were absent in the local environment and Octo Ergonaut secret key names; OpenAI key presence was observed without printing values. Lee is signing in to LiveKit on Rigel; hosted project access remains to be established. No paid calls, purchases, deploys, shared restarts, secret changes or production migrations were performed.

## Local evidence

- Backend: `cd ergonaut && PYTHONPATH=.:../src /tmp/ergo-voice-test/bin/pytest -q --disable-warnings` — 264 passed, 5 skipped. Isolated voice tests cover scoped tokens, rejected browser writes, disabled-by-default delegation, concurrent deduplication, result claims, and cooking context across 105 unrelated turns; no provider inference or live dispatch occurs.
- Frontend: `cd ergonaut/frontend && bun test` — 233 passed; React emitted existing act warnings. `bun run build` succeeded; Vite reported a chunk larger than 500 kB.
- `cd ergonaut && PYTHONPATH=.:../src DJANGO_SETTINGS_MODULE=ergonaut.settings.test_settings /tmp/ergo-voice-test/bin/python manage.py makemigrations --check --dry-run` — no changes detected.
- Chromium rendered the actual `VoiceSessionCard` component with a local cooking fixture: current flip, ingredients, missing ingredients and completed marinade appeared. This was not hosted audio or a full authenticated UI journey.
- No actual first-audio, interrupt, reconnect or delegated-result latency was measured. Mock/local timings are not hosted service latency.

## Remaining gates and rollout

No live audio, browser playback acknowledgment, real delegated bot result, provider billing or total delegated spend cap has been demonstrated. The normal bot path can use workers and multiple model turns; the private-pool ledger does not bound the entire voice-request graph. A bounded real harness requires an enforced read-only allowlist, input/output and deadline caps, no recursive workers or retries, and an upper cost calculated from exact configured provider prices before a paid call. [OpenAI's published rate](https://developers.openai.com/api/docs/models/gpt-live-1) is $0.05/minute billed per second: $0.075 for 90 seconds, excluding LiveKit and delegated work. See [realtime-voice.md](realtime-voice.md) for setup, blockers and rollout/rollback guardrails.

Before any shared rollout: establish project credentials and model entitlement without exposing secrets; review the schema migration and retention plan; build and validate enforced spend controls; approve a capped isolated hosted test; measure actual latency, interruptions, reconnect and billing; then separately approve deployment and migration. Rollback disables the flag and stops only the isolated bridge; persisted history and already queued work require explicit handling.
