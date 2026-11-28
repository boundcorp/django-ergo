# OpenAI-only subset of #418

Status: draft code/tests/docs only; no merge, deploy, restart, live credential read,
runtime secret/config/infra write, inference, funding write or ergo-bots edit.

## Provenance and scope

Exact base: **4e292f3d9d756815bc99dc847412886d25bc6989**, fetched origin/main,
initially clean worker checkout. Exact read-only source: **66f005dd41d0a8235cc6de461ea2d0a0a6aae6a7**
in `../selective-openai-key`. Extracted option 3 from
`../model-pools-review/PR418_REREVIEW.md`; this is an independent main-based
subset, not a merge of #418. Original #418 verified open/draft at the source SHA
using read-only GitHub metadata; left unchanged. New tested implementation head: **cb4002b1dcbbdd4a24814aece4ac0d71a3b29837**.
Publication branch: `leewardbound/openai-key-scope-only`; the final report/evidence
commit follows this code commit without changing its code or tests. The exact
publication head is recorded in draft PR metadata and the worker outcome.

Lee's Nov 27 22:16 authorization covers explicit org OpenAI selection and
selective exposure, not loss of Ads/Storytime media services or increased spend.
Ergonaut remains load-bearing. No operational authorization is inferred from
this implementation or from the environment map's default deploy permissions.

## Implementation

- `selected_openai_key` uses only the configured variable; absent/empty selected
  keys fail closed even with ambient/inline alternatives. Unset selectors retain
  legacy behavior. OpenAI chat, attachment transcription, voice enablement,
  realtime voice, TTS, local voice setup and preflight use explicit selectors.
- Embedding providers own independent OpenAI clients and endpoints. They do not
  mutate SDK module globals. Existing endpoint/private-inference guards remain;
  per-request guards inspect the actual instance endpoint.
- New child withholding is strictly `OPENAI_API_KEY`,
  `BOUNDCORP_ORG_OPENAI_KEY` and explicit OpenAI consumer aliases. Claude/Codex
  final launch/catalog boundaries recheck after overrides. Local-shell rebuilds
  and scrubs ordinary start/resume environments after `pass_env`/`secrets`.
  Existing Bash/tool scrub points use the same narrow policy. Fixed-argv media,
  git reload and preflight subprocesses use scrubbed env; trusted host web and
  Celery service launches retain application credentials.
- Alias collection uses parsed OpenAI bot/provider/model selectors, consumer
  selection calls and the known voice/chat/embedding settings. Disabled OpenAI
  providers are included. Names are validated identifiers, max 256 characters,
  stored without values, and retained across process-local reloads under a lock.
  Traversal is bounded to those schema fields and explicit model entries; no
  recursive arbitrary-config walk, YAML-source registry, whole-repo/file scan,
  discovery cache, suffix classification or global minimal-env fallback exists.
  Raw settings inspection avoids importing a malformed unrelated provider.
- Main's `is_protected_env`, default FIREWORKS prefix, configurable extra prefix
  rules and explicitly confined metered launch policy are preserved. Native
  subscription auth handling remains. No org OpenAI secret was added to any
  real local-shell configuration.
- Existing media secret lists remain compatible. No GEMINI/MINIMAX/GCP refusal,
  new provider-family stripping or changed media budget policy is included.

## Narrow boundary and remaining limitations

**Existing general-agent media credential exposure remains UNRESOLVED.** This
subset preserves main's Ads/Storytime service behavior; it does not solve the
separate trusted media-executor/grant problem and does not certify a hard
sandbox, spending cap, billing identity, live authentication or live integration.
Same-UID/proc/file access and shell startup profiles can reacquire credentials
beyond the inherited environment. A local Bash probe demonstrated profile
reintroduction of an OpenAI variable (names only were printed, no provider call);
its corrected boundary test captures env before profile execution. That result
is retained, not recast as whole-shell isolation.

New selectors become known through normal successful config parsing/loading or
the OpenAI consumer itself. Unloaded file edits and malformed unrelated files do
not trigger global discovery. Retired loaded aliases remain withheld until
process replacement; name-only collection is process-local. Renaming a key with
broad envFrom still present does not provide per-role isolation. An unrelated
media name is never inferred to be OpenAI just from its suffix/name family; an
explicit OpenAI selector can deliberately classify any valid identifier.

## Fake compatibility and independent controls

Exact lists verified read-only in ergo-bots origin/main
**68b24ff6ab5954a8f48ed95209cec04b2db15f81**:

- Ads: `ELEVENLABS_API_KEY`, `SUME_API_KEY`, `GEMINI_API_KEY`, `MINIMAX_API_KEY`.
- Storytime: `GEMINI_API_KEY`, `SUME_API_KEY`, `ELEVENLABS_API_KEY`,
  `RYAN_MINIMAX_BASE_URL`.

The identical compatibility module runs on head and exact main with synthetic
`ctx.secret` results and a local Python supervisor child replacing any actual
CLI/provider. Both lists, both configured agents, start and resume are covered;
media values arrive unchanged and OpenAI keys remain absent. Additional tests
attempt org/alias restoration via both pass_env and secrets. The controls do
not run actual media CLIs or send provider traffic.

Independent rereview selector, selected-auth MockTransport, concurrent 80-request
embedding isolation, and weekly-budget/mapping/hold controls are retained from
`../model-pools-review/tests/test_pr418_rereview.py`. Broad-provider/malformed-file
minimal-env/refusal assertions are intentionally excluded because that behavior
is outside this subset. OpenAI override/grandchild/resume tests are adapted to
the narrow policy; final CLI/catalog and Bash boundary tests are added.

## Validation

Commands, keyless runner and full raw output: `artifacts/openai-only/`.
Tests use synthetic credentials, MockTransport/local fake endpoints and
pytest-socket loopback/Unix restrictions. This is Python test egress control, not
an OS sandbox attestation for arbitrary subprocesses. Source-bound Python 3.12
venvs use identical read-only dependency trees and exact matching checkout paths
first. Base is a fresh detached `/tmp/openai-only-base`, clean for full collection.
Four full runs use identical `-q -n 6 --tb=short --disable-socket
--allow-unix-socket --allow-hosts=127.0.0.1,::1,localhost -p no:cacheprovider`
within each head/base suite pair. No full-suite failure is hidden by a later
focused pass.

| Check | OpenAI-only head | Exact main 4e292f3 |
| --- | --- | --- |
| Full library | **1 failed, 3788 passed, 13 skipped** (495.80 s) | **3755 passed, 13 skipped** (542.60 s) |
| Full API | **4 failed, 698 passed, 6 skipped** (309.17 s) | **3 failed, 695 passed, 6 skipped** (318.68 s) |
| Identical fake Ads/Storytime compatibility controls | **8 passed** (22.37 s) | **8 passed** (25.98 s) |
| Django system check | **pass** | **pass** |
| Migration drift (`makemigrations --check --dry-run`) | **no changes** | **no changes** |
| Ruff lint / format | **pass**, 650 files formatted | not rerun |
| Diff whitespace | **pass** | n/a |
| Common focused CLI/local-shell/public-guard/admission files (`-n 2`) | **76 passed** (28.75 s) | **1 failed, 75 passed** (30.55 s) |
| Library admission file, exact serial rerun | **13 passed** (16.04 s) | **13 passed** (15.03 s) |
| API admission/broker files, serial (head adds voice controls) | **21 passed, 1 skipped** (43.84 s) | **1 failed, 16 passed, 1 skipped** (41.60 s) |
| Selected consumer/independent/media/guard/embedding focus (`-n 4`) | **60 passed** (17.79 s) | new head tests |
| Final OpenAI alias/CLI/Bash/resume boundary controls, serial | **14 passed** (0.78 s) | new head tests |

Library full-run failure (not a green suite):

- **Head only in full runs:**
  `tests/test_admission_paths.py::test_background_consumer_waits_then_executes`
  failed line 230 because `drain.executions_active()` remained true at cleanup.
  Exact main passed its full run. The identical test **failed on main** in the
  separate common focused run; head passed that focused run. Exact serial reruns
  of the full admission file passed all 13 tests on both revisions. This is
  independent evidence of a baseline timing/cleanup sensitivity, but does not
  erase this head-only full-run failure or prove the subset unrelated.

API full-run failures (not a green suite):

- Both head and main failed
  `ergonaut/apps/bots/tests/test_admission_drain.py::test_channel_deferral_and_cleanup[False]`
  at line 100, `assert not drain.executions_active()`.
- **Head only** also failed that exact test's `[True]` parameter with the same
  assertion. Main passed `[True]` in this full run. The admission/drain code and
  tests are unchanged; that alone does not prove the subset unrelated.
- Both revisions exceeded the unchanged 15-second subprocess limit in
  `ergonaut/api/tests/test_realtime_voice_broker.py::test_retired_paid_flag_cannot_launch_the_native_cost_profile`
  for `[ergonaut.realtime_voice_preflight]` and `[ergonaut.realtime_voice_local]`.
  Argument rejection happens before credential lookup/decrypt/provider calls.
  Concurrent import load is a plausible cause, not a proven root cause.
- API logs retain local database-teardown session warnings and a client fixture's
  attempted GitHub connection, blocked by pytest-socket. No successful external
  provider call followed from these failures.

Serial reruns of the exact admission-drain and retired-flag files (head also
includes the new voice credential file): **head 21 passed, 1 skipped** in 43.84 s;
**main 1 failed, 16 passed, 1 skipped** in 41.60 s. Main again failed admission
cleanup `[False]`; both retired-flag variants and head-only `[True]` passed on
both revisions. Assertions/timeouts were unchanged. These outcomes support a
cleanup/timing sensitivity hypothesis, not a proven explanation; they do not
erase the head-only full-run failure.

`scope-check.txt` verifies unchanged ASTs for main's Fireworks prefix/protection
functions and confined local-shell env, byte-identical Claude/Google API engines,
registry and plugin constructors, and no generalized credential-discovery module,
infra or migration changes. Implementation commit hooks all passed. The full
baseline checkout stayed clean, and the source #418 checkout stayed at its
original SHA with no worker edits.

Initial focused run: 1 failed/59 passed because collection imported an unrelated
provider class; corrected production collector to inspect raw known settings,
then 60 passed. Additional boundary run: 1 failed/13 passed due Bash login-profile
reintroduction; narrowed the test to the launch boundary and documented the
limitation, then all 14 passed. Neither initial failure is omitted.

Read `~/ENVIRONMENT.md` first. `envcheck` was absent on PATH (127); documented
`/home/dev/bin/envcheck` succeeded, retained in `artifacts/openai-only/envcheck.txt`.
Its implicit Kubernetes probes are not an availability determination. This work
did not need cluster access and makes no unreachable-host/cluster assertion.

## Coordinated future alias migration/account mapping; no new capacity

This is a proposed later operational plan, not work performed or newly approved:

1. Review this independent subset and obtain the required operational approval
   before merge/rollout. Mutable main can trigger fleet upgrades; voice-bridge
   is single-replica Recreate. No merge/restart is part of this task.
2. Under separate exact approval, provision the proposed new field
   `BOUNDCORP_ORG_OPENAI_KEY` in the same
   `infra-octo/kubernetes/apps/ergonaut/app/runtime-secret.sops.yaml`, namespace
   ergonaut, Secret `ergonaut-runtime`, using existing `OPENAI_API_KEY` only after
   independently confirming the intended billing account. Preserve the old
   field for remaining consumers and rollback. No secret value/presence was
   inspected here; missing nonoptional refs would prevent startup.
3. Inventory unrelated integrations before replacing broad envFrom. Selectively
   inject org key and voice selector into web, celery, bots, bot-tasks and
   voice-bridge (all five can transcribe). Add no new org key to beat,
   runpod-maintenance or migrate init. Seven application roles must remain
   healthy; migrations run with web init. Preserve native subscriptions and
   existing media integrations; merely adding refs alongside broad envFrom
   does not achieve selective exposure.
4. Verify each required consumer without inference and validate unrelated
   integrations before changing routing. A config-name check alone is not live
   account verification or security isolation certification.
5. In the same approved accounting window, append the renamed transport
   identity to the existing **openai** family and independently attested same
   account label. Versioned funding reauthorization must preserve the existing
   exact weekly cap, consent, period/expiry, spent usage, held/unknown
   reservations and reconciliation. Old grants must refuse the new identity
   until mapping/authorization completes. No new family, duplicate funds,
   allowance increase, reset, expiry extension or provider is authorized.
6. **Strictly last**, coordinator uses skillbuilder for separate ergo-bots
   provider/design selectors after every consumer is ready: its shared checkout
   hot-pulls without restart. Worker made no ergo-bots changes. Requeue refused
   work only after the coordinated mapping/reauthorization is complete.
7. If rollback is approved, roll back bot selectors first, then runtime
   selectors/refs, preserving old credentials, all usage/holds and audit history.
   Keep the narrow child hygiene boundary. A separately reviewed media executor
   tied to trusted per-job purposes and existing limits remains future work;
   this subset neither disables those workflows nor grants them fresh spend.
