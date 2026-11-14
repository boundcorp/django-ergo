# H3 RunPod Serverless experiment — BACKUP ONLY

Lee's decision (2026-11-14): Ryan is the default. All RunPod generation is
parked as backup; there is **no automatic fallback from Ryan**. This historical
example is not authorization to build, rent, retry, benchmark, render, deploy,
merge, or activate anything. Do not run `worker_entry.py` now: it downloads model
weights and starts a server. It is only the experiment's worker-side entrypoint,
not a complete or independently safe deployment, and has no billing deadline or
provider cleanup of its own.

## Sanitized trial manifest

The successful single-shot experiment used the public, immutable image
`ghcr.io/boundcorp/ergo-h3-runtime@sha256:04b69208ac477452cf982b17e3e1eaacc2e84bf9cd824923f2c92fca472b51d9`.
Preserve this image; do not delete it. The model was `MiniMaxAI/MiniMax-H3`,
revision `42ed227ee7df40d41602854ae760620d6eb651fe`, FL2VA partition only.
The included entrypoint is a sanitized, formatting-only copy of the successful
v2 trial entrypoint, with a warning added. It applies the video VAE symlink and
serves `/workspace/model/FL2VA` with pinned SGLang options; the corrected image
already supplies Python headers. No model weights or private inputs are included.

The observed configuration was a load-balancing endpoint with min 0/max 1
workers, A100-SXM4-80GB, 250 GB ephemeral disk, no network volume, ports
8080/30010, a 300-second idle timeout, and no cached-model attachment. The
entrypoint checks for at least 249,000,000,000 cgroup RAM bytes before download.
The 8080 health shim returns 204 until the local server's health is OK, then 200;
client readiness used `/health` on the application port, not `/ping`. Capacity,
pricing, provider behavior and RAM availability are not guaranteed by this record.

The first trial produced no clip (missing Python headers); a later trial using the
corrected image produced one playable clip. The comparison with the native run
used different timing boundaries and does not establish a Serverless speedup.
Historical scripts that create endpoints, poll, send frozen inputs, or tear down
resources remain local evidence only: they are experiment-specific and are not a
reviewed reusable controller. No secrets, private input bodies, provider logs,
receipts, media or binary weights are committed with this example.

## Reactivation gate — no implicit authorization

1. Obtain explicit Lee approval for a new RunPod run; no automatic Ryan fallback.
2. Establish a fresh, separate budget, quota and capacity assessment, including a
   cleanup reserve and current provider request limits. Do not assume prior rates.
3. Review the pinned image, model and worker entrypoint and the current provider
   API. Before any create, establish ownership, one endpoint maximum and one
   worker maximum, independent hard wall/spend deadlines and a cleanup owner.
4. Export the approved output durably before teardown where possible. Scale the
   owned endpoint to zero, delete only owned resources and verify endpoint,
   worker and storage absence; reconcile delayed posted endpoint-specific charges
   and any retained template. Never infer zero billing from an empty early bill.
5. Preserve original evidence securely; sanitize before sharing or committing.

The preserved local receipt and output index is
`.worktrees/serverless-backup-parking.md` in the experiment worktree. Local
scratch artifacts are not a portable or access-controlled backup.
