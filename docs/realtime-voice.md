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

Never print keys. Do not use production data or modify shared service configuration for this prototype. Room tokens expire in five minutes, are restricted to one voice room, and permit microphone publishing but not data publishing. User and chat ownership are checked by Django. Browser clients cannot write events, state, or delegations: those are bridge-only operations. A queued delegation is serialized under a session row lock; duplicate IDs reuse the same message. Results are sourced from the linked thread message, not model assertions. The UI reconnects to the same logical voice session. Assistant transcript labels indicate interrupted or unknown playback; generated text is not proof it was heard.

## Bounded paid-test proposal — approval required

**Do not run until the coordinator approves the complete cost and setup plan.** Use an isolated, non-production Django database and worker, an authorized test account and one disposable LiveKit project. Configure the local browser/API and bridge to use the same database. A bridge process requires a fresh one-shot cap file and permits one provider WebSocket attempt, one admitted room job and 90 seconds of voice runtime; use an outer wall-clock kill as a second limit. For example, after prerequisites and explicit approval:

```sh
ERGO_VOICE_CALL_CAP_FILE="/tmp/ergo-voice-cap-$(date +%s)-$$" \
REALTIME_VOICE_ENABLED=1 PYTHONPATH="$PWD/ergonaut:$PWD/src" \
timeout --kill-after=5s 115s /path/to/isolated-bridge-venv/bin/python \
  -m ergonaut.realtime_voice_bridge start
```

The bridge environment must already contain the pinned SDK packages and Django/Ergonaut runtime dependencies. Do not pass secrets on the command line. Start the browser and isolated Django separately; the bridge, that Django and its worker all run with `DJANGO_SETTINGS_MODULE=ergonaut.settings.voice_live_test`, which adds the delegation budget priced below (the bridge creates the delegation, the worker runs it under the budget the message carries). Background delegation is off under any settings that do not set `REALTIME_VOICE_DELEGATION_BUDGET`. The cap file remains after the run: do not remove it to retry without fresh approval. The bridge logs elapsed time to finalized assistant transcript and elapsed wall time plus an estimated voice charge; these are **not** provider-billed usage or verified first-audio latency. Reconcile provider and LiveKit dashboards separately; logs contain no transcript by design.

Delegated work is held to a hard server-side budget (next section), so the cost of the whole test has a ceiling. [OpenAI's published rate](https://developers.openai.com/api/docs/models/gpt-live-1) for the voice session is $0.05/minute, billed per second: $0.075 for 90 seconds. The ceiling for voice plus capped delegation is **$0.08575**, derived under "Maximum cost of one live test". LiveKit transport is additional and not verified. No paid call was made.

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

## Maximum cost of one live test

Sources, read 2026-11-11: [GPT-Live-1 model page](https://developers.openai.com/api/docs/models/gpt-live-1) ("Voice sessions cost $0.05 per minute, billed per second", "not rounded up to the next whole minute", "Backend model and tool usage is billed separately"); [OpenAI pricing page](https://developers.openai.com/api/docs/pricing) (the GPT-Live row, and the `gpt-6-luna` Standard row); [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna). The repository's own table (`django_ergo/pricing.py`) agrees with the Standard row.

| Component | Rate | Quantity | Ceiling |
| --- | --- | --- | --- |
| GPT-Live voice session | $0.05 per minute, per second | 90 s | $0.07500 |
| Delegation input, `gpt-6-luna` | at most $0.25 per 1M (12-hour cache write, the highest input-side rate) | 40,000 tokens | $0.01000 |
| Delegation output, `gpt-6-luna` | $0.50 per 1M | 1,500 tokens | $0.00075 |
| **Total** | | | **$0.08575** |

The rates for `gpt-6-luna`, Standard, prompts up to 272K tokens, per 1M tokens: input $0.10, cached input $0.01, 30-minute cache write $0.125, 12-hour cache write $0.25 (the pricing page; the model page lists only the $0.125 write), output $0.50. Pricing every input token at the $0.25 write rate cannot be exceeded and is not reachable here: the code sends no cache-retention parameter. At the uncached $0.10 rate the same caps cost $0.00475 for the delegation and $0.07975 in all; at the 30-minute write rate $0.00575 and $0.08075.

What this holds only under these conditions, all of which the code or the budget fixes: Standard processing (the engine sets no `service_tier`, so no Fast 2x, Flex or Batch), no regional endpoint (+10%), a prompt far under the 272K long-context surcharge (the input cap is 40,000), `reasoning_effort: none` because the engine forces it for GPT-6 with tools (output tokens, reasoning included, are capped at 1,500 anyway), and the engine on exactly `gpt-6-luna` (the budget's `model`). One delegation per voice session, so the delegation figure is for one budgeted request. If the backend model that is actually configured is not `gpt-6-luna`, this figure does not apply: no live Ergonaut configuration was read, only the library default and the `ergo-bots` bots that pin it.

Not included: LiveKit transport and any LiveKit plan charges (not verified), taxes, and voice seconds outside the 90-second runtime. The outer `timeout --kill-after=5s 115s` bounds a runaway to 115 s: $0.09583 for voice, so $0.10658 with the delegation ceiling.

## Rollout and known gaps

Do not deploy, run a production migration or enable the flag without a reviewed migration, credentials, project isolation and explicit authorization. Rollback is disabling the flag and stopping only the isolated bridge; persisted Django history and already queued bot work remain. A shared-service rollout needs separate approval and a migration/retention plan.

Known gaps before acceptance: no live hosted room or real bot result has been verified; adapter reconnect, browser microphone/playback and interruption are not proven. Automatic compact state uses conservative extractive rules, not a semantic summarizer: unrecognized facts can fall outside the bounded restore window. User-reported activities are anchored to server receipt, not the physical action; client timestamps are separately stored, with unknown clock error. The bridge provides server-calculated time since reports to GPT-Live as silent context; live response timing has not been verified. A durable claim permits at most one commentary attempt per result across reconnects. A crash after the claim can lose the spoken announcement, but the result remains in the session card; the claim does not indicate playback. GPT-Live's `append_commentary` asks the model to speak in its own words and returns no speech identifier, so it cannot be correlated to browser playback with the inspected adapter. No browser acknowledgment or human hearing is claimed. The model is instructed not to repeat restored results unless asked, but this is not a deterministic guarantee. A reconnect needs a newly approved one-shot bridge invocation because the paid-test cap forbids a second provider connection. End-to-end delegation usage/spend and provider billing are not capped by this prototype. Do not represent it as production-ready.
