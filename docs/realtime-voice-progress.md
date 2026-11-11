# Realtime voice progress

## Current branch

The off-by-default prototype uses Django-owned logical sessions, finalized events, conservative extractive compact state, reported-activity anchors, stable delegation IDs, a durable completion outbox, a single chat card per voice session and a minimal Stage. Recent events and restored anchors are bounded; persistent records remain in Django. User speech times supplied by a client are separate and have unknown clock error. Elapsed time is calculated since server receipt of the user's report, not since a verified physical action. The rules cannot semantically summarize arbitrary phrasing, and the model's actual live use of silent time context remains unverified.

The completion outbox does not consider sending commentary to be playback; unconfirmed results retry on reconnect and can repeat. The current integration has no correlated browser playback acknowledgment, and cannot establish what anyone heard. Normal bot permissions and confirmation remain in force. One background request per local voice session is not a total delegated-work budget.

## Adapter and setup

The bridge pins `livekit-agents`, `livekit-plugins-openai`, and `livekit-plugins-silero` at **1.8.11** and uses the distinct `GPTLiveModel(model="gpt-live-1", delegation="client")`. Its OpenAI SDK 3 dependency conflicts with this repository's SDK <3, so the bridge environment is isolated. See [the released adapter source](https://github.com/livekit/agents/blob/livekit-agents%401.8.11/livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/realtime/gpt_live_model.py) and [LiveKit GPT-Live guide](https://docs.livekit.io/agents/models/realtime/gpt-live.md). Documentation and local imports do not establish hosted entitlement.

Prior setup checks: `envcheck` reported Octo and home Kubernetes APIs ready and documented routes to rigel, macaroni, vega and enterprise reachable. LiveKit URL/key/secret were absent in the local environment and Octo Ergonaut secret key names; OpenAI key presence was observed without printing values. Lee is signing in to LiveKit on Rigel; hosted project access remains to be established. No paid calls, purchases, deploys, shared restarts, secret changes or production migrations were performed.

## Local evidence

- Backend: `cd ergonaut && PYTHONPATH=.:../src /tmp/ergo-voice-test/bin/pytest ergonaut/api/tests/test_realtime_voice.py ergonaut/api/tests/test_realtime_voice_outbox.py -q --disable-warnings` — 11 passed, including the cooking/elapsed/compaction/delegation/reconnect scenario; all inference and dispatch were local/mocked.
- Isolated bridge timing smoke invoked the actual turn-persistence method with mocked storage/time and observed a 180-second elapsed-since-report context; it opened no provider connection.
- Frontend: `cd ergonaut/frontend && bun test src/components/RealtimeVoice.test.tsx && bun run build` — 4 passed and build succeeded; Vite reported a chunk larger than 500 kB.
- `cd ergonaut && PYTHONPATH=.:../src DJANGO_SETTINGS_MODULE=ergonaut.settings.test_settings /tmp/ergo-voice-test/bin/python manage.py makemigrations --check --dry-run` — no changes detected.
- Chromium rendered the real `VoiceSessionCard` component with a local fixture: Stage, decisions, constraints and expandable transcript were visible. This was not hosted audio or a full authenticated UI journey.
- No actual first-audio, interrupt, reconnect or delegated-result latency was measured. Mock/local test timings are not hosted service latency.

## Remaining gates and rollout

No live audio, true browser playback acknowledgment, real delegated bot result, provider billing or total delegated spend cap has been demonstrated. The normal bot path can use workers and multiple model turns; the private-pool ledger does not bound the entire voice-request graph. A bounded real harness requires an enforced read-only allowlist, input/output and deadline caps, no recursive workers or retries, and an upper cost calculated from the exact configured provider prices before a paid call. GPT-Live's published voice rate is $0.05/minute ($0.075 for 90 seconds), excluding LiveKit and delegated work. See [realtime-voice.md](realtime-voice.md) for setup, blockers and rollout/rollback guardrails.

Before any shared rollout: establish project credentials and model entitlement without exposing secrets; review the schema migration and retention plan; build and validate enforced spend controls; approve a capped isolated hosted test; measure actual latency, interruptions, reconnect and billing; then separately approve deployment and migration. Rollback disables the flag and stops only the isolated bridge; persisted history and already queued work require explicit handling.
