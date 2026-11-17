# Selectable text-agent voice: offline evidence

November 17, 2026. Draft source only; no activation, merge, deployment, real-owner task, provider inference, room connection or device capture authorized/performed.

## Scope and implementation

- Baseline confirmed from actual checkout: `7433edf7422cd84a16c375b164c78482439a9e9c`.
- Native remains the default; the existing GPT-Live/Responses Assistant is retained. The dedicated voice page offers an explicit alternate selection before connection, fixed for the logical session/manual resume. Composer dictation Stop/edit versus Send-once is unchanged.
- Alternate: owner microphone only, Silero-finalized audio batch identity before STT, existing OpenAI transcription helper/settings/cost recording, one durable request through existing normal originating-thread `messaging.send` delivery, exact verified normal result through separately configured pinned SDK TTS. No LLM or native interpreter runs inside the alternate voice adapter.
- Existing normal thread identity/context/engine/tools/ACLs/action approvals and voice at-most-once tool/never-automatic-model-retry policy remain. Concurrent utterances retain separate durable roots and serialize through normal work, rather than the composer's transient merge/steer inbox. No authenticated interactive host-review principal is fabricated.
- Session/bridge/utterance receipt identity survives bounded restore-window eviction. Conflicting replay, stale claims, failed/late STT and revoked grants cannot admit new work. Audio interruption does not cancel accepted work. Root/child/result/speech identities persist; routine queued/delivered states remain silent.
- No fixed application call duration, cumulative request quota, new provider/key provisioning, wake feature or additional four-mode product work. Existing STT service rate and per-recording limits remain in force.

## Exercised verification

| Surface | Observed evidence |
| --- | --- |
| Backend native/alternate/dictation regressions | **206 passed** on the initial candidate; final revised alternate-only rerun **24 passed**, including model/voice eligibility, old unannounced outcomes outside both restore windows, approval-wait-to-terminal/root/child identities, End/publication race and UNKNOWN synthesis usage |
| Frontend production build | TypeScript + Vite passed; existing >500kB chunk warning remains |
| Frontend regressions | Full suite **393 passed**, 1,643 assertions; the voice/composer subset separately passed **107 cases**, 514 assertions, covering explicit alternate selection, unavailable spending gate, immutable active mode and existing recorder Stop/Send behavior. Existing React `act` warnings remain |
| Standalone actual installed SDK + disposable PostgreSQL + isolated Redis/Celery | Actual AgentSession/Silero consumed synthetic local PCM; **2 finalized STT batches**, **2 real broker-delivered normal tasks**, **1 actual OpenAI plugin HTTP/PCM fixture request**, **960 output samples** before interruption, **2 audio buffer clears**, verified second result after End |
| Actual alternate session disconnect branch | Real `text_voice_session`/AgentSession with offline Room boundary fixture ended on owner disconnect, closed media resources and left the accepted normal request queued; real broker delivery then completed and its verified result remained recoverable after disconnect |
| Final revised result-consumer runtime | Actual normal handler, disposable PostgreSQL and installed AgentSession/OpenAI TTS HTTP fixture: one normal request, one unannounced root selected, one speech attempt, zero pending outcomes afterward, complete 2,400-sample fixture PCM retained within 2,880 SDK output samples, original context/root identity and usage-only synthesis cost record preserved |
| Actual Chromium, 390×844 | Native selected initially; alternate displayed actionable missing-consent/config reason with disabled Start and explicit AI-generated-voice disclosure; approved offline fixture submitted exactly `{chat_id: disposable-chat, mode: text_agent}`, made one deliberately denied token request, hid the active-session selector and retained its mode; **0 capture calls, 0 peer constructions** |

These are offline parser/runtime/browser-state proofs, not real STT semantic accuracy, provider generation entitlement, physical speaker/microphone/Bluetooth behavior or hearing acceptance. Synthetic STT text and HTTP audio fixtures substitute only for external media/model transports; the normal database/queue/bot/tool/approval code is real. Scripted bot results use the repository's existing fake provider transport, never a real account's tasks.

Browser artifacts: [unavailable alternate](mobile-unavailable-mode.png) and
[explicit selected mode](mobile-selected-mode.png).

The first standalone harness accidentally retained Django Ergo's cached in-process runner after assigning plain settings; its second broker-hold assertion timed out although both normal tasks completed. Reloading `api_settings` in the disposable harness activated its intended isolated real broker. No product behavior was changed for that harness error. An initial backend whole-history row-count assertion also failed because normal orchestration pre-seeds history; the incidental assertion was removed, not re-pinned. An incomplete browser routing fixture was corrected before visual acceptance; no unrelated product change was made.
The final output fixture also removed an exact frame-count assumption: SDK segment
padding is permitted, while retention of the complete original PCM payload is the
verified invariant; no application audio trimming or suppression was introduced.

### Installed-SDK input-loss reproduction

The actual alternate session constructor and pinned SDK audio/VAD stream paths
were exercised with synthetic PCM and deterministic inference-probability fixtures,
not a microphone or a claim about real speech recognition. The earlier standalone
two-turn smoke above separately exercised actual Silero ONNX inference.

| Boundary | Before correction | After correction |
| --- | --- | --- |
| First-response user PCM reaches STT unchanged | false: substituted silence | true |
| Continuous synthetic utterance's final qualification retained | false | true |
| Finalized buffered audio | 60.5 seconds, truncated | 63.584 seconds, full supplied speech retained |

The model/voice and End/publication cases failed before their fixes; the expanded
restore-window/root-identity regression also failed before independent output
selection. All revised alternate regressions then passed. No paid transport ran.

## Actual providers, prices and activation prerequisites

- Existing native: LiveKit Agents/OpenAI/Silero `1.8.11`, OpenAI SDK `3.49.0`; `gpt-live-1` with same-connection Responses interpreter `gpt-5.6-luna`. Native interpreter consent does not approve separate STT/TTS costs.
- Existing STT defaults to `gpt-transcribe`; existing priced/eligible choices are `gpt-transcribe`, `gpt-4o-mini-transcribe`, `whisper-1` using the existing OpenAI key/helper. No new secret access route or provider was created.
- Pinned SDK-listed TTS choices: `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts`; shared legacy voices `alloy`, `ash`, `coral`, `echo`, `fable`, `onyx`, `nova`, `sage`, `shimmer`, with `ballad` accepted only for mini-TTS. New model and voice settings are empty by default; no invented default alias.
- [Published OpenAI pricing](https://developers.openai.com/api/docs/pricing), inspected read-only: existing default STT **$0.0045/input minute**; mini-STT estimated **$0.003/input minute**; Whisper **$0.006/input minute**; `tts-1` **$15/million characters**; `tts-1-hd` **$30/million characters**; mini-TTS **$0.60/million text input tokens + $12/million audio output tokens**. Normal bot/tool and LiveKit costs are additional; no maximum full-stack spend or remaining shared allowance is claimed.
- Before activation: existing realtime enablement/account allowlist, LiveKit credentials, existing OpenAI/STT configuration, reviewed exact normal-task and intentional follow-up authorization, explicit TTS model/voice, and **separate** exact originating chat/owner/bot `stt_tts_costs_approved: true` policy matching all three model/voice fields and its own approval reference. Web and bridge must match; old session grants do not silently change.
- TTS stores usage-only estimates where billing basis exists and UNKNOWN cost when mini-TTS token data is absent. No audio is persisted. SDK/model listing or read-only metadata access is not evidence of generation entitlement, credit or account billing capacity.
- Provider sunset: the [current Speech API guide](https://developers.openai.com/api/docs/guides/text-to-speech) deprecates these three TTS models with shutdown scheduled **January 6, 2027**. The proposed successor `gpt-realtime-2.1-mini` requires a different Realtime protocol, not a Speech API model alias. No automatic protocol substitution or additional spending authorization is included; revalidate this sunset before activation.

## Existing platform reconciliation

`envcheck` succeeded for both documented cluster routes and the documented SSH routes before availability reporting. Read-only Octo inspection found every Ergonaut application role using image `sha256:335e867f9dc5658f1b6cee8d430b142b195d2568bb790d815f8a34c24b175327`; web `ERGONAUT_VERSION` was `7433edf7422cd84a16c375b164c78482439a9e9c`. PR #350 merged as `520f53a6dac26eec9949700c3eb1b6ae29c8b8cf`, and that merge is an ancestor of the served source. Its silent routine-progress correction is therefore already in this baseline; no duplicate platform fix, rollout, restart or live policy change was made.

## Review and release boundary

Exact-head independent source review and CI status are tracked by the draft PR and coordinator handoff; runtime proof alone is not release authorization. Source remains default-off; activation requires the separate media spending consent/configuration above. Physical/provider semantic quality, echo rejection, real media latency, generation access, invoices and remaining LiveKit allowance remain unverified. Merge/deploy remain blocked on coordinator release checkpoint and fresh quiet notice; rollback disables the separate alternate gate/reverts draft source without cancelling accepted normal work.

Independent source reviews of initial `8326971` found five P2 issues despite
green CI: legacy-model/`ballad` eligibility, old unannounced roots outside bounded
restore windows (including root/child aliasing), ignored authoritative event
rejection after End, Silero's default continuous-audio truncation, and SDK warmup
silencing early barge-in STT input. The new model matrix, independent bounded
unclaimed-outcome query, rejected-event guard, recording-bound VAD buffering and
disabled SDK input suppression address those boundaries without call/request caps.
Failing-before authority regressions and actual-SDK audio-loss reproduction are
retained in the revision evidence; final-head reruns/review are tracked in the PR.
