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
  replayed, and there is no automatic replacement/retry rental.
- The installation defaults to **one lifetime rental**, one unresolved rental,
  a planned USD 3 total and at most 3,600 seconds from pre-create reservation.
  Reservations remain charged against admission after cleanup; terminating a
  rental does not mint a top-up. Invoice cost remains explicitly unknown.
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
**disabled**, with intentionally stale price evidence; it is not live admission:

```python
RUNPOD_ACCOUNTS = {
    "gpu_jobs": {
        "enabled": False,
        "allowed_bots": ["storytime"],
        "allowed_users": ["approved-admin"],  # active superusers only
        "api_key_env": "ERGO_RUNPOD_API_KEY",  # existing host injection, not its value
        "ledger_root": "/var/lib/ergonaut/runpod",
        "max_concurrent": 1,
        "max_rentals": 1,
        "budget_usd": "3.00",
        "prior_committed_usd": "0.00",
        "profiles": {
            "h3_frozen": {
                "adapter": "frozen_h3",
                "parts": ["/approved/test-ready.zip.part001", "/approved/test-ready.zip.part002"],
                "base_url": "http://127.0.0.1:30010",
                "max_output_bytes": 2_000_000_000,
                "quote_observed_at": 0,  # replace with genuinely fresh evidence
                "config": {
                    "mode": "live",
                    "live_execute": True,
                    "account": "provider-consumer-user-id",
                    "image": "registry.octo.boundcorp.net/ergo-h3-experiment/h3-runtime@sha256:7b08f5d6b56988afeab528c9524ded9052a6739df0899768294134afe1453f8b",
                    "data_center_ids": ["EU-RO-1"],  # verify permitted actual capacity
                    "idle_grace": 120,
                    "eligibility": {"model": True, "hardware": True, "license": "delegated", "inputs": True, "budget": True},
                    "timeline": {
                        "rental_seconds": 3600, "startup_seconds": 600,
                        "download_seconds": 600, "compile_seconds": 0,
                        "generation_seconds": 2100, "export_seconds": 120,
                        "termination_seconds": 120,
                    },
                    "pricing": {
                        "compute_hourly_usd": {"amount": "0.99", "verified": True},
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
operation, so genuinely fresh quote evidence can be atomically replaced just
before admission without restarting every role. The file takes precedence over
`RUNPOD_ACCOUNTS`: missing, malformed or non-mapping data never falls back to an
older enabled policy. Revocation stops existing work, but independent cleanup
continues from persisted account keys and each job's credential binding.

Keep credentials out of that JSON; it contains only the approved environment
variable name. Use a durable path shared by controller and maintenance, not a
bot checkout. Mode `0600` is useful operator hygiene, not isolation from bot
shell tools running as the same UID on the same writable volume.

The example numbers are a plan, not current quotes or a billing guarantee.
Admission includes prior commitments and all recorded reservations under an
account row lock. Native preparation pins one RTX 6000 Ada, one GPU, at least
167 GB RAM and 10 vCPU, 200 GB container disk, zero optional volume and no network
volume; it reuses the existing reviewed create request. A generic GPU catalog's
smaller minimum RAM is not evidence this exact profile is available. The observed `costPerHr` must be present and
not exceed the approved quote before model work. Storage/ancillary evidence is
still independently required. Failed provider termination can exceed estimates;
no impossible zero-overrun guarantee is imposed as an admission gate.

**Before enabling:** verify the actual account/quote/datacenter, approved inputs,
filesystem locking/durability, credential injection, independently consumed
maintenance queue and authenticated model access. The preserved published H3
image listens only on loopback and does not run sshd. The optional reviewed
`config.ssh_public_key` bootstrap installs direct SSH without rebuilding it;
only the public client key enters the rental. See the
[authenticated access and CPU proof](runpod-h3.md#authenticated-access-without-rebuilding-the-preserved-image)
for strict provider-log host-key pinning, forwarding, timing export and runtime
driver checks. A helper is not an established tunnel: never expose unauthenticated
SGLang or assume proxy SSH supports forwarding/SCP. Keep policy disabled until
the access path and exact quoted hardware are verified. Model readiness waits only within the original startup/download
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
| `ergo_runpod_start(profile, request_key)` | Reserve one immutable frozen workload and create its actual native Worker; returns job/Worker IDs, not GPU readiness. |
| `ergo_runpod_status(job_id)` | Read this bot/principal/chat's durable outcome, rental phase, cleanup error, original deadline, manifest and planned reservation. |
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
