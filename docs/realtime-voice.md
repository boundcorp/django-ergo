# Realtime voice prototype

This is an **off-by-default prototype**, separate from composer dictation, enabled on Octo for one allowlisted account only (see "Hosted deployment"). Wake-word and a general Stage are out of scope. Django holds session state, finalized text and task references; LiveKit carries audio, not durable memory. No audio recordings are stored by this code.

**Historical acceptance/release checkpoint, 2026-11-12:** Lee accepted phone audio/basic conversation, not Android Bluetooth or orchestration. Source PR #297 was live as `6b33f825` / `ffa206f6` through infra-octo #171. After all old consumers exited and a fresh acknowledged quiet window, exact scoped policy #170 merged at 23:34:50 (`eff442d9`) and matching web/bridge configuration was verified at 23:38:39. Only new logical sessions could receive the normal-thread grant; the then-newest retained 22:46 call stayed ungranted and had failed before durable delegation. Normal task costs were approved at 21:15:41; normal approvals, one root request and the 90-second voice cap remain. No hosted normal-task/provider test ran at that checkpoint, and Android output remained physically unknown. [Historical policy evidence](realtime-voice-progress.md#continuation--newest-live-session-and-ux-2026-11-12) separates policy activation from the UX frontend release.

**Latest owner correction, 2026-11-13 18:53 UTC:** input transcription now
works in the Android Brave/Bluetooth call. The owner withdrew the latest apparent
playback fault: device volume was down, and playback is not broken. This call is
not evidence of an output-pipeline defect; no speculative playback fix is made.
Historical physical Android/Bluetooth route acceptance remains separate. See the
[current causal audit](realtime-voice-causal-review.md#same-call-continuation-and-volume-correction--2026-11-13).

The [all-session usage audit and separate draft315 safety review](realtime-voice-audit-20261113.md)
enumerates all five production calls and three preserved paid attempts, with
UNKNOWN billing/allowance boundaries and a reusable owner/time-filtered
`voice_audit` READ ONLY metadata export. The completed 02:10 backend call records
`claude-opus-5-5`, not the earlier reconstructed Codex route.

## Adapter and setup

The pinned direct adapter is `livekit-agents==1.8.11`, `livekit-plugins-openai==1.8.11` and `livekit-plugins-silero==1.8.11`; it uses `GPTLiveModel(model="gpt-live-1", delegation="client")`, not the older Realtime adapter or a dictation pipeline. [LiveKit](https://docs.livekit.io/agents/models/realtime/gpt-live.md) and [OpenAI](https://developers.openai.com/api/docs/guides/live) document this combination. The adapter's OpenAI SDK 3.x requirement conflicts with Ergo's `<3` dependency; use a separate bridge environment with the Ergonaut source on `PYTHONPATH`, not a production dependency upgrade. `livekit-api==1.2.2` provides Ergonaut's room token signer.

The isolated bridge requirements also pin `redis==8.1.0` for the Celery/Kombu Redis transport; the repository's OpenAI SDK constraint is unchanged. Offline preflight publishes and consumes a harmless probe on a unique isolated Redis queue, without a worker or model. A post-commit dispatch failure marks a still-queued voice message failed and reports a not-run outcome through the same session's durable outbox; if a worker has already claimed it, its state is not overwritten. The assistant may say a request was submitted, not that it is running. No paid retry is automatic.

Client delegation is an opaque provider event, not a function/tool call: `session.delegation.created` with `delegation.id` and `target="client"` emits `GPTLiveDelegation(id, pending_transcript)`. Voice tools stay empty. The bridge reserves a finite request slot and collects ordered SDK transcript segments; it dispatches only after actual Python Silero VAD detects speech and an end of speech, the pause grace has elapsed, the transcript has been quiet for 0.8 seconds and observed segments are final. A resumed VAD speech event resets the boundary. No model delegation means no background task. Stop, session end, missing final text or the 60-second collection deadline fail closed without automatic retry. A verified result is supplied as silent authoritative context before plain commentary correlated with its delegation ID; internal labels and IDs are not spoken narrative. Neither commentary enqueueing nor an announcement claim proves transmission, model consumption, playback or hearing.

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

Never print keys. Do not use production data or modify shared service configuration for this prototype. Room tokens expire in five minutes, are restricted to one voice room, and permit microphone publishing but not data publishing. User and chat ownership are checked by Django. Browser clients cannot write arbitrary events, state or delegations. The model selects delegation without a separate confirmation; submission and session end are serialized under the session lock. Results are sourced from the linked thread message, not model assertions. The UI reconnects to the same logical voice session. The transcript displays each SDK segment separately, replacing a partial with its matching final; segments are not necessarily complete conversational replies. Interruption remains labeled, while bounded delivery uncertainty appears in Diagnostics rather than on every bubble. Generated text is not proof it was heard.

The pinned SDK creates output IDs for acoustic bursts and late text, not verified semantic replies. Neither a shared bridge claim nor adjacent speakers establishes a response boundary; historical boundaries are unknown. Full reply coalescing requires an upstream contract that identifies semantic responses across segments and distinguishes consecutive responses.

## Bounded paid-test proposal — approval required

**Do not run until the coordinator approves the complete cost and setup plan.** Use an isolated, non-production Django database, broker, worker, bot registry and test account; do not attach to existing `ergonaut up` services. The LiveKit project is shared, not disposable: approval must cover it and the conditional reserve. The isolated instance uses a random agent dispatch name and room prefix, distinct from `ergo-voice`. This makes accidental collision unlikely but does not prove that no external worker can register the same name. The bridge rejects other room prefixes before joining and uses `record=False` for SDK session recording; no shared project observability setting was changed.

The following historical command reads the private first-attempt environment and obtains the existing SOPS and Octo runtime credentials in process memory. Its markers have been consumed; do not use it for another attempt. Without the flag it checks local routing, the pinned bot, one-shot cap and a locally signed room token, then exits without starting the bridge. It does not establish provider entitlement, shared allowance or external-worker isolation. The wrapper reads the Octo secret, so it is not an offline command; the underlying `ergonaut.realtime_voice_preflight` checks are offline once credentials are already in the environment.

```sh
source /tmp/ergo-voice-isolated-e1cy2fyd/environment.sh
/tmp/ergo-voice-bridge/bin/python -m ergonaut.realtime_voice_local
```

The approved command first checks both OpenAI model metadata endpoints and the LiveKit rooms API, then creates a persistent exclusive launch marker and runs the bridge with an outer 115-second timeout. These read-only checks prove neither paid entitlement nor a hard spend limit. A second exclusive cap file is consumed on the first admitted job; one provider WebSocket attempt and 90 seconds of voice runtime are allowed. Neither marker should be removed for a retry without fresh approval.

The bridge environment must already contain the pinned SDK packages and Django/Ergonaut runtime dependencies. Do not pass secrets on the command line. Start the browser and isolated Django separately; the bridge, that Django and its worker all run with `DJANGO_SETTINGS_MODULE=ergonaut.settings.voice_live_test`, which adds the delegation budget priced below (the bridge creates the delegation, the worker runs it under the budget the message carries). Background delegation is off under any settings that do not set `REALTIME_VOICE_DELEGATION_BUDGET`. The cap file remains after the run: do not remove it to retry without fresh approval. The bridge logs elapsed wall time, provider-reported session usage if available, and elapsed time to finalized assistant transcript; these are **not** verified provider billing or first-audio latency. Reconcile provider and LiveKit dashboards separately; logs contain no transcript by design.

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

The PR #293 profile in `ergonaut/settings/voice_live_test.py` is deployed at `06284d4`, including the listing tools below. The earlier `75234dc` image allowed only the two history tools.

```python
REALTIME_VOICE_DELEGATION_BUDGET = {
    "max_turns": 4,
    "max_input_tokens": 40_000,
    "max_output_tokens": 1_500,
    "deadline_seconds": 45,
    "start_within_seconds": 30,
    "model": "gpt-6-luna",
    "allowed_tools": [
        "ergo_chat_history_read", "ergo_chat_history_search",
        "ergo_bot_list", "ergo_thread_list", "ergo_worker_list",
    ],
}
```

The selected chat must resolve to a request-limited API engine and exactly `gpt-6-luna`; Ergonaut's default subscription/CLI routes cannot hold a request to this budget. The draft preflights the actual selected engine instead of trusting a stored `engine_type` label, never reroutes the thread, and narrows configured tools against both a hard read-only ceiling and the chat's already-offered non-approval tools. Listing bots/threads/workers is read-only; pending approvals are summarized by existing thread listing, never approved. History and listings retain existing owner/bot access checks. The turn still runs in the exact launch thread: a context too large for 40,000 input tokens fails rather than escaping the budget. A populated-thread continuity test therefore needs a bounded fitting history/compaction, not a silently fresh unrelated chat.

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

With the standby flag off, each logical voice session gets one bridge job, recorded in `RealtimeVoiceSession.bridge_started_at`. When the job ends (90 seconds, the user leaving, an error) the bridge ends the session; a claimed session gets no new room token (409), and one whose bridge died without ending it is ended by the next token request once 120 seconds have passed. The bridge uses `record=False` and provider connection `max_retry=0`; the delegation budget and its allowlist remain as above. The local paid-test harness also caps a whole bridge launch at one job with `ERGO_VOICE_CALL_CAP_FILE`; the hosted bridge does not set it.

Delegation only runs in a chat whose bot is on the OpenAI API engine and `gpt-6-luna` (the budget's pinned model); in any other chat the request is not run and the session hears that it failed.

### Output routing (PR #293 deployed; Android acceptance open)

The UI probes a detached silent audio element without playing media or requesting microphone permission. A selector appears only after `setSinkId('')` succeeds and the element reports the default sink; a native `selectAudioOutput` button additionally requires that API. Selection awaits current playback elements and reads their applied sink IDs; failure restores the previous confirmed selection where possible, otherwise exposes unknown/mixed state. A chooser's returned device ID is applied separately; permission alone is not routing.

Remote audio stays on HTML audio elements (`webAudioMix=false`). A newly subscribed track gets its sink before LiveKit attaches/plays its stream. Track unsubscribe/close races cannot attach stale audio. Network reconnect rechecks devices; standby resume and in-app page navigation keep the app-owned sink selection and apply it to new room tracks. The selection lives with the authenticated call owner, not the page; a fresh owner starts at system default. `devicechange` refreshes exposed output devices and falls back to default if the selected one disappears; failures are shown rather than relabeled successful. Microphone input/mute stays separate and capture constraints are unchanged.

Unsupported/blocked browsers show **output controlled by your phone/operating system** guidance, not a Bluetooth/speakerphone imitation. The selector confirms **browser state only**, not a physical speaker or hearing: current iOS WebRTC reports include false-success sink changes. System default is not synonymous with speakerphone. See the [platform matrix and citations](realtime-voice-causal-review.md#browser-and-bluetooth-restrictions); only Lee's real-device test can accept the route.

The follow-up adds **Collect route diagnostics** on the dedicated page. It reads browser family/major, platform/tab mode, sink capability and applied selection category, attached HTML element state, optional audio-session intent and the existing microphone track's non-identifying processing settings. It never requests capture, selects output or connects a room; it contains no device IDs/names and remains local to the page. Collect again after mute, reconnect, Bluetooth loss or changing the OS default. It reports physical route as **unknown**, including when `setSinkId` succeeds. Android communication routing while capture is open can differ from normal media playback; the already-selected OS headset is not evidence of user misconfiguration.

### Microphone input evidence and explicit recovery

The earlier November 13 **15:51 UTC** input investigation is separate from
the later successful transcription and volume-down correction. That earlier call reached bridge Silero three times, but retained
no provider transcript or generated assistant audio. It ended explicitly after
34.438 seconds, not at the 90-second cap. A separate successful composer
`gpt-transcribe` request followed on the same owner/thread. Neither observation
establishes the actual phone/headset microphone, intelligibility or a Brave defect.
See the [current input audit](realtime-voice-causal-review.md#android-brave-input-audit--2026-11-13).

The page observes the existing native microphone track and LiveKit publication,
including ended/muted/unpublished/upstream-paused state and reconnect. It no
longer leaves an ended track labeled enabled. These browser states do not prove
nonzero audio, remote receipt or transcription. Device changes only refresh
observations and invalidate the local probe; they never select hardware,
restart capture or unmute. Hardware input source remains **unverified**.

**Recover microphone** is not a default call control. It appears only with a
classified capture error and requires an explicit click on the existing room:
it restarts the existing SDK track, or requests capture if no track exists.
A failed initial capture keeps the room and original deadline, with actionable
permission/missing/busy/capture guidance; it never automatically recaptures or unmutes.
Recovery is disabled for intentional mute, pending mute/unmute and while another recovery is pending. An SDK-muted
track is restored only by the explicit recovery action while it still belongs
to the current microphone publication. End/disconnect or lost publication
ownership stops late returned capture, including across restart and unmute.
Successful Unmute clears a prior capture error only after live publication is
observed. Microphone actions are serialized per room; application reconnect/
device callbacks never request capture. LiveKit's existing SDK-managed full
reconnect may reacquire previously permitted tracks. Normal task/action
approvals and costs are unchanged.

**Check existing microphone locally**, under Diagnostics, examines at most one
second of the existing track through an isolated analyser. It requests no new
capture, connects no output, records/uploads no audio and sends no provider
request. Nonzero samples mean only nonzero local PCM, not speech, a particular
microphone or provider receipt. A suspended/failed analyser reports unavailable,
not silence. The local route snapshot now includes capture/publication/error/
probe state; no device identifiers or names are added.

Bounded durable `bridge_input` events distinguish actual Silero speech,
provider session start, input queued immediately before a WebSocket send,
nonempty provider transcription and a generic provider error. The send event
fires **before** `send_str` succeeds, so it is not remote receipt. Missing stages
mean **not observed**, not proof of failure. The pinned SDK exposes no public
raw-frame observation hook; no private input pump is replaced to invent one.
Each stage has fixed text and at most one event per bridge claim, with no
transcript/audio/error payload. Android/Brave phone-versus-headset input and
physical Bluetooth communication routing still require authorized real-device
proof; automated checks here use synthetic PCM only.

### Launch-thread continuity and experimental read-only execution (PR #293 deployed)

On initial bridge startup and each authorized resume, voice reuses the owning bot's normal `context_builder`, chat identity and configured sources. The global orchestration overview and duplicate native-history source are excluded: other private threads are not a startup briefing. Startup includes at most **8,000 characters of owning instructions** and **16,000 context characters**: bounded configured context, the stored compaction (up to 2,000 characters), recent 40 exact-thread messages within a bounded render budget, durable reported decisions/current step, this thread's task/approval status, and five attachment references (not file contents). No paid compaction or global scan is introduced. Truncation is explicit; older detail remains an authorized history lookup, not a claim that all historic/actionable content fits.

Finalized user and assistant voice events are written idempotently into the **same launch thread's normal history**, with owner/bot identity, voice session/event provenance and server receipt timestamps. Assistant text retains generated-speech/delivery uncertainty. Extracted user decisions and current task persist with that thread and seed a later voice session. Reconnect restores persisted voice text through thread history once; pre-cutover voice events without a history row retain a bounded fallback. This does not duplicate the transcript into every live provider turn.

The bridge, not the web process, validates the actual selected engine/model and publishes a sanitized durable availability/restriction status. This matters because the deployed web uses default settings with no delegation budget while the bridge uses `voice_live_test`. Before validation the UI says **Bridge has not validated delegation for this session**, not a false failure based on web-only settings. Afterwards the UI and voice instructions name restricted read-only mode and the actual unavailability reason or offered read tools. Polling, transcript updates and delegation assembly do not rerender expensive startup context.

Bot/thread/worker lookups use the existing authorized chat toolkit and budgeted same-thread message path. Thread listing supplies bounded pending-approval tool names/status, not arguments or approval actions. Cross-thread overview is excluded only for explicitly scoped voice delegation; ordinary bot turns are unchanged. The original delegation ID travels through message/call metadata and the same logical session's durable outbox; a result claim is still not proof of spoken playback.

**Experimental-profile restriction, not the normal-thread contract:** read-only budgeted requests cannot send/forward/start/approve; the budget refuses those actions independently. The actual Boundcorp CLI engine cannot enforce that API-only token budget, and voice never silently reroutes it. The separately reviewed scoped normal-thread policy below supplies the authorized orchestration path with normal approvals and costs; read-only listings are not a substitute for that path or hosted acceptance.

### Default-off normal originating-thread execution (enabled only for the reviewed Octo scope)

`REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS` defaults to `{}`. It is a
reviewed **scoped JSON policy** supplied by settings or the same-named environment
variable, not a blanket boolean/browser toggle and not permission to test on the
real owner. Its mapping key is the exact launch-chat UUID; its value requires:

| Field | Required authorization |
| --- | --- |
| `owner` | Exact authenticated username owning this chat. |
| `bot` | Exact registered bot name already accessible to that owner. |
| `normal_task_costs_approved` | Exactly `True`, separately authorizing existing normal-task execution costs rather than claiming the prototype's API-token price covers them. |
| `approval_reference` | Nonempty reference to that reviewed authorization; recorded durably with the task. |
| `max_requests` | Optional; defaults to `1`. Only `1` or the reviewed finite cap `3` is accepted. |
| `followup_costs_approved` | Must be exactly `True` when `max_requests=3`; authorizes explicit sequential follow-up execution costs, not unlimited inference. |
| `followup_approval_reference` | Separate nonempty reviewed continuation reference when `max_requests=3`. |

Web and bridge must carry the same policy. Only a **new** owner-created voice
session receives its exact-chat/owner/bot grant and authorization timestamp.
Conversational restore state cannot inherit or mint one. The bridge rechecks
the matching current policy and normal bot access before dispatch; revocation
refuses new requests, not cancellation of already accepted normal work.
No grant means the existing priced read-only path and its engine gate remain
unchanged. No mandatory Send button or new callable GPT-Live tools are added.

An authorized model-selected client delegation enters the **existing exact
thread's normal message queue**, with its authenticated user identity, normal
engine/context/registered tools and approval semantics. It does not reroute the
bot or broaden its allowed targets. The transport still has its original
90-second wall-clock deadline. The default remains one root request; a separately
reviewed exact scope can permit **three sequential execution attempts total**.
Only one can be collecting, queued, delivered or awaiting approval at a time.
Duplicate IDs cannot consume another slot or replay a write. Expired collections
consume their attempt; reconnect/resume never resets allowance or deadline.
The UI shows remaining allowance and offers explicit continuation in the owning
text thread at exhaustion. Read-only status observation/result reading uses no
execution slot and launches no inference. Each root and normally authorized child
work remains governed by the bot's existing normal turn/task controls,
**not** the 40,000/1,500-token API lookup estimate. Normal compaction,
task tools and their downstream costs are therefore outside that experimental
estimate; this is why an explicit separate cost-policy decision is required.
The audited live Boundcorp bot has a normal **50-model-turn** limit; this is
not the prototype's four-turn limit or a new financial ceiling. Lee approved
normal originating-thread costs at **21:15:41 UTC**, but no real-owner normal
task was run during this investigation.

Only new logical sessions receive a changed grant; increasing configured capacity
cannot widen an existing one. Normal action approvals remain in the owning thread.
The bridge distinguishes speech collection, durable admission, queued/handler-
claimed work and persisted result. A handler claim does not prove model execution
or consumption; missing terminal evidence remains pending/unknown, not success.

### Composer Stop and Send

Ordinary composer dictation is independent of realtime call controls. **Stop**
ends capture, waits for final transcription and inserts it into the current editable
draft without sending or deleting selected draft text. **Send**, including Enter,
stops any recording, awaits that same finalization and invokes the normal send
handler exactly once with the latest draft and final transcript. Duplicate clicks,
Enter and reentrant sends share one gate. Edits made during finalization survive.
Failure keeps the draft and recoverable audio, exposes Retry/Download guidance
and sends nothing partial. Cancel, unmount or a draft-route change invalidates
late responses and pending sends. Retry restores an editable draft; it does not
automatically send. Offline browser coverage uses native MediaRecorder with generated
audio and mocked transcription only, not a paid transcription or hardware test.


Submission is serialized by the session and unique delegation ID. Opted-in
normal tasks record a durable claim for **each external-tool invocation ID
within a structured call**, bound to the tool name and canonical argument hash,
before running its actual handler. Duplicate IDs reuse the receipt; conflicting
arguments are refused. A crash after claim has an unknown outcome and is never
automatically replayed. A new ID is a new normal tool invocation, not a promise
that an external API supports exactly-once effects. Existing approval checks
happen **before** any handler claim; approval/denial and stale-answer protection
use the normal thread endpoint, including its admin-only restrictions. Speech,
a provider signal or status lookup cannot approve an action.
The local structured-output validator has no external effect and is revalidated
instead of cached, so a repeated final reply can complete after approval or denial.
Automatic Ergo request retries and API-SDK retries are disabled for the scoped
normal task, its normal approval continuation and causally delegated messages.
Asynchronous `Worker`/`BotTasks` handoffs retain their existing normal controls;
they do not inherit these invocation receipts or retry-policy metadata. This is
not end-to-end retry suppression or exactly-once execution across worker tasks.
The actual Codex CLI still owns its internal provider transport behavior; this
is not a claim to count or financially bound hidden provider attempts, and no
provider configuration or route is changed.

Causal normal thread sends/replies retain the original session/delegation IDs.
The bounded observer follows those durable rows and existing worker reports
arriving in the launch thread; existing reports are labeled observations, not
proof voice started that worker. Queued, delivered, waiting-for-approval and
answered results are distinct. A same-session outbox transition can be claimed
for commentary only once, with the original provider delegation ID; receipt
does not establish playback. Reconnect restores bounded results without
redispatching. Owner polling can persist late terminal outcomes after End,
without a provider connection or a model request.

**Result readback.** The bot's normal reply is posted in the launch thread as an
ordinary message (that is the normal-thread contract), and the bridge's one-second
result watcher also hands it to the live provider session: silent
`session.thinking.append` context, then `session.commentary.append` carrying the
original client delegation ID, which is how GPT-Live client delegation is answered
(LiveKit's `append_commentary`). In the 2026-11-13 02:10 UTC call the 1,206-character
answer was marked answered at 02:10:48.63, recorded as a result event at 02:10:48.74
and queued as correlated commentary at 02:10:49.31; the next assistant turn spoke
it. The voice's instructions now carry a `capability` statement built with the
startup briefing: in normal-thread mode it names the owning bot, its normal tools
and approvals and the one-request limit, and forbids claiming the voice has no
tools (the previous "no callable voice tools" wording made it answer "I don't have
any special tooling"). Once the call's one request is taken, the bridge appends a
standing rule (`session.instructions.append`): a submitted request's verified
result will be read here and further work needs a new call. If collection
expires before submission, a correlated commentary says no result will arrive.
A reconnected bridge applies the matching rule (submitted or never submitted),
and the startup capability counts any recorded signal as the used request. A
later delegation signal is answered at once with a correlated commentary saying
a new call is needed, instead of leaving the model waiting.

**Deployment gate satisfied, 23:38:39 UTC:** source #297 was deployed with policy
absent first; all old queue consumers exited before exact policy #170 merged.
A fresh policy-specific notice and quiet window preceded the web/bridge-only
reload. Both web replicas and bridge passed no-inference READ ONLY grant/scope
checks, including old-session no-upgrade and other owner/thread/bot denial.
Default remains off outside that exact reviewed scope. Code/config revocation
prevents new dispatch, not accepted work; hosted orchestration and physical
Android acceptance still require the user's actual observation.

### Dedicated voice page and progressive text (PR #293 deployed; device acceptance open)

Voice belongs to `/s/<launch-chat-id>/voice`, entered from that thread's top-bar options, not a modal or a new chat. The page keeps bot/thread identity, context and a return link visible. Merely opening the page must not activate a microphone: **Start call** and manual **Resume** are explicit. A single authenticated app-level call owner keeps an active call across ordinary in-app route/back navigation; leaving the voice page is not **End call**, and a different thread must not silently replace the active call. A continuing-call affordance must make background microphone/connection state visible. Browser/tab teardown cannot preserve a WebRTC connection; it is not the same guarantee as SPA navigation.

Thread options use the same purpose groups on desktop and mobile: Conversation
(voice and files), Model & reasoning, Thread data (JSON export), and Thread
lifecycle. Resolve is danger-styled: it archives the thread and stops queued/
running workers and their agents. A new message reopens the thread, not cancelled
work. Rename and pinned material remain in thread details. The mobile dialog contains keyboard focus and
returns it on Escape; the desktop panel focuses its first action and returns
focus to Options on Escape.

The browser attaches the assistant's audio track and starts playback as soon as
it is subscribed; audio output routing (`setSinkId`) is applied afterwards and
never delays or blocks playback. Browser playback is tracked as not started,
playing (an attached element fired `playing`), blocked (autoplay policy) or
failed. **Enable sound** stays visible while connected until playback is
confirmed playing. It runs `room.startAudio()` and plays the attached elements
inside the tap, on the existing call; it does not join a room, request
microphone permission, unmute capture, or confirm physical hearing. When inbound
audio energy arrives while playback is confirmed, the page suggests the volume
(Android uses the call volume) and speaker/Bluetooth route. If a final assistant
turn arrives on this connection, or inbound audio arrives, while playback is not
confirmed, the page shows an alert to tap Enable sound. The browser reports
`track_subscribed`, `playback_started`, `playback_blocked`/`playback_failed`
(error name only), `sound_enabled`, `audio_arriving` and `turn_unplayed` once per
connection to `POST /api/realtime-voice/sessions/{id}/client-audio` (owner only,
active sessions only, at most 40 per session, idempotent by report ID); they
appear as `client_audio` runtime events under Diagnostics. The bridge adds
`bridge_audio` events when RoomIO reports that the browser subscribed to the
agent's audio track and when the voice model generates its first assistant audio
frame (before RoomIO playout; not confirmed sent or played). Together these
separate "no audio produced", "browser never subscribed", "blocked or not
started in the browser" and "playing but inaudible on the device". None of them
proves the user heard anything.
Start call connects the room and enables the microphone; Mute/Unmute controls
capture separately. IDs, transport/provider state, browser playback details,
output configuration, route snapshots and runtime events are under
**Diagnostics**. Callback status, unavailable tools, failed work and approval
links remain outside the diagnostic disclosure; speech is not action approval.

**End call** stops the logical voice session/transport, not previously submitted thread work. It must not send thread cancellation, stop a worker or retract an outbox result. Standby, microphone mute and audio output remain separate controls. An ended call's finalized text/results stay readable.
While the ended session remains on its voice page, lightweight read-only observation continues at the slower three-second interval so a final already accepted by the bridge can appear after End's first response. Navigating away stops that ended-session observation unless delegated work remains pending; it never reopens a provider or requests another microphone session.

Progressive transcript drafts are bounded server-owned state, not finalized history or tool results. User partials retain their SDK item ID; assistant partials use the adapter's identified message-generation stream, because raw duplex transcript deltas do not carry a reliable final message ID. Finalization replaces the same speaker turn rather than creating a second bubble. Finalized text retains timestamp/provenance and delivery uncertainty; partial text must never be called heard or complete. Coalesced state publication limits per-token database work.

Displayed user/agent states come only from observed framework state-change events with timestamps. Missing transport/microphone evidence, standby, reconnect and teardown require unknown/disconnected rather than stale “listening.” Assistant-generated transcript text is not evidence of “thinking,” and queued background work is not evidence that a model/worker is running. Transcript follow-latest scroll must yield to manual reading; new text must not pull a reader away from an older turn.

### Explicit standby (off by default)

`REALTIME_VOICE_STANDBY_ENABLED` gates standby independently of the voice kill switch and owner allowlist. It defaults to false; enable it consistently for web and bridge only through an approved deployment. With the flag off, the current single-connection behavior remains unchanged. This branch does not itself enable or deploy the flag.

The default is **Stay connected**. **Standby while waiting** requests a pause; the bridge defers it until its actual Silero VAD has observed speech and its end, the pause grace and transcript drain have elapsed, all observed transcript fragments are final, and no request assembly or bridge job is in flight. Unknown speech state does not qualify. Detection has latency and can miss speech, so the boundary protects only speech already observed by the VAD; it is not a guarantee against lost speech.

The bridge confirms standby only after the provider session, HTTP client and room have closed and the request is still valid. If speech resumes before teardown commits, the bridge defers the request. Once irreversible teardown starts, the UI shows Disconnecting and warns that microphone input may become unavailable and speech may be cut off. If speech or transcript activity is detected during teardown, the session still enters resumable standby and records that the boundary audio is uncertain; it cannot establish how much speech, if any, was lost. The browser disconnects after confirmation. Already queued background work continues, and its IDs, results, bounded context and at-most-once announcement claims stay with the same logical session. **Resume** is manual; opening the panel does not wake a standby session. End remains terminal. Unexpected disconnects, errors and expiry end the session, not standby.

The original `bridge_started_at` is never reset. The 90-second deadline is wall-clock time from the first bridge claim, including time in standby; resuming receives only the remaining time. An expired standby session is ended on the next access, and its durable work remains readable. Resume cannot acquire another live bridge concurrently, cannot reset the one-delegation limit or budget, and does not enable SDK/provider retries. Provider teardown and billing can outlast the local deadline, so this is not a billing ceiling.

Session metadata exposes the observed standby and resume timestamps and last standby duration, when available. Bridge logs distinguish elapsed wall-clock time from provider-reported session usage seconds; missing provider usage remains unknown. Reconnect latency is wall-clock time from the resume request to the provider's `session.started` event, or unknown if that event is not observed; it is not first-audio or playback latency. These measurements do not establish input audio duration, silence savings, lost speech, playback or hearing. Finalized text, task references and results remain in Django history; the code does not store audio recordings. Standby is not inactivity detection, auto wake, browser wake detection or pre-roll: a disconnected microphone cannot be monitored by the server VAD.

**Kill switch.** Set `REALTIME_VOICE_ENABLED: "false"` in `ergonaut-config` (infra-octo) and restart web and the bridge (`kubectl --context octo -n ergonaut rollout restart deploy/web deploy/voice-bridge`); no image deploy is needed. With it off the web app hides voice and refuses tokens, and the bridge refuses every job. For an immediate stop before the commit lands, `kubectl --context octo -n ergonaut scale deploy/voice-bridge --replicas=0` leaves no agent to join a room; Flux restores the replica on its next reconcile (≤5 min), so commit the flag too. Persisted Django history and already queued bot work remain.

## Known gaps

Known gaps beyond the accepted basic phone audio/conversation: the third hosted synthetic attempt produced a real owner-scoped history search and same-session durable result, but delegated before the request ended and generated contradictory status after the answer. Lee's later phone session submitted no delegation. Hosted speech-boundary/transcription correctness, useful orchestration, Bluetooth routing, interruption and standby/resume remain unaccepted. Automatic voice compact state uses conservative extractive rules, not a semantic summarizer: unrecognized facts can fall outside its bounded restore window. User-reported activities are anchored to server receipt, not the physical action; client timestamps have unknown clock error. The bridge provides server-calculated time since reports as silent context; live response timing has not been verified. A durable claim permits at most one commentary attempt per result across reconnects. A crash after the claim can lose the spoken announcement, but the result remains in the session card; the claim does not indicate playback. GPT-Live's `append_commentary` returns no speech identifier, so it cannot be correlated to browser playback with the inspected adapter. No per-utterance browser acknowledgment is claimed. The model is instructed not to repeat restored results unless asked, but this is not a deterministic guarantee. With standby off, a reconnect after the bridge job ends is refused. End-to-end delegation usage/spend and provider billing are not capped by this prototype. Do not represent it as production-ready.
