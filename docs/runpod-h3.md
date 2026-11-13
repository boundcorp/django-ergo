# Frozen MiniMax H3 six-shot worker

The native hosted integration is the [RunPod plugin](runpod.md), whose real
`runpod:watch` Worker owns polling and whose independent Ergonaut maintenance
consumer owns cleanup. This guide also retains the standalone validation/operator
commands that supply its reused H3 primitives; do not substitute those commands
or a temporary Kubernetes Job for the hosted native Worker.

No deployment or rental authorization is implied. Keep the controller/export
directory **off the rented Pod** on the durable rental host, and never bake or
relocate provider credentials into an image.

## Exact runtime decision

- Official `MiniMaxAI/MiniMax-H3` revision
  `42ed227ee7df40d41602854ae760620d6eb651fe`, **FL2VA partition only**.
- SGLang source `2511743bd784e69e5a81ca3d926a000711dae4ab`.
- `task=fl2va`, first-frame image keyframe at frame 0. This is not Ref2VA:
  that partition is for arbitrary image/video/audio references. We retain the
  approved narration in postassembly, not as a request for generated dialogue.
- Record the approximate **`kitchen_int8`** linear precision, exact FA attention,
  zero resident DiT layers, DiT/text-encoder streaming, and eager execution.
  Do not use newer `convrot_int8` spelling with this older pin. No Turbo LoRA,
  Spectrum, approximate attention, prompt rewriting, or text-only fallback.
- Proposed host: one **RTX 6000 Ada 48 GB, 167 GB RAM, 200 GB ephemeral
  container disk**, no optional/network volume. This is a capacity-oriented
  proposal, **not measured H3 image-conditioned fit or throughput**.
- Image recipe: `examples/runpod-h3/Dockerfile`, including exact SGLang checkout,
  Python 3.12, Rust 1.93.0, uv 0.13.8, `comfy-kitchen` 0.2.41,
  HF client 1.28.0 and ffmpeg/ffprobe. The CUDA13 amd64 base is digest-pinned.
  Resolve the engine and HF client together: separately downgrading HF can leave
  an incompatible `datasets` installation. The recipe requires `uv pip check`
  before recording `/opt/h3-environment.txt`.
  Local CPU build/import/CLI/schema validation passed without weights or GPU;
  conditioned fit, kernel execution and cold runtime are **not** measured.
  Rebuilds still resolve transitive dependencies; retain the resulting immutable
  image and its full package freeze rather than assuming bit-identical rebuilds.

Build before starting the rental clock:

```sh
docker build -f examples/runpod-h3/Dockerfile \
  -t ergo-h3:sglang-2511743-kitchen-int8 .
docker image inspect ergo-h3:sglang-2511743-kitchen-int8 --format '{{.Id}}'
```

The local `.Id` is an image **configuration** digest, not a registry manifest
digest. A provider pull requires a named registry manifest digest and a proven
delivery route; a locally tagged image alone is not pullable.

The preserved image is public at
`ghcr.io/boundcorp/ergo-h3-runtime@sha256:7b08f5d6b56988afeab528c9524ded9052a6739df0899768294134afe1453f8b`.
Its manifest config digest is
`sha256:74ae80e663556a495683e8fdae6cec1981e4d48b5e237902dc4880f28b2c2f5c`.
Its embedded launcher predates the partition-path fix below: use the native SSH
entrypoint override, which delivers the corrected launcher at runtime. No image
rebuild or retag is needed, and a published image is not GPU execution evidence.

Long source builds compile native Rust extensions; use an isolated bounded
builder and preserve verbose logs/cache rather than repeatedly cancelling short
attempts. CPU validation used2CPU/6GiB RAM, disk reserve/growth monitoring and
a one-hour build deadline, not a GPU or model download.

Image startup requires `ERGO_DEADLINE` equal to the ledger's **existing absolute
Unix deadline**, established before creation; container restart never resets it.
The canonical `runpod/h3_launcher.py`, delivered by the native SSH entrypoint
override and copied by new image builds, downloads only the pinned FL2VA files,
starts a loopback-only SGLang service, records timings in
`/workspace/h3-timings.json` and publishes fingerprint-bound state in
`/workspace/h3-launch-state.json`. It exits immediately if download or server
fails; the Worker observes the failed marker and requests independent cleanup.
SGLang stderr is retained privately in `/workspace/h3-server-stderr.log` rather
than depending on post-deletion provider log retention. Terminal state includes
its last64KiB (dropping a partial leading line and redacting configured HF tokens)
and observed timings; the native Worker durably exports `launcher-failure.json`
before requesting teardown. `ergo_runpod_collect` exposes this diagnostic even
when no shot was submitted. Diagnostic persistence does not extend the deadline,
wait for log streaming, or block stop if storage fails.
The active subprocess group is killed at deadline minus 120 seconds,
even during download/load/generation. `compile_seconds=0` means Torch compile is
disabled; any startup kernel JIT/warmup is included in startup. Independent
provider deletion is still necessary to stop billing. There is no auto-restart,
second rental, generation retry, or top-up.

### FL2VA directory layout at the pinned revision

Download `MiniMaxAI/MiniMax-H3` at
`42ed227ee7df40d41602854ae760620d6eb651fe` with the single HF filter
`--include 'FL2VA/*' --local-dir /workspace/model`, then serve
`--model-path /workspace/model/FL2VA` **without** `--model-variant` or
`--model-subfolder`. The partition's index already names `MiniMaxH3Pipeline` and
declares `_minimax_h3.partition: "fl2va"`.

The repo-root index uses three-element modular descriptors. Pinned SGLang's
[validator accepts only two-element component descriptors, then falls back to
root `transformer/` and `vae/`](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/utils/hf_diffusers_utils.py#L715-L743);
the exact reported exception is at lines 738–739. The real partition instead
contains `FL2VA/model_index.json` plus `audio_vae`, `processor`, `text_encoder`,
`tokenizer`, `transformer` and `video_vae`. Its
[pinned HF tree](https://huggingface.co/api/models/MiniMaxAI/MiniMax-H3/tree/42ed227ee7df40d41602854ae760620d6eb651fe?recursive=true&limit=1000)
has 81 FL2VA files (144,051,182,625 bytes by metadata); no root-level transformer
is missing from the download.

Simply changing the path while retaining `--model-variant fl2va` is also wrong:
[H3 sets the subfolder to `FL2VA`](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/pipelines/minimax_h3_pipeline.py#L87-L103),
and [the base loader appends it](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/pipelines_core/composed_pipeline_base.py#L173-L206),
yielding `FL2VA/FL2VA`. With no variant override, the partition-local index is
validated and [all component paths resolve beneath that directory](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/pipelines_core/composed_pipeline_base.py#L349-L361).
CPU source/tree validation proves these paths, not weight loading or GPU fit.

## Repeatable image builds and different models

Preserve the published audited image above; native plugin installation does not
require overwriting it. For a rebuild or a **different model**, use a new image
tag/digest and separate reviewed recipe/profile. Never retag a changed image as
the audited artifact.

1. **Pin the inputs.** Record the base image manifest digest, architecture,
   runtime Git commit, model repository/immutable checkpoint revision, Python,
   compiler/toolchain and direct package versions. Keep model weights out of
   the image. Resolve compatible dependencies together, require `pip check`,
   and retain the complete resolved freeze: source pins alone do not make
   transitive package resolution bit-reproducible.
2. **Use a minimal context.** This recipe needs only the Dockerfile and canonical
   stdlib launcher. Match its `COPY` statement; do not send the checkout, private
   archives, `.env`, home directory or credential cache:

   ```sh
   CONTEXT=$(mktemp -d)
   mkdir -p "$CONTEXT/examples/runpod-h3" "$CONTEXT/src/django_ergo/plugins/runpod"
   cp examples/runpod-h3/Dockerfile "$CONTEXT/examples/runpod-h3/"
   cp src/django_ergo/plugins/runpod/h3_launcher.py "$CONTEXT/src/django_ergo/plugins/runpod/"
   docker build --platform linux/amd64 \
     -f "$CONTEXT/examples/runpod-h3/Dockerfile" \
     -t ergo-h3:sglang-2511743-kitchen-int8 "$CONTEXT"
   docker image inspect ergo-h3:sglang-2511743-kitchen-int8 --format '{{.Id}} {{.Size}}'
   ```

   Use an isolated builder with explicit CPU/memory/disk/time limits, monitor
   free disk, and preserve logs/cache for long Rust compilation. Delete only
   this temporary context afterward; never broadly prune a shared daemon.
3. **CPU import and CLI checks, without weights/network/GPU.**

   ```sh
   IMAGE=ergo-h3:sglang-2511743-kitchen-int8
   docker run --rm --network none --entrypoint python "$IMAGE" \
     -c 'import torch, sglang, huggingface_hub, comfy_kitchen; print(torch.cuda.is_available())'
   docker run --rm --network none --entrypoint uv "$IMAGE" pip check
   docker run --rm --network none --entrypoint sglang "$IMAGE" serve --help
   docker run --rm --network none --entrypoint hf "$IMAGE" download --help
   docker run --rm --network none --entrypoint cat "$IMAGE" /opt/h3-environment.txt
   ```

   Exercise the exact task schema and launcher deadline/error paths with
   fixture-only inputs. A CPU import pass is **not** GPU initialization,
   memory-fit, conditioned-generation, download-speed or quality evidence.
4. **Audit before publication.** Save the image and retain its SHA-256,
   configuration digest, layer inventory and package freeze. Scan **every
   layer**, including subsequently deleted files, image config/history/env,
   nested wheels/tars/archives and the build context for secrets/private assets.
   Never print matching values. Classify every finding by path/type and
   cryptographic provenance; upstream test keys and random checksum matches
   need evidence, not an ignored scanner exit code. Retain the audit report.
   Repeat after any recipe or dependency change; the previous image's audit
   does not cover a new image.
5. **Publish only through an approved isolated registry scope.** Use an expiring
   project/repository-scoped robot, never a shared app's cached write credential.
   Supply its value through an approved stdin/credential-helper route, not
   command arguments, Dockerfile `ARG`/`ENV`, build context or shell history.

   ```sh
   # REGISTRY/REPOSITORY/ROBOT and a short-lived credential are operator supplied.
   export DOCKER_CONFIG=$(mktemp -d)
   docker login "$REGISTRY" --username "$ROBOT" --password-stdin
   docker tag "$IMAGE" "$REGISTRY/$REPOSITORY:$TAG"
   docker push "$REGISTRY/$REPOSITORY:$TAG"
   # Record the manifest digest emitted by the push, not docker image .Id.
   docker pull "$REGISTRY/$REPOSITORY@$MANIFEST_DIGEST"
   docker image inspect "$REGISTRY/$REPOSITORY@$MANIFEST_DIGEST" \
     --format '{{.Id}} {{json .RepoDigests}}'
   docker logout "$REGISTRY"
   ```

   Independently read/pull the digest using the actual intended read authority
   (anonymous for a public image) and compare its config digest with the local
   audited `.Id`. Verify quota and expiry, revoke/discard the temporary robot
   credential and remove only its isolated credential directory. A successful
   push is not a deployment and does not establish a RunPod pull.
6. **Add a reviewed execution profile, not arbitrary model arguments.** For a
   different model/runtime, copy the recipe/launcher to a separate example
   directory and replace *all* runtime/checkpoint/schema/hardware pins. Implement
   a separate trusted native adapter with bounded readiness, durable
   before-submit identity/no replay, cancellation/deadlines, ordered verified
   artifacts and independent cleanup; do not pretend another model supports
   H3's FL2VA schema. The current plugin admits only `frozen_h3`. Review hardware,
   disk, prices, license delegation and authenticated control/artifact transport
   for any new adapter. Add it to the host's server allowlist and bot-selected
   profile IDs only after its mocked-provider lifecycle and actual CPU contract
   smoke pass. Never let a model choose its own image, shell, credentials,
   arbitrary URL, mount or deadline extension.

All runtime downloads, warmup, inference and export count toward the original
rental deadline. A launcher must stop its process group before that deadline;
only independently confirmed provider deletion ends compute/storage liability.

## Validate the actual frozen archive — zero GPU/network calls

```sh
PYTHONPATH=src python -m django_ergo.plugins.runpod.runner validate \
  .worktrees/production-input/test-ready.zip.part001 \
  .worktrees/production-input/test-ready.zip.part002
```

Checks the concatenation SHA256
`ca4adf0852b44eb7b88300e73ffd4a37afb0c614dbc29d2760afa191d9f00efe`,
ZIP structure/CRC, all 48 inventory hashes/sizes, each selected sidecar prompt,
keyframe, historical version and unchanged approved WAV. Selected requests:
MS01/03/04 v1; MS02/05/06 v2. The controlled prompts remain provenance, never
blended. Known chest-tear, grime and mouth-motion flags remain in every result.
This is a technical comparison, not new art approval. The historical
**Sume `minimax-h3-max`** is a different baseline; open H3-Base excludes
Context-IR/Regenerate-2K and numerical/quality equivalence is not established.

The exact portrait target is **768×1344 at24fps**. At the pinned revision,
`conditions` uses `uri`, not `data`:

```json
{"task":"fl2va","seconds":5,"conditions":[{"type":"image","role":"keyframe","uri":"data:image/png;base64,...","frame_index":0}],"target":{"short_edge":768,"aspect_ratio":"9:16","duration_seconds":5}}
```

H3 rounds frames upward to `17k+5`: 5s→124 native frames and4s→107.
The worker requires sufficient real 24fps frames and trims to
**120/120/120/120/120/96**, without fps conversion, slowed playback, freeze,
repeat or padding. Generated native audio is removed from conformed clips.
The six clips are concatenated into **696 frames/29s**; approved continuous WAV
is preserved as a separately hashed original and muxed as PCM in
`comparison29s.mov`. Decoded master PCM must equal the WAV samples exactly.
No AAC substitution or silent audio replacement is permitted. A short,
wrong-canvas, malformed or non-video result fails; fake MP4 is never success.

## Controller and independently monitored cleaner

Prerequisites: a genuinely pullable named image manifest, an actual accepted
quote and permitted datacenter, an account-matched existing scoped credential
binding on the stable host, and a durable off-Pod ledger/export filesystem.
The controller commits a single create intent before sending the POST; explicit
live configuration and a fresh independent execute-mode cleaner are required.
Preparation and fixture mode do not call Runpod.

### Authenticated access without rebuilding the preserved image

The image itself remains loopback-only and has no sshd. The reviewed optional
`config.ssh_public_key` enables the fixed `runpod.ssh.SSH_BOOTSTRAP` entrypoint
override: it installs Ubuntu `openssh-server` at launch, accepts only that fresh
ed25519 **public** client key, exposes only `22/tcp`, and delivers the corrected
canonical launcher into `/run/sshd` before running it with the preserved image's
Python. Generate and keep the private client key on the durable controller host,
never in the image, provider environment or rental. Bind its absolute
owner-private path as host-only `ssh_private_key_file`; admission checks it
against `config.ssh_public_key`.
Do not change account-wide SSH keys. All overrides and the public key are bound
into the original request fingerprint before the one create.

The bootstrap checks the existing absolute deadline **before apt**, kills the
active apt process group at the same deadline-minus-120s cutoff, disables apt's
automatic sshd startup, and runs an explicitly foreground, separately tracked
public-key-only sshd child with bounded listener readiness. It never waits for
daemonization before publishing the host key. Forwarding is restricted to `127.0.0.1:30010`; SFTP/SCP and
authenticated command execution are available. Same-attempt restarts reuse the
fingerprint-bound private host key and recover its public file; unmarked or
different-attempt overlay keys are replaced noninteractively. This preserves the
authenticated log pin without ever prompting. Host private keys stay **inside**
the rental; they are not controller private keys.
Before any checkpoint download, the wrapper records effective RAM/disk/VRAM and
rejects a GPU outside the explicitly verified `gpu_nvml_names` for the quoted
`gpu_type`, less than48GB VRAM, or a driver older than major580. Runpod's catalog
ID `NVIDIA A100-SXM4-80GB` can correspond to NVML `A100-SXM4-80GB`; the host must
bind both identities explicitly rather than assuming equality or fuzzy matching.
A10080GB is supported by the pinned
Torch CUDA build's SM80 code; that is not measured H3 fit or throughput. The NVIDIA
[CUDA13 compatibility table](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
requires driver580 or later. The bound request requires `minRAMPerGPU=167` and
`minVCPUPerGPU=10`. The live v1 schema's CUDA enum stops at13.0 while current
catalog machines can report13.2, so no unsupported enum values are submitted;
the pre-download driver/GPU check is mandatory.

**No TOFU or blind `ssh-keyscan`.** The bootstrap prints only its public host
key with the immutable attempt fingerprint. `RunpodREST.ssh_connection(pod_id,
ownership, deadline=...)` performs exact-owned v1 GET, authenticated
[v2 container-log GET](https://docs.runpod.io/api-reference-v2/pods/stream-pod-logs)
at `https://api.runpod.io/v2/pods/{id}/logs?source=container&tail=5000`, and another
exact-owned GET to bind the returned `publicIp` and `portMappings["22"]`.
The result has `host`, `port`, `host_key_alias`, and a complete `known_hosts`
line. Save that line in an isolated file off-Pod and require
`StrictHostKeyChecking=yes`, `HostKeyAlias=<host_key_alias>`,
`UserKnownHostsFile=<isolated-file>`, `IdentitiesOnly=yes`, `BatchMode=yes`,
`-F /dev/null`, and `-i <fresh-private-key>`. The helper authenticates access
without creating resources; the native Worker uses it to start and maintain SSH.
Missing logs, wrong attempt markers, changed address/ownership or API failure
fail closed. Log reads have a total deadline even if an SSE line trickles forever.
Both API origins use a fixed ordinary User-Agent; provider credentials never
go to the Pod or SSH process.

The Worker uses the provider-returned direct address, not `ssh.runpod.io`.
For manual transport diagnosis only:

```sh
# All angle-bracket values come from the authenticated helper, never a model.
env -u "$SCOPED_CALLBACK_ENV" ssh -F /dev/null -i <fresh-private-key> -p <port> \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=<isolated-file> -o HostKeyAlias=<host_key_alias> \
  -o ExitOnForwardFailure=yes -N \
  -L 127.0.0.1:30010:127.0.0.1:30010 root@<host>
```

The native Worker creates a per-job OpenSSH control socket on its stable host
and forwards an ephemeral local loopback port to the rental's `127.0.0.1:30010`.
Subsequent polling tasks reuse the master; transport reconnection does not replay
generation. Stop and terminal cleanup close it, and a remote watchdog retains the
original work cutoff. The Worker probes the fingerprint-bound launcher marker
over that authenticated connection every poll, failing promptly on startup death.
No operator tunnel keeper is required. An operator SSH process is observation
only, never a second runner.
Runpod's [SSH guide](https://docs.runpod.io/pods/configuration/use-ssh) explicitly
requires public-IP direct SSH for SCP/SFTP; basic proxy support is not assumed.

**Pre-create CPU proof (no provider, weights or GPU):**

```sh
PYTHONPATH=src python examples/runpod-h3/prove_ssh.py
```

This requires Docker access, a local `ssh-keygen`, and Ubuntu package-download
access. It checks the preserved local image config digest, starts its **exact**
bootstrap override in a2CPU/6GB local container without publishing public ports,
and leaves the original launcher untouched. Explicit synthetic `hf`,
`nvidia-smi` and `sglang` executables prevent checkpoint/GPU calls. A separate
CPU client container holds a disposable proof key; the server receives only
its public key. Trusted local Docker logs supply the host pin (the same public
marker used by authenticated provider logs). Real OpenSSH tests correct/wrong
client keys, rejects a wrong host pin, forwards loopback health and binary
bytes, and SCPs timing/baseline files. Its JSON labels GPU/resource observations
synthetic. Both containers and disposable proof keys are removed on exit.
This is an operator proof command, not a claim that it has already passed.

**Runtime evidence:** SCP `/workspace/h3-access-timings.json`,
`/workspace/h3-resource-baseline.json` and `/workspace/h3-timings.json` to durable
off-Pod storage using the same strict SSH options (`scp -P <port> ...`).
The first records SSH apt/setup duration, the baseline records effective RAM,
free/total disk, GPU/VRAM and driver before downloads, and the original launcher
records actual download/startup durations with Torch compile disabled. Sample
`nvidia-smi --query-gpu=timestamp,name,driver_version,memory.total,memory.used,utilization.gpu --format=csv`,
`cat /proc/meminfo`, `cat /sys/fs/cgroup/memory.current`,
`cat /sys/fs/cgroup/memory.peak`, and `df -B1 /workspace` through authenticated
SSH during download/load/generation; timestamp and retain results **off-Pod**.
Also retain native request/export elapsed times. None of these measurements is
an invoice, and a48GB model-fit proposal is not measured fit or throughput.

**Residual pre-create limits:** authenticated read-only account/catalog calls
can prove key permissions, current GPU/datacenter price/availability, driver
advertisement and requested hardware constraints, but cannot reserve stock or
prove the eventual public port mapping, Runpod registry pull, apt throughput,
target-specific logs, cold model download/load, RAM/VRAM fit or generation
speed. Secure Cloud's public IP is documented, while the external TCP22 mapping
exists only after allocation. With no existing Pod, target log authorization
cannot be exercised before creation; a synthetic404 probe proves at most API
routing, not log permission or execution. If exact167GB stock, the authenticated
log pin route, or CPU transport proof is unavailable, keep starts disabled.
Never buy a smaller host, expose SGLang, retry an unknown create or rely on the
[v2 exec API](https://docs.runpod.io/api-reference-v2/pods/run-a-command-in-a-pod):
that API explicitly permits503 for machines not yet exec-enabled. Independent
off-Pod maintenance must continue regardless of tunnel/controller failure.


Use the already-injected scoped credential environment-variable **name**, not
a secret value, for `$SCOPED_CALLBACK_ENV`. Establish cleaner freshness before
any create/start decision and leave it running independently after the worker
returns. A dry-run heartbeat does not qualify. See [cleaner supervision](runpod-cleaner.md)
for service/supervision configuration and fixture proof.

The create configuration is a JSON file containing no secrets. For a live run,
replace every angle-bracket value with the verified selected account, manifest,
datacenter and quoted/estimated amounts; the boolean evidence fields mean the
operator has actually verified the associated fact, not merely copied this
example. The phase seconds are planning estimates, not actual-time guarantees.

```json
{
  "mode": "live",
  "live_execute": true,
  "account": "<renting-account-id>",
  "image": "<pullable-image>@sha256:<64-hex-manifest-digest>",
  "data_center_ids": ["<permitted-datacenter-id>"],
  "idle_grace": 120,
  "eligibility": {"model": true, "hardware": true, "license": "delegated", "inputs": true, "budget": true},
  "timeline": {"rental_seconds": 3600, "startup_seconds": 600, "download_seconds": 600, "compile_seconds": 0, "generation_seconds": 2061, "export_seconds": 219, "termination_seconds": 120},
  "pricing": {
    "compute_hourly_usd": {"amount": "<actual-hourly-quote>", "verified": true},
    "storage_usd": {"amount": "<one-hour-storage-estimate>", "verified": true},
    "network_usd": {"amount": "<network-estimate>", "verified": true},
    "fees_usd": {"amount": "<fees-and-margin-estimate>", "verified": true},
    "project_cap_usd": "3.00",
    "project_committed_usd": "<existing-actual-and-outstanding-commitments>"
  }
}
```

Run `prepare` **only when all prerequisites are ready**: it starts the
irrevocable pre-create clock. Use a new absolute ledger path on local durable
storage; never replace it after a failed or uncertain request.

```sh
PYTHONPATH=src python -m django_ergo.plugins.runpod.create prepare "$LEDGER" "$CONFIG"
# Start the independent cleaner shown below and check its fresh heartbeat.
PYTHONPATH=src python -m django_ergo.plugins.runpod.supervisor "$LEDGER" --check-fresh
PYTHONPATH=src python -m django_ergo.plugins.runpod.create create "$LEDGER" "$CONFIG" \
  --execute --credential-env "$SCOPED_CALLBACK_ENV"
```

The quoted compute rate is bound into the request fingerprint and checked
against reservation evidence; it is **not** a provider-enforced price ceiling.
One failed or timed-out create leaves `create_unknown`; never rerun it, replace
the ledger, or purchase a retry. Continue the cleaner and reconcile a late
receipt. The SQLite `experiment_receipts` table stores received Pod IDs.

```sh
# Process1: stable host, not a child/background task of the GPU worker.
PYTHONPATH=src python -m django_ergo.plugins.runpod.supervisor "$LEDGER" \
  --interval-seconds 1 --operation-timeout 5 --freshness-seconds 10 \
  --execute --credential-env "$SCOPED_CALLBACK_ENV"

# Process2: after /health is ready and h3-timings.json is copied off-Pod.
# $OFF_POD_EXPORT must be durable storage on this controller host, not Pod disk.
PYTHONPATH=src python -m django_ergo.plugins.runpod.runner work \
  .worktrees/production-input/test-ready.zip.part001 \
  .worktrees/production-input/test-ready.zip.part002 \
  --ledger "$LEDGER" --output "$OFF_POD_EXPORT" \
  --setup-timings "$OFF_POD_SETUP_TIMINGS" \
  --base-url http://127.0.0.1:30010
```

Each stable queue submission persists a tombstone **before** POST. Accepted
receipts survive restart and can be inspected via `Submissions(ledger).inspect`
and GET `/v1/videos/{receipt}`. A lost POST response remains unknown forever:
SGLang does not document client-id idempotency or lookup. Never replay it or
claim list absence proves nonacceptance. Queue originals/corrections remain
immutable; this fixed pilot command accepts only the six original shots and
will not execute appended chapter work or paid corrections.

Off-Pod native and conformed artifacts have size/SHA256 metadata.
`manifest.json` atomically records all six queue positions including unfinished
items and any real partial exports; `complete` requires all six successful
clips plus validated assembly. Download/startup/compile, per-shot generation,
conform/export, assembly and total transfer durations are recorded. Fixed
work cutoff is no later than reservation+3480s and hard deadline−120s; clock
does not start at model readiness. A failed worker requests cleanup immediately;
a killed worker is fenced by its fixed work deadline and independent cleaner.

CH1 has no approved meaningful-motion plan; CH2 has unapproved script,
missing approved audio and unresolved art/motion. They remain non-executable:

```sh
# Intentionally exits2 before model/provider access with the exact missing plan.
PYTHONPATH=src python -m django_ergo.plugins.runpod.runner validate \
  .worktrees/production-input/test-ready.zip.part001 \
  .worktrees/production-input/test-ready.zip.part002 --stage ch01
```

## Price estimate and honest fit boundary

[Public Runpod profile](https://www.runpod.io/pricing) lists RTX6000Ada
48GB/167GB host RAM/10vCPU at **$0.99/h**. The standard 4090 profile has
24GBVRAM but only41GB host RAM at$0.89/h. Public price is not a live quote or
availability evidence; the strict bridge requests SECURE cloud. Confirm exact
cloud/region, host RAM, disk and accepted rate before launch, not after.

Intended full-hour GPU$0.99 +200GB container disk at
[$0.10/GB/month](https://docs.runpod.io/pods/pricing), using720h/month,
is **$1.01778**. Reserving **$0.25 ancillary/termination margin** yields a
**$1.27 rounded planned reservation**, leaving$1.73 of the$3 authorization.
A higher actual quote/fees/prior commitments must be recalculated; no extra
spend, top-up, retry rental, or extension is authorized. The removed
absolute-zero-overrun guarantee is **not a gate**: controls are estimated cap
admission, early stop, immutable hard≤1h deadline, single create and persistent
cleanup. Provider failure can still exceed the estimate; do not claim otherwise.

Pinned upstream evidence reports **4090D24GB, ~18GBVRAM,107-frame
1344×768 T2VA20NFE**: INT8+FA303.3s, BF16+FA405.6s **warmed**.
That is not conditioned portrait124-frame fit/throughput evidence. ~108GB
weights and a reported116.7GB pinned/anonymous host-memory operating point
explain the larger-RAM proposal. A different constrained24.5GiB anonymous
measurement used a2TBhost caching107.7GiB mapped weights at480p; it does not
prove fit/speed on a41GBhost. Reserve200GB for one checkpoint copy plus
environment/outputs; exact disk/RAM peak of the selected I2V profile is unknown.

[INFERENCE] Frame-linear scaling of the warmed INT8 measurement gives
`303.3*(5*124+107)/107=2060.7s` (~34.35min) for six shots. Assuming20min
cold/setup/export/cleanup gives54.35min. Neither the scaling nor20min overhead
is measured. Downloading108GB within10min requires~180MB/s sustained alone.
One-hour completion is therefore **unproved**, not impossible by schema;
deadline enforcement can legitimately yield a hashed partial manifest.

### Primary source pins

- [Official deployment/model split](https://platform.minimax.io/docs/guides/local-deploy-h3.md).
- [Pinned condition fields](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/pipelines_core/stages/model_specific_stages/minimax_h3/request_validation.py),
  [URI loader](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/pipelines_core/stages/model_specific_stages/minimax_h3/material_io.py),
  [frame lattice](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/python/sglang/multimodal_gen/runtime/pipelines_core/stages/model_specific_stages/minimax_h3/time_request.py).
- [Pinned offload recipe/benchmarks](https://github.com/sgl-project/sglang/blob/2511743bd784e69e5a81ca3d926a000711dae4ab/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx).
- [Public pinned CUDA base digest](https://hub.docker.com/v2/repositories/nvidia/cuda/tags/13.0.0-cudnn-devel-ubuntu24.04).

Engineering launch checklist: retain the validated image/freeze and establish an
authorized pullable image delivery; read accepted live quote/profile; retain the
same scoped callback and existing ledger; verify independently supervised cleaner
freshness and tunnel; record cold timings; run only this six-shot batch; inspect
retained defects and partial/complete manifest; continue cleanup until
compute+storage absence.

`RESTCleanup.configure_create` requires a named `@sha256:` image reference with
a full 64-hex digest and an explicit nonempty `data_center_ids` allowlist. It
fingerprints those selected locations (`dataCenterIds`, custom order), the
existing ledger's absolute `ERGO_DEADLINE` (at most reservation+3600s), GPU, disk
and zero optional volume. Network volumes and templates are deliberately omitted;
no provider default location or storage attachment is allowed. The launcher's
deadline−120s cutoff means work ends by minute58 of a full-hour reservation,
including startup; a restart never renews it. The immutable request is not
evidence of actual placement or a provider-enforced TTL.

Operator update: Lee confirmed at22:42 that operation/video use is in permitted
countries; at22:43 the LEGAL TEAM owns review. The prior pending operator/use
question is closed, not a new engineering approval gate or a legal-clearance
claim. Select the actual permitted datacenter allowlist before launch; this
preparation performs no rental or legal investigation.
