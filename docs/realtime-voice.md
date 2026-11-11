# Realtime voice prototype

This is an **off-by-default local prototype, not deployment-ready**. It is separate from composer dictation. Wake-word and a general Stage are out of scope. Django holds session state, finalized text and task references; LiveKit carries audio, not durable memory. No audio recordings are stored by this code.

## Adapter and setup

The pinned direct adapter is `livekit-agents==1.8.11`, `livekit-plugins-openai==1.8.11` and `livekit-plugins-silero==1.8.11`; it uses `GPTLiveModel(model="gpt-live-1", delegation="client")`, not the older Realtime adapter or a dictation pipeline. [LiveKit](https://docs.livekit.io/agents/models/realtime/gpt-live.md) and [OpenAI](https://developers.openai.com/api/docs/guides/live) document this combination. The adapter's OpenAI SDK 3.x requirement conflicts with Ergo's `<3` dependency; use a separate bridge environment with the Ergonaut source on `PYTHONPATH`, not a production dependency upgrade. `livekit-api==1.2.2` provides Ergonaut's room token signer.

Example isolated bridge setup from the repository root (Python 3.12 and `uv` required):

```sh
uv venv --python 3.12 /tmp/ergo-voice-bridge
uv pip install --python /tmp/ergo-voice-bridge/bin/python -r ergonaut/realtime-voice-requirements.txt \
  django django-ninja django-environ django-cors-headers django-ninja-jwt django-extensions \
  django-filter django-storages celery pgserver 'psycopg[binary]' pgvector uvicorn whitenoise \
  pydantic pytz requests prometheus-client anthropic pyyaml jinja2 markdown-it-py pillow
```

These are isolated development dependencies, not a lockfile for deployment. The Ergonaut and Django Ergo source directories are supplied on `PYTHONPATH` in the command below.

| Requirement | Current inspection | Purpose |
| --- | --- | --- |
| `REALTIME_VOICE_ENABLED` | off by default | Explicit local-only enablement |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | missing locally and from Octo Ergonaut namespace secret keys | Hosted room, scoped token, agent registration |
| `OPENAI_API_KEY` | locally present; Octo namespace secret key present | Direct GPT-Live; presence does not establish account/model eligibility |
| Hosted LiveKit project | access not established; local CLI/config missing | Room and agent dispatch |
| Local isolated PostgreSQL, Redis/worker and bot registry | must be configured for each isolated test | Django history and existing serialized background queue |

Never print keys. Do not use production data or modify shared service configuration for this prototype. Room tokens expire in five minutes and are restricted to one voice room. User and chat ownership are checked by Django. Each session permits one background request; duplicate request IDs reuse the same queued message. Results are sourced from the linked thread message, not model assertions. The UI reconnects to the same logical voice session. Assistant transcript labels indicate interrupted or unknown playback; generated text is not proof it was heard.

## Bounded paid-test proposal — approval required

**Do not run until the coordinator approves the complete cost and setup plan.** Use an isolated, non-production Django database and worker, an authorized test account and one disposable LiveKit project. Configure the local browser/API and bridge to use the same database. A bridge process requires a fresh one-shot cap file and permits one provider WebSocket attempt, one admitted room job and 90 seconds of voice runtime; use an outer wall-clock kill as a second limit. For example, after prerequisites and explicit approval:

```sh
ERGO_VOICE_CALL_CAP_FILE="/tmp/ergo-voice-cap-$(date +%s)-$$" \
REALTIME_VOICE_ENABLED=1 PYTHONPATH="$PWD/ergonaut:$PWD/src" \
timeout --kill-after=5s 115s /path/to/isolated-bridge-venv/bin/python \
  -m ergonaut.realtime_voice_bridge start
```

The bridge environment must already contain the pinned SDK packages and Django/Ergonaut runtime dependencies. Do not pass secrets on the command line. Start the browser and isolated Django/Celery separately; select the voice room once and issue at most one background request. The cap file remains after the run: do not remove it to retry without fresh approval. The bridge logs elapsed time to finalized assistant transcript and elapsed wall time plus an estimated voice charge; these are **not** provider-billed usage or verified first-audio latency. Reconcile the provider and LiveKit dashboards separately; logs contain no transcript by design.

[OpenAI's published rate](https://developers.openai.com/api/docs/models/gpt-live-1) is $0.05/minute, so 90 seconds is at most $0.075 in voice duration at that rate, subject to provider billing details. LiveKit transport and the existing delegated bot's models/tools are additional and are **not bounded by the voice cap**. A hard total-spend/call cap for that bot and a verified LiveKit project/entitlement are prerequisites for approval, not assumptions. No paid call was made.

The existing queue is not the proposed bounded harness: `messaging._run_turn`
calls the configured bot's `ask`, `Bot` includes an always-on worker toolkit,
and `chat_reply_spec` permits the bot's configured number of model turns.
The private-inference ledger can reserve a priced request against a private
pool's daily cap, but that does not by itself bound every provider, worker,
tool, retry and recursive delegation in this voice request. Consequently no
defensible maximum total delegated-work cost can be calculated from the voice
cap; no real background request has been attempted. A dedicated enforced
allowlist and per-request budget/deadline are prerequisites, not a prompt.

## Rollout and known gaps

Do not deploy, run a production migration or enable the flag without a reviewed migration, credentials, project isolation and explicit authorization. Rollback is disabling the flag and stopping only the isolated bridge; persisted Django history and already queued bot work remain. A shared-service rollout needs separate approval and a migration/retention plan.

Known gaps before acceptance: no live hosted room or real bot result has been verified; adapter reconnect, browser microphone/playback and interruption are not proven. Automatic compact state uses conservative extractive rules, not a semantic summarizer: unrecognized facts can fall outside the bounded restore window. User-reported activities are anchored to server receipt, not the physical action; client timestamps are separately stored, with unknown clock skew. The bridge provides server-calculated time since reports to GPT-Live as silent context; live response timing has not been verified. Completion records remain pending across reconnect until an actual acknowledgment; because no correlated browser playback acknowledgment is available, announcements can repeat. Generated text and playback are not proof of hearing. A reconnect needs a new explicitly approved one-shot bridge invocation because the paid-test cap forbids a second provider connection. End-to-end delegation usage/spend and provider billing are not metered or capped by this prototype. Do not represent it as production-ready.
