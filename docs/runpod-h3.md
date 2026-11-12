# Frozen MiniMax H3 six-shot worker

Local preparation, not a deployment or rental authorization. The worker never
creates a Pod, obtains provider credentials, or downloads weights. Run the
controller and export directory **off the rented Pod** on the stable host that
holds the existing experiment SQLite ledger. The independent cleaner alone
uses the already-scoped credential callback; do not move an existing secret.

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
  Python 3.12, `comfy-kitchen`, HF client 1.28.0, ffmpeg/ffprobe. The CUDA13
  amd64 base is digest-pinned. Build before starting the rental clock and record
  the **resulting** image digest and `/opt/h3-environment.txt`; the final image
  has not been built/validated here and dependency resolution is not locked.
  No model is downloaded by the image build.

Preparation command (not run by this worker):

```sh
docker build -f examples/runpod-h3/Dockerfile \
  -t ergo-h3:sglang-2511743-kitchen-int8 .
docker image inspect ergo-h3:sglang-2511743-kitchen-int8 --format '{{.Id}}'
```

Image startup requires `ERGO_DEADLINE` equal to the ledger's **existing absolute
Unix deadline**, established before creation; container restart never resets it.
The entrypoint downloads only the pinned FL2VA files, starts a loopback-only
SGLang service, records download/startup timings in `/workspace/h3-timings.json`,
and kills the whole active subprocess group at deadline minus 120 seconds,
even during download/load/generation. `compile_seconds=0` means Torch compile is
disabled; any startup kernel JIT/warmup is included in startup. Independent
provider deletion is still necessary to stop billing. There is no auto-restart,
second rental, generation retry, or top-up.

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

## Two independent processes — launch preparation only

Prerequisites: existing account-bound REST ledger and actual resource receipt;
sole approved launch supervisor; prepared image digest; explicit quote admitted
through `budget.admit_rental`; previously requested operator/deployment/use
arrangement; stable off-Pod ledger/export filesystem; authorized tunnel to the
Pod's **loopback** port30010. No public ingress is required. The existing bridge
still intentionally has no live-create credential path; `create_once` only
accepts the in-memory fixture transport. These commands do not create a Pod.

Use the already-injected scoped credential environment-variable **name**, not
a secret value, for `$SCOPED_CALLBACK_ENV`. Establish cleaner freshness before
any create/start decision and leave it running independently after the worker
returns. A dry-run heartbeat does not qualify. See [cleaner supervision](runpod-cleaner.md)
for service/supervision configuration and fixture proof.

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

Engineering launch checklist: build/record final image and dependency snapshot;
read accepted live quote/profile; bind deadline/image/200GBdisk/zero volumes and
same scoped callback to the existing ledger; verify separately supervised
cleaner freshness and tunnel; record cold timing evidence; run only this six-shot
batch; inspect retained defects and partial/complete manifest; continue cleanup
until compute+storage absence. One consolidated outstanding human detail is the
already-requested actual operator/deployment/use arrangement; Mexico partner
ownership is accepted and no generic ownership/license re-investigation is needed.
