# Realtime voice prototype

This is an **off-by-default prototype**, separate from composer dictation, enabled on Octo for one allowlisted account only (see "Hosted deployment"). Wake-word and a general Stage are out of scope. Django holds session state, finalized text and task references; LiveKit carries audio, not durable memory. No audio recordings are stored by this code.

## Adapter and setup

The pinned direct adapter is `livekit-agents==1.8.11`, `livekit-plugins-openai==1.8.11` and `livekit-plugins-silero==1.8.11`; it uses `GPTLiveModel(model="gpt-live-1", delegation="client")`, not the older Realtime adapter or a dictation pipeline. [LiveKit](https://docs.livekit.io/agents/models/realtime/gpt-live.md) and [OpenAI](https://developers.openai.com/api/docs/guides/live) document this combination. The adapter's OpenAI SDK 3.x requirement conflicts with Ergo's `<3` dependency; use a separate bridge environment with the Ergonaut source on `PYTHONPATH`, not a production dependency upgrade. `livekit-api==1.2.2` provides Ergonaut's room token signer.

The isolated bridge requirements also pin `redis==8.1.0` for the Celery/Kombu Redis transport; the repository's OpenAI SDK constraint is unchanged. Offline preflight publishes and consumes a harmless probe on a unique isolated Redis queue, without a worker or model. A post-commit dispatch failure marks a still-queued voice message failed and reports a not-run outcome through the same session's durable outbox; if a worker has already claimed it, its state is not overwritten. The assistant may say a request was submitted, not that it is running. No paid retry is automatic.

Client delegation is an opaque provider event, not a function/tool call: `session.delegation.created` with `delegation.id` and `target="client"` emits `GPTLiveDelegation(id, pending_transcript)`. Voice tools stay empty. The bridge immediately captures the first ID and collects ordered SDK transcript segments; it dispatches only after actual Python Silero VAD detects speech and an end of speech, the configurable pause grace has elapsed, the transcript has been quiet for 0.8 seconds and all observed segments are final. A resumed VAD speech event resets the boundary. No model delegation means no background task: VAD alone never submits work. Stop, session end, missing final text or the 60-second collection deadline fail closed without an automatic retry. A durable result is added as silent authoritative context before commentary correlated with the original delegation ID.

The LiveKit 1.8.11 integrated recognition path receives the same input frames as GPT-Live and runs the explicitly supplied Silero VAD. Its default silence threshold is 0.55 seconds; `REALTIME_VOICE_PAUSE_GRACE_SECONDS` adds 2.0 seconds by default (allowed 0.5–10). Thus dispatch waits at least roughly 2.55 seconds after acoustic silence, plus scheduling/inference overhead or any longer transcript drain. Increasing grace trades latency for tolerance of longer pauses. Acoustic silence is not semantic completion: a pause longer than the grace, missed speech, late provider fragments or transcription omissions can still truncate a request. Final SDK segments and prompt instructions are not guarantees of complete text or truthful generated speech. Queued commentary proves neither transmission nor playback. See the [causal review](realtime-voice-causal-review.md).

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
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Encrypted local SOPS configuration, mode 600; decrypted into an isolated test process only | Hosted room, scoped token, agent registration |
| `OPENAI_API_KEY` | Octo Ergonaut runtime key returned HTTP 200 for both model metadata endpoints; the local shell key returned 401 | Metadata access is not proof of inference entitlement or billing capacity |
| Hosted LiveKit project | Existing Ergonaut project linked in the protected CLI profile; read-only rooms API succeeded | Shared project, not an isolated project |
| Local isolated PostgreSQL, Redis/worker and bot registry | Disposable instance prepared under `/tmp/ergo-voice-isolated-e1cy2fyd`; see [progress](realtime-voice-progress.md) for lifetime and ownership | Django history and existing serialized background queue |

Never print keys. Do not use production data or modify shared service configuration for this prototype. Room tokens expire in five minutes, are restricted to one voice room, and permit microphone publishing but not data publishing. User and chat ownership are checked by Django. Browser clients cannot write arbitrary events, state or delegations. The model selects delegation without a separate confirmation; submission and session end are serialized under the session lock. Results are sourced from the linked thread message, not model assertions. The UI reconnects to the same logical voice session. Assistant transcript labels indicate interrupted or unknown playback; generated text is not proof it was heard.

## Bounded paid-test proposal — approval required

**Do not run until the coordinator approves the complete cost and setup plan.** Use an isolated, non-production Django database, broker, worker, bot registry and test account; do not attach to existing `ergonaut up` services. The LiveKit project is shared, not disposable: approval must cover it and the conditional reserve. The isolated instance uses a random agent dispatch name and room prefix, distinct from `ergo-voice`. This makes accidental collision unlikely but does not prove that no external worker can register the same name. The bridge rejects other room prefixes before joining and uses `record=False` for SDK session recording; no shared project observability setting was changed.

The following historical command reads the private first-attempt environment and obtains the existing SOPS and Octo runtime credentials in process memory. Its markers have been consumed; do not use it for another attempt. Without the flag it checks local routing, the pinned bot, one-shot cap and a locally signed room token, then exits without starting the bridge. It does not establish provider entitlement, shared allowance or external-worker isolation. The wrapper reads the Octo secret, so it is not an offline command; the underlying `ergonaut.realtime_voice_preflight` checks are offline once credentials are already in the environment.

```sh
source /tmp/ergo-voice-isolated-e1cy2fyd/environment.sh
/tmp/ergo-voice-bridge/bin/python -m ergonaut.realtime_voice_local
```

The approved command first checks both OpenAI model metadata endpoints and the LiveKit rooms API, then creates a persistent exclusive launch marker and runs the bridge with an outer 115-second timeout. These read-only checks prove neither paid entitlement nor a hard spend limit. A second exclusive cap file is consumed on the first admitted job; one provider WebSocket attempt and 90 seconds of voice runtime are allowed. Neither marker should be removed for a retry without fresh approval.

The bridge environment must already contain the pinned SDK packages and Django/Ergonaut runtime dependencies. Do not pass secrets on the command line. Start the browser and isolated Django separately; the bridge, that Django and its worker all run with `DJANGO_SETTINGS_MODULE=ergonaut.settings.voice_live_test`, which adds the delegation budget priced below (the bridge creates the delegation, the worker runs it under the budget the message carries). Background delegation is off under any settings that do not set `REALTIME_VOICE_DELEGATION_BUDGET`. The cap file remains after the run: do not remove it to retry without fresh approval. The bridge logs elapsed time to finalized assistant transcript and elapsed wall time plus an estimated voice charge; these are **not** provider-billed usage or verified first-audio latency. Reconcile provider and LiveKit dashboards separately; logs contain no transcript by design.

Delegated work has a server-side budget (next section), but its input estimator has not been validated against provider usage, so this is not a guaranteed full-stack maximum. The conditional 90-second direct-provider estimate is **$0.08575**; with a 115-second provider duration it is **$0.10659** rounded up, before taxes and any costs outside the listed assumptions. The shared LiveKit Build allowance was unknown and disclosed. Three separately approved attempts have been used; billing remains unreconciled, and no fourth attempt is authorized.

## Delegation budget

A voice delegation is a thread message that carries a budget in its metadata (`turn_budget`). `realtime_voice.delegate` builds it on the server from `REALTIME_VOICE_DELEGATION_BUDGET`; the bridge can neither choose nor widen it, and with the setting unset or invalid delegation is off. The turn runner enforces it (`django_ergo.conversation.budget`, used by `structured._run_loop`, `messaging._run_turn` and the API engines), and only for messages that carry one: a turn without a budget runs exactly as before, approvals included. A budget that can't be read, or one whose start window passed before the chat was free, fails the message ("Not run: ...") instead of running it unbudgeted.

| Limit | How it is enforced |
| --- | --- |
| Model turns (`max_turns`) | The loop makes no request past it. There is no wrap-up turn: the answer is the model's, so a stop must read as a stop. |
| Output tokens (`max_output_tokens`) | Every request is sent with `max_tokens` (`max_completion_tokens` on OpenAI, reasoning included) no larger than what is left, so the provider holds the total. None left, no request. |
| Input tokens (`max_input_tokens`) | Provider-reported prompt tokens (cached ones included) accumulate. Before each request the engine refuses to send a payload whose UTF-8 size plus 1,000 exceeds what is left; a token is at least one byte, so that is an upper bound. A request that came back over the cap is discarded and its tool calls are answered "Not run". |
| Deadline (`deadline_seconds`) | Wall clock from the start of the call, setup included. It is each request's timeout (the time left), with an asyncio backstop 2 s later around the network wait only (so it never interrupts storing a response), and is checked before every request and after every tool step. A tool that is already running can't be interrupted. `start_within_seconds` sets how long the message may wait for the chat before it is not run. |
| Tools (`allowed_tools`) | Only these are offered and run; any other existing tool is refused with a message and recorded. A tool that needs approval is refused too: a budgeted turn never pauses, so nothing can resume it without its budget. Tools must be ones the chat offers anyway (an always-loaded skill's, such as the history tools). |
| No worker or agent starts, no thread sends | Refused where they happen (`workers.start`, `agents.start`, `BotTasks.start`, `messaging.send`, the orchestrator's send path, `wakes.schedule`, since a wake fires later as an unbudgeted turn), so even an allowed tool can't do them. The guard is active for the whole call, setup and any structured call a tool makes inside it. Attempts are recorded in `refused_actions`. |
| No automatic retries | No library retry, and the SDK client is used with `max_retries=0`. No compaction and no model routing either: both would be model requests or model changes outside the budget. |
| Engine and model | Only engines that claim `supports_request_limits` (the Claude and OpenAI API engines; a subclass must claim it itself, so the CLI subscriptions and Fireworks do not) and, when `model` is set, exactly that model. Otherwise the turn fails before any request. |

When a limit is hit the call ends `failed` with `error_category` `budget`, the thread message is `failed` with the same error (written by the library, never by the model: which limit, what was used, that there is no final answer), and `call.metadata["budget"]` holds the limits, the use (`model_turns`, `input_tokens`, `output_tokens`, `seconds`), `stopped_by`, `refused_tools` and `refused_actions`. The voice outbox records that as the result event of the same voice session ("The background request failed: Stopped by the request budget (max_turns): ..."). Any other failure text (an exception, a provider error) is not relayed: the session hears only "The background request failed." Elapsed tool time, one request's own duration and the estimator's conservatism are the places this can drift from the numbers; only a live run shows how far.

Not covered, and why that is acceptable here: a tool that calls a provider or starts a subprocess itself (web search, bash, local shell, orca, kubectl) is held back only by the allowlist, so never allow one; plugin before/after-turn hooks and context sources run as for any turn (server code, not chosen by the model); a worker that dies mid-turn leaves its message `delivered` (never retried, since the claim is a one-way update, but also never reported as failed, so the voice session would show it pending until its 90 seconds end). A first request measured with the cooking scenario, the two history tools and `send_reply` was 6,621 bytes, so the estimator reads 7,621 against the 40,000 cap; a real chat adds its instructions, skill list and history, so measure a dry run of the actual test chat first.

The profile used for the live test is `ergonaut/settings/voice_live_test.py`:

```python
REALTIME_VOICE_DELEGATION_BUDGET = {
    "max_turns": 4,
    "max_input_tokens": 40_000,
    "max_output_tokens": 1_500,
    "deadline_seconds": 45,
    "start_within_seconds": 30,
    "model": "gpt-6-luna",
    "allowed_tools": ["ergo_chat_history_read", "ergo_chat_history_search"],
}
```

The chat the voice session belongs to must be a bot on the OpenAI API engine (`engine: {type: openai, ...}`) and `gpt-6-luna`; Ergonaut's default routing (`auto/<tier>` on the Claude and Codex subscriptions) is refused because a subscription CLI can't hold a request to a budget. The history tools read the user's stored chats; they change nothing. The delegation runs in the voice session's own chat, so its history is part of every request: a chat too large for 40,000 input tokens fails with the input limit instead of running, so use a fresh test chat.

## Conditional cost estimate for one live test

Sources, read 2026-11-11: [GPT-Live-1 model page](https://developers.openai.com/api/docs/models/gpt-live-1) ("Voice sessions cost $0.05 per minute, billed per second", "not rounded up to the next whole minute", "Backend model and tool usage is billed separately"); [OpenAI pricing page](https://developers.openai.com/api/docs/pricing) (the GPT-Live row, and the `gpt-6-luna` Standard row); [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna). The repository's own table (`django_ergo/pricing.py`) agrees with the Standard row.

| Component | Rate | Assumed quantity | Estimated cost |
| --- | --- | --- | --- |
| GPT-Live voice session | $0.05 per minute, per second | 90 s | $0.07500 |
| Delegation input, `gpt-6-luna` | at most $0.25 per 1M (12-hour cache write, the highest input-side rate) | 40,000 tokens | $0.01000 |
| Delegation output, `gpt-6-luna` | $0.50 per 1M | 1,500 tokens | $0.00075 |
| **Total** | | | **$0.08575** |

The rates for `gpt-6-luna`, Standard, prompts up to 272K tokens, per 1M tokens: input $0.10, cached input $0.01, 30-minute cache write $0.125, 12-hour cache write $0.25 (the pricing page; the model page lists only the $0.125 write), output $0.50. The calculation prices every assumed input token at the highest listed short-context input-side rate; the code sends no cache-retention parameter. At the uncached $0.10 rate the same assumed quantities cost $0.00475 for delegation and $0.07975 in all; at the 30-minute write rate $0.00575 and $0.08075. The input estimator has not been validated against provider-reported usage; actual billed input can exceed the assumption.

This estimate assumes Standard processing (the engine sets no `service_tier`), no regional endpoint (+10%), a prompt below the 272K long-context surcharge, `reasoning_effort: none`, and exactly `gpt-6-luna` (the budget's `model`). It assumes one delegation per voice session and provider-billed usage within the stated token and time quantities. The live bot registry's default is `auto/medium`, not this API model; use a fresh isolated bot pinned to `engine: {type: openai, config: {model: gpt-6-luna}}`, not the shared bot registry or a subscription CLI.

### LiveKit and conservative planning

[LiveKit pricing](https://livekit.com/pricing) and [quotas](https://docs.livekit.io/deploy/admin/quotas-and-limits) list Build at $0/month, 5,000 WebRTC participant-minutes and 50 GB downstream per month. A locally hosted bridge counts as a WebRTC participant, so one user and one bridge connected for 90 seconds use approximately 3 participant-minutes, plus downstream media/data. The 1,000 hosted-agent minutes do not apply to this local bridge. Build allowances are shared across a user's free projects; after exhaustion new requests fail rather than incurring overages. Current project plan, remaining allowance and the effects of concurrent use are unknown: verify in the dashboard before approval. On Ship, WebRTC above allowance is $0.0005/participant-minute and downstream above allowance $0.12/GB; Ship itself starts at $50/month. Do not upgrade for this test.

[Billing](https://docs.livekit.io/deploy/admin/billing) meters time in seconds with a 10-second WebRTC minimum and rounds individual transfers up to 0.01 GB. For a conditional Ship-overage illustration only, two participants at 115 seconds each cost $0.00192 rounded up; one rounded 0.01 GB transfer costs $0.00120, but the number and size of separately metered transfers are not established, so this is not a bandwidth ceiling. No recording, SIP, LiveKit-hosted agent or LiveKit Inference is intended; verify observability/audio upload is disabled before use, rather than assuming SDK defaults. Local compute and network are existing resources, not priced incremental cloud services.

For approval planning, reserve **$0.15 plus taxes** for this single attempt, conditional on a confirmed Build plan with sufficient allowance, no optional paid services, no retries, provider duration at most 115 seconds and usage within the stated token limits. This is a planning reserve, **not an enforceable financial ceiling**: provider cleanup/billing time, input estimation, the provider's hidden overhead, and any already-running tool cannot be strictly bounded from these local controls. If any condition fails or costs cannot be monitored, do not run. Reconcile provider usage and LiveKit dashboard after the attempt; do not automatically retry.

## Hosted deployment (Octo)

Lee approved a hosted rollout for his own testing on 2026-11-12. It runs on Octo in namespace `ergonaut` (manifests in `boundcorp/infra-octo`, `kubernetes/apps/ergonaut/app/`):

- The release image carries a second virtualenv, `/app/.voice-venv` (the `voice-deps` stage of `ergonaut/Dockerfile`): Ergonaut's pinned requirements except `openai`, `redis` and `pillow`, plus `realtime-voice-requirements.txt` (OpenAI SDK 3, LiveKit agents 1.8.11). The app's own environment and SDK are unchanged.
- Deployment `voice-bridge`, one replica, `Recreate`, the same image pin as the other roles (so the upgrader moves it with them), runs `/app/.voice-venv/bin/python -m ergonaut.realtime_voice_bridge start` with `DJANGO_SETTINGS_MODULE=ergonaut.settings.voice_live_test` (the delegation budget above). It uses the app's database and Redis broker. Its readiness probe (`:8081/`) is 200 only while it is registered with LiveKit; `:8081/worker` shows the agent name and active jobs.
- `ergonaut-config` sets `REALTIME_VOICE_ENABLED`, `REALTIME_VOICE_USERS`, `REALTIME_VOICE_AGENT_NAME` (a unique dispatch name; web's room tokens request it and the bridge registers under it) and `REALTIME_VOICE_ROOM_PREFIX`; `ergonaut-runtime` (SOPS) holds `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.

Access: `REALTIME_VOICE_USERS` is a comma-separated list of exact usernames; everyone else gets `enabled: false` from `/api/realtime-voice/config` and 404 from every voice endpoint, the token included. An empty list allows nobody. The bridge re-checks the allowlist and the kill switch when it takes a job.

Limits (unchanged from the prototype): each logical voice session gets one bridge job, recorded in `RealtimeVoiceSession.bridge_started_at` (a second dispatch for the same session is refused, so there is one provider connection per session). When the job ends (90 seconds, the user leaving, an error) the bridge ends the session, so the next Start creates a new one; a claimed session gets no new room token (409), and one whose bridge died without ending it is ended by the next token request once 120 seconds have passed; a job lasts at most 90 seconds; `record=False`; provider connection `max_retry=0`; the delegation budget and its allowlist as above. The local paid-test harness still also caps a whole bridge launch at one job with `ERGO_VOICE_CALL_CAP_FILE`; the hosted bridge does not set it.

Delegation only runs in a chat whose bot is on the OpenAI API engine and `gpt-6-luna` (the budget's pinned model); in any other chat the request is not run and the session hears that it failed.

**Kill switch.** Set `REALTIME_VOICE_ENABLED: "false"` in `ergonaut-config` (infra-octo) and restart web and the bridge (`kubectl --context octo -n ergonaut rollout restart deploy/web deploy/voice-bridge`); no image deploy is needed. With it off the web app hides voice and refuses tokens, and the bridge refuses every job. For an immediate stop before the commit lands, `kubectl --context octo -n ergonaut scale deploy/voice-bridge --replicas=0` leaves no agent to join a room; Flux restores the replica on its next reconcile (≤5 min), so commit the flag too. Persisted Django history and already queued bot work remain.

## Known gaps

Known gaps before acceptance: the third hosted synthetic attempt produced a real owner-scoped history search and same-session durable result, but delegated before the request ended and generated contradictory status after the answer. The hands-free VAD correction is offline only; hosted speech boundaries, transcript completeness, model delegation and spoken truthfulness remain unverified. Adapter reconnect, browser microphone/playback and interruption are not proven. Automatic compact state uses conservative extractive rules, not a semantic summarizer: unrecognized facts can fall outside the bounded restore window. User-reported activities are anchored to server receipt, not the physical action; client timestamps are separately stored, with unknown clock error. The bridge provides server-calculated time since reports to GPT-Live as silent context; live response timing has not been verified. A durable claim permits at most one commentary attempt per result across reconnects. A crash after the claim can lose the spoken announcement, but the result remains in the session card; the claim does not indicate playback. GPT-Live's `append_commentary` asks the model to speak in its own words and returns no speech identifier, so it cannot be correlated to browser playback with the inspected adapter. No browser acknowledgment or human hearing is claimed. The model is instructed not to repeat restored results unless asked, but this is not a deterministic guarantee. A reconnect after the bridge job ends is refused (one job per voice session); start a new voice session instead. End-to-end delegation usage/spend and provider billing are not capped by this prototype. Do not represent it as production-ready.
