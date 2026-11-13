# Native RunPod workers

The `runpod` plugin starts a real `runpod:watch` Worker. It is not a coding-agent
manager, subprocess CLI bridge, or Kubernetes Job. The first reviewed workload
adapter is the frozen six-shot H3 comparison; it does not execute chapter plans,
arbitrary commands, caller-selected images, or caller-selected paths.

## Ownership and lifetime

- Django's optional `ergo_runpod` app stores installation/account identity,
  bot/principal/chat ownership, request identity, the Worker link, planned
  reservations, manifests and attachment identities. Deleting a chat or Worker
  sets links to NULL; it does not delete rental liability.
- Each job reuses the existing `Experiment`, `Queue`, `RESTCleanup`, `Submissions`
  and media/export code in a dedicated durable rental directory. This external
  SQLite ledger retains create intent, exact ownership fingerprints, late
  receipts, stop intent, absolute deadlines and off-Pod exports.
- **All native controllers and maintenance consumers must share a durable
  filesystem with working SQLite and POSIX file locks on one host.** This is a
  bounded single-host integration, not a distributed-filesystem ledger. Do not
  deploy it across hosts or on an unverified network filesystem. The Django
  admission index alone cannot replace a lost rental directory.
- Start commits reservation, job and Worker before publishing its first task.
  Same request key/specification returns the existing job. A different chat or
  changed specification conflicts. No unknown create or H3 submission is
  replayed, and there is no automatic replacement/retry rental. Within one
  admission the Worker may try several *offers* (GPU type and data center) when
  the provider refuses a create for lack of capacity; see
  [launch-time offer selection](#launch-time-offer-selection-and-stock-fallback).
- The installation defaults to **one real or unresolved lifetime rental**, one
  unresolved rental, a planned USD 3 total and at most 3,600 seconds from
  pre-create reservation. Cleanup-complete attempts that never sent a create
  consume neither money nor a rental slot. Real or uncertain creates retain
  their slot; terminating a rental does not mint a top-up. Invoice cost remains
  explicitly unknown.
- A cancelled/failed/missing Worker, closed/deleted chat, revoked principal,
  missing/disabled server policy, early work cutoff or absolute deadline requests
  stop. Provider cleanup retries independently. An accepted DELETE or empty
  inventory is not final absence evidence. Unresolved creates retain liability.
- Independent maintenance stops a controller whose active Worker row has not
  advanced for 180 seconds, without waiting for the generic 30-minute step
  lease. It checks startup/download deadlines independently of the watcher.

## Install on the host

Ergonaut installs `django_ergo.plugins.runpod.apps.RunpodConfig` and registers a
60-second Beat task. Other Django hosts must add that app to `INSTALLED_APPS`.
Run migrations before loading the plugin:

```sh
python manage.py migrate
```

Start the independent **Ergonaut** maintenance consumer in addition to Beat and
the normal `bot_tasks` consumer. The supplied production entrypoint is
`ergonaut/infra/prod/start-runpod-maintenance.sh`:

```sh
celery -A ergonaut worker -l info -Q runpod_maintenance \
  --concurrency=1 --hostname=runpod-maintenance@%h
```

Give that consumer the same Django database, durable rental mount and
installation-scoped provider credential binding as the native controller.
Never give the GPU image a RunPod management credential. For operator recovery,
the exact same reconciliation path is available as:

```sh
python manage.py reconcile_runpod
```

This command is not a substitute for the continuously supervised independent
consumer. New starts fail closed if its last successful maintenance pass is
older than 180 seconds or cleanup is failing. A newly created ledger waits for
its first independent executing heartbeat before create; a watcher cannot
self-admit by refreshing its own heartbeat. Provider requests have five-second
timeouts; a tick may perform multiple bounded reads/deletes and unresolved work
remains due. Bot-folder schedules and `serve()` are not cleanup supervision.

### Server-only policy

Set `RUNPOD_ACCOUNTS` in the host's private settings. Bot YAML cannot override
these credentials, principals, caps, inputs, endpoint or image. This example is
**disabled**; it is not live admission:

```python
RUNPOD_ACCOUNTS = {
    "gpu_jobs": {
        "enabled": False,
        "allowed_bots": ["storytime"],
        "allowed_users": ["approved-admin"],  # active superusers only
        "api_key_env": "ERGO_RUNPOD_API_KEY",  # existing host injection, not its value
        "ssh_private_key_file": "/var/lib/ergonaut/keys/h3_ed25519",  # host only
        "ledger_root": "/var/lib/ergonaut/runpod",
        "max_concurrent": 1,
        "max_rentals": 1,
        "budget_usd": "3.00",  # project cap across rentals
        "max_rental_usd": "3.00",  # per-rental planned ceiling (default 3.00)
        "prior_committed_usd": "0.00",
        "reconciled_costs": {},  # operator-reviewed terminal costs; never bot-editable
        "gpu_catalog": {},  # optional additions/overrides to the built-in GPU catalog
        "profiles": {
            "h3_frozen": {
                "adapter": "frozen_h3",
                "parts": ["/approved/test-ready.zip.part001", "/approved/test-ready.zip.part002"],
                "base_url": "http://127.0.0.1:30010",
                "max_output_bytes": 2_000_000_000,
                "config": {
                    "mode": "live",
                    "live_execute": True,
                    "account": "provider-consumer-user-id",
                    "image": "registry.octo.boundcorp.net/ergo-h3-experiment/h3-runtime@sha256:7b08f5d6b56988afeab528c9524ded9052a6739df0899768294134afe1453f8b",
                    "hardware": {
                        # Ordered preference; the first in-stock one inside the envelope wins.
                        "gpus": ["NVIDIA A100-SXM4-80GB", "NVIDIA H100 80GB HBM3"],
                        "any_gpu": False,  # True: then any other catalog GPU that fits, cheapest first
                        "gpu_count": 1,
                        "min_vram_gb": 48,
                        "min_ram_per_gpu_gb": 167,
                        "min_vcpu_per_gpu": 10,
                        "min_cuda_version": "13.0",  # stock query filter
                        "min_driver_major": 580,  # checked in the Pod before download
                        "compute_capabilities": ["8.0", "9.0"],  # what the image's CUDA build runs on
                        "cloud_type": "SECURE",
                        "countries": [],  # e.g. ["US", "CA"]; empty = any country
                        "data_centers": "any",  # or an allowlist such as ["US-KS-2", "EU-RO-1"]
                        "exclude_data_centers": [],
                        "container_disk_gb": 200,
                        "stock_poll_seconds": 60,
                        "stock_wait_seconds": 1800,
                        "max_create_attempts": 10,
                    },
                    "idle_grace": 120,
                    "eligibility": {"model": True, "hardware": True, "license": "delegated", "inputs": True, "budget": True},
                    "timeline": {
                        "rental_seconds": 3600, "startup_seconds": 600,
                        "download_seconds": 600, "compile_seconds": 0,
                        "generation_seconds": 2100, "export_seconds": 120,
                        "termination_seconds": 120,
                    },
                    "pricing": {
                        # Hourly ceiling for the whole Pod; live offers above it are skipped.
                        "compute_hourly_usd": {"amount": "1.99", "verified": True},
                        "storage_usd": {"amount": "0.03", "verified": True},
                        "network_usd": {"amount": "0", "verified": True},
                        "fees_usd": {"amount": "0.25", "verified": True},
                        "project_cap_usd": "3.00", "project_committed_usd": "0",
                    },
                },
            },
        },
    },
}
```

Ergonaut can instead set `RUNPOD_ACCOUNTS_FILE` to an operator-owned JSON file
containing the same account mapping. The native plugin rereads it on every
operation, so GPU, region, cap and catalog edits apply to the next start
without restarting any role. The file takes precedence over
`RUNPOD_ACCOUNTS`: missing, malformed or non-mapping data never falls back to an
older enabled policy. Revocation stops existing work, but independent cleanup
continues from persisted account keys and each job's credential binding.

Keep credentials out of that JSON; it contains only the approved environment
variable name. Use a durable path shared by controller and maintenance, not a
bot checkout. Mode `0600` is useful operator hygiene, not isolation from bot
shell tools running as the same UID on the same writable volume.

The example numbers are a plan, not current quotes or a billing guarantee.
Admission includes prior commitments and all liable reservations under an
account row lock, using the policy's `budget_usd` as the project cap; the USD 3
per-rental hard authorization remains. Cleanup-complete rows proving no create
was ever sent release their financial commitment and `max_rentals` slot,
regardless of ledger phase. Their historical rows and original reservations
remain immutable. Unresolved intents reserve a slot before POST to prevent
concurrent admissions; real or uncertain creates retain their lifetime slot.
Created or uncertain attempts remain reserved after cleanup until operator
review. An operator may then supply a `reconciled_costs` entry keyed by immutable
job UUID, with `amount_usd`, `basis: "conservative_elapsed_rate"`, a nonempty
`evidence` reference, and `observed_at` after admission and not in the future.
Only cleanup-complete, terminal ledgers can use that conservative reviewed
commitment; active/unknown rentals remain fully reserved. This is not an invoice,
automatic refund or new rental approval.
Each create request binds the chosen offer's GPU type and single data center,
plus `config.hardware`'s cloud type, GPU count, host RAM/vCPU minimums and
container disk, zero optional volume and no network volume. Before model
download the SSH bootstrap checks the observed GPUs (`nvidia-smi` name, driver,
memory and compute capability) against rules bound into the create
fingerprint: GPU count, `min_driver_major`, `min_vram_gb` (NVML reports slightly
less than the marketed size, so 48 GB accepts 45000 MiB), and either a verified
NVML display name from the GPU catalog or, as the generic fallback, one of the
profile's `compute_capabilities`. A new display string therefore never needs a
code change. The observed `costPerHr` must be present and not exceed the
profile's hourly ceiling before model work. Storage/ancillary evidence is still
independently required. Failed provider termination can exceed estimates; no
impossible zero-overrun guarantee is imposed as an admission gate.

## Launch-time offer selection and stock fallback

Admission prices the reservation at the profile's hourly ceiling
(`pricing.compute_hourly_usd`) and checks it against `max_rental_usd` and
`budget_usd`. It does not pick hardware. When the Worker launches it:

1. Reads live Pod stock from the v2 catalog
   (`GET https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD`,
   scoped by `cloud_type`, `gpu_count`, `countries` and `min_cuda_version`).
2. Keeps GPU types in `gpus` (or any catalog GPU with `any_gpu`) whose catalog
   compute capability is in `compute_capabilities`, whose VRAM meets
   `min_vram_gb`, and whose price × `gpu_count` is within the hourly ceiling;
   then every data center with stock that `data_centers` allows and
   `exclude_data_centers` does not exclude.
3. Orders offers by `gpus` preference, then price, then stock level, and
   creates on the first one. No further human approval is needed: every offer is
   inside the envelope the start approval covered. The chosen GPU, data center,
   $/h and quote time are recorded in the rental ledger.
4. If the provider answers the create with an explicit no-capacity refusal
   (`no_capacity_messages` in policy can replace the built-in phrases), the
   Worker lists inventory. When nothing carries the attempt's marker the
   attempt is released (`create_started` back to 0), the refused request stays
   in cleanup's ownership scan (every maintenance tick terminates a Pod it
   materializes late, even while the job runs), and the next offer is tried at
   once. A refused
   create is not a rental: it costs $0, does not use `max_rentals`, and needs no
   ledger repair. A timeout, any other error, an inventory failure or a Pod
   carrying the marker leaves the create uncertain; it is reconciled and never
   retried on another offer.
5. With nothing eligible, the job stays queued and re-reads stock every
   `stock_poll_seconds`. The rental clock is restarted at each poll and at
   create, so waiting never uses the rental interval. After
   `stock_wait_seconds` (or `max_create_attempts` refusals) it stops with
   outcome `no_stock`, listing what it saw; cleanup confirms nothing exists.

A queued job holds its concurrency/rental slot and reservation until it creates
or stops, so two waiting jobs can never both create past `max_concurrent`.
Admission snapshots the profile and the merged GPU catalog: a policy edit
applies to the next start, not to a job that is already queued (terminate it and
start again with a new `request_key`).

### Change GPUs, regions and caps without a deploy

Edit the account in `RUNPOD_ACCOUNTS_FILE`; nothing else restarts.

| To change | Edit |
| --- | --- |
| Preferred GPUs and their order | `profiles.<id>.config.hardware.gpus` (`any_gpu` to accept others) |
| Allowed regions | `hardware.countries`, `hardware.data_centers`, `hardware.exclude_data_centers` |
| Maximum $/h for the Pod | `profiles.<id>.config.pricing.compute_hourly_usd.amount` |
| Per-rental planned USD ceiling | account `max_rental_usd` (default 3.00) |
| Project USD cap | account `budget_usd` |
| What the image runs on | `hardware.compute_capabilities`, `min_vram_gb`, `min_driver_major`, `min_cuda_version` |
| Host shape | `hardware.min_ram_per_gpu_gb`, `min_vcpu_per_gpu`, `gpu_count`, `cloud_type`, `container_disk_gb` |
| How long to wait for stock | `hardware.stock_poll_seconds`, `stock_wait_seconds`, `max_create_attempts` |
| A GPU the catalog does not know | account `gpu_catalog` |

The built-in catalog (`hardware.DEFAULT_GPU_CATALOG`) maps RunPod catalog IDs
to NVML display names, compute capability and VRAM for A100 80GB (SXM4/PCIe),
H100 (SXM/PCIe/NVL), H200, B200, L40S, L40, RTX 6000 Ada, RTX A6000, A40 and
RTX PRO 6000 Blackwell (Server/Workstation). Only the A100 SXM4 names were
observed on a live rental; the others follow NVIDIA's product names, and the
compute-capability fallback covers a different string. When a profile leaves
`compute_capabilities` empty, the chosen GPU's catalog capability is that
fallback. Add or override an entry
(or set it to `null` to remove one) without code:

```json
"gpu_catalog": {
  "NVIDIA GeForce RTX 5090": {
    "nvml_names": ["NVIDIA GeForce RTX 5090"],
    "compute_capability": "12.0",
    "vram_gb": 32
  }
}
```

A GPU type missing from the catalog is never offered: its compute capability,
and so the image's support for it, is unknown. `ergo_runpod_stock(profile)`
shows what would launch right now and why every other listed GPU or data center
is skipped.

**Before enabling:** verify the actual account, hourly ceiling, allowed regions, approved inputs,
filesystem locking/durability, credential injection, independently consumed
maintenance queue and authenticated model access. The preserved published H3
image listens only on loopback and does not run sshd. The reviewed
`config.ssh_public_key` bootstrap installs direct SSH and delivers the corrected
pinned launcher without rebuilding it; only the public client key enters the
rental. The host-only `ssh_private_key_file` must be an absolute, owner-private
file matching that public key; admission validates it before reserving a job.
The Worker authenticates the owned direct address and public host key through
`RunpodREST.ssh_connection`, then maintains a per-job OpenSSH control master with
an ephemeral local loopback port. Subsequent polls reuse it; a dropped transport
can reconnect without repeating a create or generation. Stop and confirmed
cleanup close it; a remote watchdog bounds it to the original work cutoff.
The Worker's SSH commands receive a minimal environment, not provider credentials.
Provider log SSE reads have a total request budget (including partial/trickling
lines), not only an idle timeout. A fingerprint-bound launcher failure marker
fails the Worker promptly instead of waiting for the startup deadline. See
[authenticated access and CPU proof](runpod-h3.md#authenticated-access-without-rebuilding-the-preserved-image).
Keep policy disabled until the access path and the hardware envelope are
verified. Model readiness waits only within the original startup/download
deadline; work stops at rental deadline minus 120 seconds.

## Opt a bot in

In the **hosted bot repository**, not an older application's capability registry:

```yaml
plugins:
  - name: runpod
    account: gpu_jobs
    profiles: [h3_frozen]
    root_only: false
```

Load the `runpod` skill in the intended studio chat or thread. This preserves
existing `local_shell` and cloud-generation workflows. Start requires explicit
human tool approval plus the original active allowlisted superuser and bot's
server policy; `local_shell.approve: false` grants no rental permission.

| Tool | Behavior |
| --- | --- |
| `ergo_runpod_profiles()` | List configured approved profiles without provider access; missing host installation is explicit. |
| `ergo_runpod_stock(profile)` | Read live stock for the profile's envelope: the offer that would launch now, eligible offers best-first, and why other GPUs/data centers are skipped. Read-only. |
| `ergo_runpod_start(profile, request_key)` | Reserve one immutable frozen workload and create its actual native Worker; returns job/Worker IDs, not GPU readiness. |
| `ergo_runpod_status(job_id)` | Read this bot/principal/chat's durable outcome, rental phase, cleanup error, deadline, manifest and planned reservation, plus the chosen `offer`, every `offer_attempts` entry (`created`, `uncertain`, `no_capacity`, `not_sent`) and the last `stock` poll with what was seen. |
| `ergo_runpod_collect(job_id)` | Idempotently publish already off-Pod files as attachments; partial/cancelled manifests keep all six original positions. Never regenerate or rent. |
| `ergo_runpod_terminate(job_id)` | Persist stop before cancelling the watcher and attempting bounded cleanup; no second spend approval is required. Pending cleanup is not reported as terminated. |

The Worker reports provisioning, the actual readiness/tunnel wait, shot export
progress and final results. Its one-time readiness notice is a system event,
not a fabricated human question or spend approval. The ordered manifest is the
result authority; attachment creation order is not shot order. Storage keys are
stable and checksummed so a crash between file save and database commit does not
create another visible attachment or another storage copy. Existing off-Pod
partial files remain collectable after cancellation/failure. A failed Worker can
still have cleanup pending; status/collect remain authoritative when chat event
delivery fails.

`collect` also returns `generations_jsonl` for Storytime's existing
`record_generations(ledger_jsonl=...)` tool. It includes only submitted terminal
items, stable job/item identities, original prompts/flags and artifact hashes.
Shared rental reservation is provenance in `details`, not a per-output estimate
or invoice: per-output cost stays unknown. Reimporting those identities does
not duplicate Generation rows or multiply the reservation across outputs.

## Verification without spend

The real-media regression requires `ffmpeg` and `ffprobe` on PATH (Debian/Ubuntu:
`apt-get install ffmpeg`). The library CI installs this dependency explicitly;
the media path is not skipped or replaced by fake success when tools are absent.

`tests/test_runpod_native.py` exercises the real plugin tools, real Worker rows
and dispatcher, an injected in-memory provider/H3 HTTP transport, real
ffmpeg/ffprobe media validation and off-Pod attachment collection. It covers
admission/scoping, rollback/publication, cancellation/fencing, deleted
Worker/chat/config, unknown and late resources, deadlines/cleanup retries and
storage-save crash recovery. Synthetic CPU video is not H3 generation or GPU
fit/quality evidence. See [H3 image and builds](runpod-h3.md) for the preserved
image, model constraints and repeatable image workflow.
