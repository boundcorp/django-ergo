# PR #418 review fixes — draft only

This report supersedes the original 39b4b2c rollout/validation report. The
independent review and reproducers at `../model-pools-review/PR418_REVIEW.md`
and `tests/test_pr418_review.py` were read only and remain unchanged. #416 is
separate and unmerged. No deploy, merge, restart, live configuration, secret,
accounting, infrastructure change or paid provider call is authorized or done.

## Changes and remaining media decision

- **P1-2 / P3-2:** credential discovery caches successful YAML parses by device,
  inode, nanosecond mtime/ctime, size and a content digest (including symlink
  replacement and equal-metadata rewrites on coarse filesystems). Files are
  read to detect changes; unchanged YAML is not reparsed. Known
  aliases accumulate across edits/removal. YAML, Unicode and read errors warn
  without printing content, retain known aliases and restrict subprocesses to
  safe native fields. Unknown aliases cannot leak through a whole-ambient-env
  fallback; unrelated Claude/Codex subscription chats and Bash remain usable.
- **P2-1:** ordinary children deny provider-family components, configured aliases,
  arbitrary prefixed API keys, JSON service accounts and credential file paths.
  Examples covered: RYAN_MINIMAX_API_KEY, ANTHROPIC_STRUCTURED_API_KEY,
  GOOGLE_APPLICATION_CREDENTIALS_JSON, ADS_GOOGLE_SERVICE_ACCOUNT_JSON and
  GOOGLE_APPLICATION_CREDENTIALS. Native subscription OAuth/config survives;
  explicit provider aliases and private prefixes take precedence over keep names.
- **P1-1:** local_shell now raises a named startup error for denied `secrets`
  on every start/resume, before launching a child. It no longer silently drops
  requested media credentials or reports only their presence in the parent.
  `pass_env` and CLI overrides cannot restore provider credentials.
- **P2-2:** transcription mocks set explicit synthetic auth. The installed-Claude
  integration harness injects fixed fake auth only at its test-local final
  subprocess boundary, targeting its loopback FakeAnthropic server. No production
  bypass, provider approval or ambient paid credential inheritance was added.
  Local-shell offline lifecycle fixtures use OFFLINE_TOOL_TOKEN instead of a
  real provider credential name; media denial has separate regression coverage.
- **P3-1/3/4:** docs cover voice and embedding selectors, subprocess policy and
  media limitation. Fixed-argv ffmpeg, bot checkout git/reloading and CLI command
  children now receive scrubbed environments. Trusted CLI web/Celery service
  startup explicitly retains host credentials: these are intended API consumers,
  not coding children. `manage()` itself is in-process; generic `run()` defaults
  to scrubbed, with explicit `application=True` only at host-service call sites.
  A fake-only test covers that purpose boundary. GitSnapshot's merge-base already
  used git_environment() at this head. Preflight sops is scrubbed and missing
  selected voice keys use the existing `require()` diagnostic.

**Unresolved media policy, deployment blocker:** current ergo-bots `68b24ff`
`ads/bot.yaml` deliberately lists ELEVENLABS_API_KEY, SUME_API_KEY,
GEMINI_API_KEY and MINIMAX_API_KEY for general Claude/Codex jobs; Storytime lists
GEMINI_API_KEY, SUME_API_KEY, ELEVENLABS_API_KEY and RYAN_MINIMAX_BASE_URL. Ads
allows Bash; Storytime's brief instructions impose approved provider/model,
count and total USD caps. The existing AgentSpec carries no trusted media purpose
or per-job secret allowlist; `secrets` is plugin-wide for every job and resume.
Neither a key name nor an agent-authored brief distinguishes a coding subprocess
from an authorized media tool. A broad raw-key exception would restore ordinary
coding inference credentials and is therefore absent.

This draft takes the explicitly permitted fallback: affected jobs fail visibly
at startup rather than silently losing keys. Do not merge/deploy it over the
existing media workflows until the coordinator's skillbuilder config work and
a trusted per-job media-tool handoff are coordinated. Existing live workflows
remain untouched. Any future handoff must bind the actual approved media worker
purpose/current workflow to an explicit per-job allowlist and a separate tool
subprocess; it must exclude org OpenAI, private/protected keys and coding
inference. It must reuse existing media keys/caps, never create provider approval,
new budgets or reset reconciliation. This report requests no credential handling.
Environment scrubbing remains inheritance hygiene, not a same-UID sandbox.

## Current infrastructure evidence (read only, Nov 27)

Inspected infra-octo **origin/main e05f1c0059ba9b16793557e3e90c18c01298fa30**,
not its stale local HEAD 3a9d7bb. Current files:

- `infra-octo/kubernetes/apps/ergonaut/app/deployment.yaml`: seven application
  roles: web (and migrate init), celery, bot-tasks, runpod-maintenance, bots,
  beat, voice-bridge. Voice-bridge has one replica and **Recreate** strategy.
- `infra-octo/kubernetes/apps/ergonaut/app/configmap.yaml`: shared nonsecret
  selectors, `ERGONAUT_BOTS=/home/dev/p/boundcorp/ergo-bots`, mutable main
  upgrader with 300s polling. Bot checkout hot-pulls use
  ERGONAUT_BOTS_PULL_SECONDS (the key name exists in runtime Secret; its value
  was not read).
- Live Octo deployments match image
  `ghcr.io/boundcorp/ergonaut@sha256:59e92fcd35a02d1956f213cb971b7afe1b17c793785c5ac10fd2fe2613a6790a`.
  All seven application roles import broad `envFrom: ergonaut-runtime`.
  Live voice-bridge is Recreate. Redis is separate from these seven roles.
- Secret key names only: OPENAI_API_KEY exists, BOUNDCORP_ORG_OPENAI_KEY is absent;
  37 fields include the Gemini, Ryan MiniMax and Google JSON names above.
  Neither secret values nor pod environments were read. The ConfigMap has no
  VOICE_OPENAI_API_KEY_ENV selector.

`envcheck` was initially missing on PATH; `/home/dev/bin/envcheck` ran successfully.
It reported implicit Kubernetes context/readyz unavailable; the documented explicit
route `KUBECONFIG=~/.kube/clusters/octo kubectl --context octo -n ergonaut get ...`
succeeded for deployments, ConfigMap and Secret key names. No cluster is reported
unreachable. This environment check made only its normal read-only route probes.

## Exact credential proposal and coordinated future rollout

Proposed source: `infra-octo/kubernetes/apps/ergonaut/app/runtime-secret.sops.yaml`,
namespace ergonaut, Secret **ergonaut-runtime**, field **OPENAI_API_KEY**.
Proposed target: **same file, namespace and Secret**, field
**BOUNDCORP_ORG_OPENAI_KEY**. This is a description only: no material read,
copy, provisioning, migration, rotation or edit. Obtain separate approval for the
exact operation; retain the old source for consumers and rollback.

1. Review the code/tests and resolve the media boundary first. Merging consumer
   code itself triggers the upgrader and a fleet restart, including voice-bridge;
   it needs operational approval, announcement and a quiet window. Draft only now.
2. Separately approve/provision the target field before adding any non-optional
   secretKeyRef. A missing target would prevent startup; voice-bridge Recreate
   makes that a voice outage. Keep the old OPENAI_API_KEY throughout migration.
3. Against current infra origin/main, inventory every required unrelated secret
   before replacing broad envFrom with explicit refs. Add the org-key ref and
   VOICE_OPENAI_API_KEY_ENV selector to **web, celery, bots, bot-tasks and
   voice-bridge**: attachment transcription can run in all five, and the
   AUDIO_TRANSCRIBER default is computed at settings import. Inject nothing new
   into beat, runpod-maintenance or migrate init. Merely supplementing broad
   envFrom does not isolate secrets. Preserve all integrations/native subscriptions.
4. Roll and verify each named consumer role without inference: target variable
   name present, selected audio config enabled, subscription routing and child
   hygiene intact, unrelated integrations preserved. Include voice-bridge explicitly;
   code rollout can also restart beat/maintenance. Never restart devbox.
5. **#407 in the same coordinated window:** a selector rename changes transport
   identity, so old grants refuse the new identity. Independently attest the same
   OpenAI billing account; append assert_mapping for the new identity to the
   existing **openai** family and same account label. Re-authorize funding via
   the versioned approve_funding write, preserving the exact existing weekly
   cap, consent, period boundaries and expiry. This approves the renamed route,
   not fresh capacity. Keep spent amounts, held/unknown reservations, ledger
   history and reconciliation unchanged. Requeue refused paid work only after
   mapping and funding approval. Never create a new family, fresh $25/week,
   duplicate funds, extend budgets or reset accounting.
6. **Strictly last**, coordinator skillbuilder lands ergo-bots providers.yaml and
   design/bot.yaml selectors (separate repo, no edits here). The shared checkout
   hot-pulls without a restart, so merging this diff before every consumer role
   is ready would break OpenAI/design routes mid-rollout. Coordinate media
   allowlists/tool boundaries before activating the changed code as well.
7. Roll back ergo-bots selectors first, then infrastructure refs/selectors under
   approval. Retain the old credential field and all accounting/history/holds;
   keep child scrubbing in place until org injection is removed. No ledger reset.

## Validation

Results and exact commands are recorded in `artifacts/pr418-fixes/`; final
head/main results are filled below after the bounded runs complete. Both revisions
use identical pytest arguments, the same dependencies and a checked-in keyless
runner which removes provider/API/cloud credential variables before import.
Only loopback and Unix test sockets are allowed. The runner includes the installed
Claude binary in PATH so real-CLI coverage is exercised with fake upstream auth.
Main is the exact reviewed merge-base/origin/main **4e292f3**. Dedicated temporary
venvs bind each revision's sources, including isolated child imports; dependencies
are reused read only. No #416 source is imported from the dependency-only path.

Initial focused credential, local-shell, voice-guard and installed-Claude run:
**90 passed**, no skipped tests, including both installed CLI fake-upstream tests.
Additional cache-digest, transient read-recovery and fixture-isolation coverage
is recorded with the final results below.
The earlier focused attempt exposed seven legacy local-shell media-key fixture
assumptions and a missing safe CLI constant; those were corrected explicitly,
without ambient inheritance. The original review reproducer file was not edited.


Earlier full attempts (superseded, not a final green claim): the first head
library run had 3788 passed, 13 skipped and five failures. One admission
cancellation cleanup failure also reproduced in the identical main run; four
normal-environment assertions came from a synthetic broken-source test leaking
new process-wide cache/failure state through a fixture that reset only the old
registry fields. The fixture now resets all four registries, and its deliberately
malformed source is removed explicitly. A subsequent focused run exposed real
equal-metadata file rewrites on this filesystem; the cache now includes a content
digest and regression coverage. A second full invocation overlapped these
refinements and is superseded; only the final settled-source run counts below.


Final comparison (identical keyless/socket arguments; see
`artifacts/pr418-fixes/COMMANDS.txt` and `RESULTS.txt`):

| Check | Fixed head | Reviewed main 4e292f3 |
| --- | --- | --- |
| Library full | **3795 passed, 13 skipped, no failures** | 3753 passed, 13 skipped, 2 admission-cleanup failures |
| Ergonaut full | 701 passed, 6 skipped, 1 admission-cleanup failure | 697 passed, 6 skipped, 1 admission-cleanup failure |
| Ruff check / format | pass / 648 files | pass / 641 files |
| Library + API migration drift | No changes detected, both | No changes detected, both |

The final API failure is
`test_admission_drain.py::test_channel_deferral_and_cleanup[False]` (cleanup
observes `executions_active()` still true). Main fails the `[True]` variant
in its full run and targeted file rerun (14 passed, 1 failed). These share
the cleanup symptom in unchanged code; the differing parameter is explicit
and is not claimed as an identical baseline test failure. Main's library
failures are `test_direct_async_admission_waits_and_cancellation_releases`
and `test_background_consumer_waits_then_executes`; its targeted admission
file subsequently passed all 13 tests. No admission code/tests were changed.
The full API suite is therefore **not claimed green**. Earlier API head run
passed 701/6; an intermediate rerun had an inbox cleanup failure and a 15s
retired-flag subprocess timeout under concurrent suites (not a paid call).

The settled focused library regression suite passed **102 tests**, and the
voice/service-purpose boundary file passed **4 tests**. Unchanged independent
review controls passed **10** (three bug reproducers deselected, originals
preserved read only). All six original keyless regressions are covered with
synthetic auth and pass. Tests made no successful provider calls; the socket
guard also blocked a GitHub connection attempted by an unrelated API CLI fixture.


Final head API admission-file rerun: **15 passed** (20.04s), including both
channel-cleanup variants. The final full library completed in 479.91s with
**3795 passed / 13 skipped / zero failures**; final full API in 167.26s had
the single cleanup failure described above. The full main comparison ran in
519.32s (library) and 287.37s (API). These results supersede all intermediate
head runs. Current draft PR: https://github.com/boundcorp/django-ergo/pull/418.
No bot config, credentials, infrastructure, funding or running service changed;
only the draft branch code, tests, docs and evidence are pushed.
