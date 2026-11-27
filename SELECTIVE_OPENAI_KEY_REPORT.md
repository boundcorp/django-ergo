# Selective OpenAI credentials — draft code and rollout proposal

Base: `4e292f3d9d756815bc99dc847412886d25bc6989`. This branch finishes the predecessor's uncommitted work. It is for draft review only: no merge, deployment, restart, live configuration, credential copying/provisioning/migration, infrastructure apply, funding, or paid provider request is authorized or performed by this worker. PR #416 remains outside this change and must remain unmerged with live paid launches refused. The conditional $1 total OpenAI approval within $25/week remains unspent by this task; this draft grants no launch or spending authority.

The inherited read-only audit is preserved at `docs/artifacts/ergonaut-openai-key-audit.md`. Its deployment observations are dated evidence, not a fresh assertion of live state. Read-only local checkout verification for this proposal used ergo-bots `68b24ff6ab5954a8f48ed95209cec04b2db15f81` and infra-octo `3a9d7bb19d46759b34c05896c0211fb1a063386f`; neither repository was edited. Remote Orca devbox is a separate pod/namespace, not the Ergonaut runtime. No credential stores were inspected or changed.

## Implemented consumer behavior and role matrix

| Consumer | Credential selection | Potential runtime roles (verify actual call paths before injection) |
| --- | --- | --- |
| OpenAI chat API | Engine `api_key_env`; explicit name overrides inline `api_key` and missing/empty value raises even with ambient `OPENAI_API_KEY`; each engine has its own AsyncOpenAI | bots, bot-tasks, web and celery where they execute normal chat/background work |
| Attachment/audio transcription | `DJANGO_ERGO["VOICE_OPENAI_API_KEY_ENV"]`; explicit per-call AsyncOpenAI key | web composer; bots/bot-tasks/celery if attachments are transcribed there; voice-bridge for text mode |
| Composer enablement | Same voice selector; absent configured key disables endpoint without generic fallback | web |
| GPTLive realtime | Explicit selected voice key passed to each LiveKit GPTLiveModel | voice-bridge |
| Text-agent speech | Explicit selected voice key passed to each LiveKit TTS; recognizer uses the same transcription helper | voice-bridge |
| Embeddings | `DJANGO_ERGO["EMBEDDING_PROVIDER_CONFIG"]["api_key_env"]`; each provider owns an OpenAI client, timeout, public base URL; no SDK module-global mutation | usually indexing/background celery, other roles only if verified; Hindsight is a separate service, not established as an OpenAI consumer |
| Historical local voice/preflight utilities | `VOICE_OPENAI_API_KEY_ENV`, default legacy name; local helper requires the same named Secret field, never silently copies the old field into a new alias | explicitly operator-run isolated process only; scripts not run by this task |
| Ordinary local agent children | All configured provider selectors, known SDK provider credentials, org/generic aliases and protected prefixes removed | local_shell (initial/resume), Claude, Codex, bash, local Orca CLI including omp launch/helpers, search CLI, and other callers of scrubbed_env |
| Remote Orca workers | Local Orca CLI env scrubbed; existing launch transport does not forward Ergonaut provider credentials | separate devbox; leave native credential stores/subscriptions untouched |
| Beat, migration init, runpod-maintenance, standalone ficsit-drive-claude | No established need for the org OpenAI credential | propose no injection after dependency inventory; audit broad envFrom first |

Unconfigured chat retains its existing SDK defaults. Unconfigured voice/embeddings retain `OPENAI_API_KEY`, and embeddings still accept legacy inline `api_key`. No org key is forced globally. `BOUNDCORP_ORG_OPENAI_KEY` is only an explicit selector value; configuring a missing name fails closed. Empty/unconfigured selectors intentionally retain legacy behavior.

Ergonaut exposes the voice selector via `VOICE_OPENAI_API_KEY_ENV` in project settings and uses it for attachment transcriber setup. Library hosts set the DJANGO_ERGO value directly. Example opt-in settings (proposal, not activated):

```python
DJANGO_ERGO = {
    # Merge with the host's existing settings; do not replace unrelated entries.
    "VOICE_OPENAI_API_KEY_ENV": "BOUNDCORP_ORG_OPENAI_KEY",
    "EMBEDDING_PROVIDER_CONFIG": {
        "api_key_env": "BOUNDCORP_ORG_OPENAI_KEY",
        "model": "text-embedding-3-small",
    },
}
```

## Launcher and credential-name audit

`provider_credentials.py` records names only, never values. Provider and bot definition parsing, plugin config construction, and explicit OpenAI selection register arbitrary `api_key_env`, `key_env`, and plugin `*_api_key_env` aliases. DJANGO_ERGO settings are checked at launch, before a client has to be initialized. Registry construction registers every bot source before loading the first bot, so ordering does not expose another bot's credential. Bot/provider YAML files are reread before each scrub: changed files and aliases are covered even by stale registries. The union of previously seen names is retained for the process lifetime across reloads; unreadable/malformed existing registered files block child launch rather than passing an incomplete denylist. Removed files retain their old names in the denylist and are checked again if recreated, so deleting a retired bot does not block unrelated launches. Operators must repair/reload unreadable or malformed existing configuration if that fail-closed behavior blocks work. Symlink source paths are retained so replacing a config symlink is also rechecked.

Existing `scrubbed_env` launch sites include bash, Orca run/status/launch/resume/read/scanner helpers, browser/kubectl helpers and management processes. Claude and Codex call it after merging overrides, so overrides cannot reintroduce keys. Search's Claude subprocess uses Claude child_env. local_shell now scrubs after both pass_env and explicitly listed secrets; its standard-library runner and agent descendants inherit this already scrubbed environment, including resumed runs. Existing narrowly authorized metered confinement is kept separate: the environment is scrubbed before its explicitly selected credential is added under native CLI names. This code does not enable that route or relax its approval/refusal gates.

The installed omp wrapper and bundled provider source were audited read-only (no harness or auth-store access); additional LLM/search API-key names and cloud token-file selectors from that source are included, while native OAuth tokens remain available. Known ambient SDK credentials cover OpenAI/Codex, Anthropic, Gemini/Google, Azure OpenAI, AWS Bedrock, Groq, Mistral, Cohere, OpenRouter, DeepSeek, xAI, Cerebras, Together, Fireworks, Hugging Face, and other common SDK names. Existing protected prefixes remain additive. HOME and native subscription authentication (including Claude OAuth) are preserved; provider API keys and cloud routing switches are removed. Omp remains subscription-only under its existing host admission checks; no auth database or devbox configuration is changed. The local Orca process and its helpers are scrubbed, but this does not attest remote credential files or an adversarial boundary.

Environment scrubbing is defense in depth against accidental inheritance. Same-UID code can potentially inspect proc, parent environments, mounted files, shared homes or credential stores, and arbitrary shell startup files may add credentials independently. This change makes no claim of sandbox isolation, proc isolation, egress enforcement or safe live paid-agent execution. No new infrastructure sandbox is proposed in scope.

## Precise ergo-bots proposal — coordinator through skillbuilder only

Verified local paths and selectors; do not apply directly from this worker:

```diff
--- a/providers.yaml
+++ b/providers.yaml
@@
   openai:
     type: openai
-    api_key_env: OPENAI_API_KEY
+    api_key_env: BOUNDCORP_ORG_OPENAI_KEY
--- a/design/bot.yaml
+++ b/design/bot.yaml
@@
-engine: {type: openai, api_key_env: OPENAI_API_KEY, config: {model: gpt-6-luna}}
+engine: {type: openai, api_key_env: BOUNDCORP_ORG_OPENAI_KEY, config: {model: gpt-6-luna}}
```

Leave `default: auto/medium`, subscription-only tiers, Claude/ChatGPT CLI providers and xai-oauth unchanged. Coordinator must route any eventual bot config change through skillbuilder, with exact proposed diff and approval gates. These changes are not included in this branch.

## Infrastructure proposal — nonsecret, not applied

Source of the intended credential: `infra-octo/kubernetes/apps/ergonaut/app/runtime-secret.sops.yaml`, namespace `ergonaut`, Secret `ergonaut-runtime`, field **`OPENAI_API_KEY`**. Intended target: same Secret, field **`BOUNDCORP_ORG_OPENAI_KEY`**. This describes a proposed future transition of the existing credential, not permission to read/copy/provision/migrate/rotate/edit secret material. Present that exact source and destination to Lee and obtain separate explicit approval before any credential operation. Do not edit SOPS in this task.

Nonsecret manifest paths: `infra-octo/kubernetes/apps/ergonaut/app/deployment.yaml` (web + migrate init, celery, bot-tasks, bots, beat); `configmap.yaml` for shared runtime nonsecret selectors; inspect repository/live definitions of voice-bridge, runpod-maintenance and the standalone drive before proposing their concrete edits. The audit records broad `envFrom.secretRef: ergonaut-runtime`, which must be replaced by individual refs for **every required secret**, not just supplemented by the following entry:

```yaml
# Proposal only, in each verified OpenAI-consuming application container:
env:
  - name: BOUNDCORP_ORG_OPENAI_KEY
    valueFrom:
      secretKeyRef:
        name: ergonaut-runtime
        key: BOUNDCORP_ORG_OPENAI_KEY
  - name: VOICE_OPENAI_API_KEY_ENV  # only voice/transcription consumers
    value: BOUNDCORP_ORG_OPENAI_KEY
# Retain configMapRef imports as appropriate. Replace broad secretRef imports
# with explicit refs for all inventoried unrelated secrets before claiming isolation.
```

Candidate injection: web (composer), bots/bot-tasks (API chat/audio), celery only when background/embedding paths are verified, voice-bridge (realtime/TTS/STT). Propose no org-key injection to beat, migration init, maintenance or drive until a concrete consumer is established. Inventory DATABASE_URL, Django signing, queue/storage, LiveKit, integration/plugin/provider secrets and all init requirements separately; incomplete inventory is a rollout blocker, not license to drop unrelated secrets. Broad envFrom in any affected container continues exposure even after a selective ref is added. Native subscriptions and devbox are outside this credential transition.

## Backward-compatible staged rollout, rollback and approval gates

1. Review this draft's code/tests and report. No merge or deployment under this task. Recheck current running versions and consumer/role inventory before authorizing a future rollout.
2. Independently attest the OpenAI account/project binding of the existing key. Environment names are transport selectors, not new accounts or budgets. `ProviderAccountMapping` is append-only and transport identity includes api_key_env: a rename creates a distinct transport identity, so an explicitly audited mapping must associate old/new selectors with the **same existing ProviderBudgetFamily and account label**. Preserve weekly consent, period boundaries, spend history, held/unknown reservations and the original shared cap; do not reset or duplicate funds. No mappings or ledger rows were changed here.
3. Obtain separate explicit approvals for the exact credential source/target operation, concrete nonsecret infrastructure diff, identity mapping, skillbuilder bot config diff, code merge/deploy and each restart/window. Existing spending consent is conditional and does not approve these operations or #416 activation.
4. If separately approved, first deliver compatible consumer code while keeping legacy selectors; provision the approved target field while retaining the approved old source for rollback. Then switch only intended consumers/settings to the new explicit selector. Absence of the target must fail closed. Do not replace the shared generic key globally.
5. Replace broad secret imports role by role after complete dependency inventory; selectively inject the org credential only where needed. Verify configuration and fake/no-spend checks. Deployment currently follows mutable main automatically: merging is itself a live-change decision, so draft status is deliberate.
6. Announce and arrange a quiet window for authorized runtime changes/restarts of web, celery, bot-tasks, bots, beat and any voice/other affected workload. Never restart devbox. Verify composer enablement, key-name presence only, subscription routing, child env hygiene, durable work, unrelated integrations and preserved accounting without provider requests. Paid requests require a separate applicable approval and safe execution boundary; never enable live paid agents on this draft's strength.
7. Roll back consumer selectors and individual refs to the previously approved source plus compatible code, under separate operational approval; preserve accounting mappings/history/holds. Retain old source until verified rollback and all consumers are migrated. Reverting code before removing org injection can reintroduce inherited aliases, so maintain scrubbing or remove injection first under the approved plan. Never erase ledger history or create a new weekly budget to make rollback easier.

## Validation

Final verification uses dedicated temporary environments pinned to this checkout, including isolated (`python -I`) subprocess imports. Dependencies are reused read-only from the existing library/voice environments; their editable source bindings are not used. The guarded suites use pytest-socket to permit only loopback and Unix sockets. Real OpenAI tests are deselected. CI installs the checkout normally; the temporary .pth setup is local test plumbing, not an application configuration change.

Final results (the final alias-list, removed/recreated-source behavior and direct-provider registration refinements were also checked by the focused reruns below):

- Library full suite: **3778 passed, 13 skipped** in 396.44s under the socket guard, with no failures.
- Ergonaut full suite: **701 passed, 6 skipped** in 139.22s with the installed LiveKit 1.8.11/OpenAI 3.49.0 bridge SDK and socket guard. Existing warnings include fixture signing-key length, factory deprecations, open test DB sessions at teardown, and a blocked GitHub connection in a CLI fixture; no suite failures.
- Focused library client/launcher/provider suite: **204 passed**; final other-provider/field subset **117 passed, 3 skipped, 2 deselected**; final launchers/CLI subset **75 passed, 2 skipped**; final environment/reload/subscription checks **21 passed**. These overlap and must not be summed as unique coverage.
- Actual installed voice constructors: **3 passed**, exercising explicit realtime/TTS instance keys, composer disablement, and missing custom variables despite an ambient key. These are also included in the full Ergonaut count.
- Library/embedding/analysis correction subset: **39 passed, 2 skipped, 2 deselected**. Exact unchanged-base analysis log-isolation subset: **14 passed**.
- Repository-wide `ruff check .`, `ruff format --check .` and `git diff --check`: passed.

The first library full attempt was interrupted after an xdist stall (3634 passed, 13 skipped, 7 failures). Six analysis log-isolation failures came from isolated child imports resolving a different checkout through the reused environment's editable install; dedicated import environments resolved them, and the same 14-test file passed on the exact unchanged base. The seventh failure was an old embedding mock targeting the removed module-global resource: it allowed one request using the fake literal `test-placeholder-not-a-credential`, rejected with HTTP 401. No valid credential or successful paid inference was used; mocks in both the field tests and shared helpers now target the instance resource class, and the corrected full run has a socket guard. This failed attempt is not a clean baseline or a final pass.

The first Ergonaut full run had 696 passed, 6 skipped and two failures (admission-drain cleanup timing, and a retired-flag subprocess timeout). Both affected files passed on branch rerun and on the exact unchanged base (**17 passed, 1 skipped** on base); these were non-reproducing full-run failures, not demonstrated persistent baseline failures. The subsequent dedicated-environment full run above passed all 701 tests.

All new provider tests use fake keys and HTTP MockTransport or SDK constructors without sessions. No live credentials, funded inference, deployment, configuration activation or secret operations were used. The preserved audit is historical evidence, not fresh operational verification. The final pushed SHA and draft PR are supplied in the authenticated worker outcome (a report cannot embed its own commit hash).


Reproduction commands (temporary environments described above; pytest-socket is a test-only dependency):

```sh
ERGO_TEST_ISOLATED=1 /tmp/selective-openai-test-venv/bin/python -m pytest tests -q --tb=short -m 'not openai_real' -n 4 --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1,localhost
# From ergonaut/:
ERGO_TEST_ISOLATED=1 /tmp/selective-voice-test-venv/bin/python -m pytest -c pyproject.toml -q -n 4 --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1,localhost
```
