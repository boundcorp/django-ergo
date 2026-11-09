# Storytime Telegram passive-context handoff

## Scope and release gate

PR [#267](https://github.com/boundcorp/django-ergo/pull/267) adds opt-in passive group context. Lee authorized merge and deployment for this task; group enablement and secret provisioning remain separate operator steps. No token values were read, printed, or written for this handoff.

The active/passive sequence collision and concurrent first-thread creation were reproduced locally, then fixed with short database row locks; both independent-connection concurrency regressions now pass. The isolated end-to-end smoke and integrated regression suite also passed (details below). Distributed intake ordering and atomic cross-worker deduplication are not promised.

## Intended policy

Lee's nonsecret Telegram sender ID is **107609281**. The prior handoff obtained it through a targeted read-only kitchen-mgmt user lookup for `username=lee`. In each explicitly opted-in group:

| Incoming message | Result |
| --- | --- |
| Lee explicitly tags `@storytime69bot` | Authorized active turn |
| Lee sends an untagged message or only replies to the bot | Passive context only |
| A partner tags the bot, replies, or sends ordinary text | Passive context only |

A group ID never substitutes for sender authorization in an opted-in group. Passive capture stores attributed, untrusted text/captions and media attachments without transcribing audio, fetching author profiles, starting a turn, or changing the reply destination. It is not permission to execute instructions in that context. Album items are authorized individually. Private chats still bypass the group message-kind filter, but not sender authorization; this is not a private-chat ban.

The tag requirement governs new group messages. Separately, authorized Lee may deliberately click a current Approve/Deny button to resolve an already pending approval without an @tag. That callback must match this bot/thread and the current batch fingerprint under the turn lock. Partner callbacks and group-ID mappings do not authorize approval. Final integrated verification must cover this explicit approval flow as well as message routing.

Kitchen is **unchanged**: its existing `respond_to: {users: all, messages: all}`, `replies: tool`, eight-second photo batching, and `mode: polling` remain in its own bot config. Do not copy the Storytime policy into kitchen or alter global defaults.

## Identify the group safely

1. Confirm the intended household group with Lee and obtain members' consent to storing its future messages as model context. Confirm retention/access expectations before opting in.
2. Prefer a targeted read-only database lookup of the already-linked kitchen Telegram thread: inspect its session metadata `telegram.chat_id` and title, and confirm the numeric ID with a trusted member. Use the existing kitchen thread to disambiguate similarly named groups; do not enumerate message bodies or unrelated users.
3. If needed, ask a trusted member to confirm the ID using a **separate newly created discovery bot** under a separately approved setup. Do not start another `getUpdates` consumer for the live kitchen bot or Storytime bot, and do not call `deleteWebhook` or otherwise change their intake to discover an ID.
4. Replace `GROUP_CHAT_ID` below with the verified numeric group/supergroup ID, preserving its sign. It is deliberately unresolved here: no intended group ID has been established by this task.

Ordinary group delivery requires disabling **Storytime's group privacy mode in BotFather** before capture is enabled. Have the owner confirm Telegram is actually delivering ordinary messages under the final membership/privacy settings. Do not change kitchen's privacy settings. Telegram cannot backfill messages from before the bot joined; this feature performs no history import or backfill.

## Proposed Storytime bot configuration

Apply only after the release gates, group confirmation, consent, and separately authorized secret provisioning. The local config source is `boundcorp/ergo-bots`, `storytime/bot.yaml` (checked out at `/home/dev/p/boundcorp/ergo-bots/storytime/bot.yaml`); its inspected plugin list currently has no Telegram stanza. Add this plugin alongside the existing plugins rather than replacing them:

```yaml
plugins:
  - name: telegram
    token_env: STORYTIME_TELEGRAM_TOKEN
    users: {107609281: lee}
    owner: leeward@boundcorp.net
    target_thread: Telegram
    passive_group_chats: [GROUP_CHAT_ID]
    respond_to:
      users: [107609281]
      messages: tags
    mode: polling
```

Confirm `lee` resolves to the intended Django user and `leeward@boundcorp.net` is the intended thread owner before applying. Never put the token in YAML or reuse kitchen's token. `tags`, not `mentions`, is required: `mentions` intentionally retains reply-to-bot activation. Keep only the verified group in the opt-in list. No Storytime bot configuration was changed by this documentation task.

## Durable secret and runtime wiring

The durable infrastructure repository is **`boundcorp/infra-octo`**, locally `/home/dev/p/boundcorp/infra-octo`:

| Purpose | Exact source or target |
| --- | --- |
| SOPS-encrypted Secret source | `kubernetes/apps/ergonaut/app/runtime-secret.sops.yaml` |
| Kustomize inclusion | `kubernetes/apps/ergonaut/app/kustomization.yaml`, resource `./runtime-secret.sops.yaml`, namespace `ergonaut` |
| Flux Kustomization source | `kubernetes/apps/ergonaut/ks.yaml` |
| Flux object | `flux-system/cluster-apps-ergonaut` |
| Flux reconciliation path | `./kubernetes/apps/ergonaut/app`, from `GitRepository/home-kubernetes` |
| Kubernetes Secret target | context `octo`, namespace `ergonaut`, Secret `ergonaut-runtime` |
| Runtime consumer source | `kubernetes/apps/ergonaut/app/deployment.yaml` |
| Polling process env injection | Deployment `ergonaut/bots`, container `bots`, `spec.template.spec.containers[].envFrom[].secretRef.name: ergonaut-runtime` |
| New environment key | `STORYTIME_TELEGRAM_TOKEN` |

The same Secret is also injected into web, Celery, bot-task, beat, and migration processes. The prior handoff records SOPS decryption in the deployed Flux pipeline; the checked-out child Kustomization does not itself declare a decryption block. Preserve the established authorized SOPS workflow rather than adding plaintext or applying a cluster-only Secret patch. The prior inventory listed `KITCHEN_TELEGRAM_BOT_TOKEN` but no Storytime token key; that is a historical observation, not a fresh secret inventory.

The orchestrator owns provisioning the new key through encrypted Git and confirming Flux decryption/reconciliation. `envFrom` is read at process startup: reconciling a Secret alone does not update a running process environment. Never print the Secret, decode it for documentation, log token-bearing URLs, or fetch a token from logs.

## Intake deployment boundary

The deployment manifest declares one `bots` replica running `ergonaut bots`; kitchen explicitly polls there. Keep Storytime explicitly in polling mode with **one actual polling process for its token**, and no webhook consumer or second polling client. One replica alone is insufficient if rolling updates briefly overlap old and new pods; the operator enabling Storytime must arrange a nonoverlapping handover for that token.

This arrangement avoids the inherited multi-webhook-worker intake paths: each process has its own FIFO, and durable update acceptance uses check-then-insert deduplication, which can race across processes. It does **not** make intake globally ordered, distributed, or exactly-once. Database session/sequence locking and first-thread creation fixes do not convert process-local queues into a distributed queue. Do not enable concurrent webhook workers or multiple pollers on the strength of the single-process tests.

## Attachment audit

Before this change, passive media was recorded only as descriptors, active video
was not downloaded, and the only outbound Telegram tool sent text. Incoming
photos, videos, documents, voice, audio, video notes, animations and stickers
now save their bytes as attachments on the message in the shared thread, including
each passive album item. Downloads are limited to 20,000,000 bytes per file;
Telegram may impose its own stricter limits. An oversized, empty or failed
download is recorded as a not-downloaded descriptor rather than silently
claiming the file was saved. Passive audio is never transcribed.

`ergo_telegram_file` sends a persistent current-session attachment by ID or
filename as a photo, video, or document, with optional caption and reply. It does
not remove the attachment. URL-only files cannot be uploaded, and uploads are
limited to 50,000,000 bytes. Telegram's own file-type, size and caption
restrictions still apply. No real group media was
sent during this audit.

## Evidence and remaining operator prerequisites

Prior worker evidence at the initial handoff (not rerun for this document):

- `pytest -q tests/test_bot_plugins.py -k telegram`: **44 passed**.
- Ruff **0.12.5** check and format check passed.
- Direct routing smoke: Lee untagged false; partner tagged false; Lee tagged true; longer username false.
- Existing coverage included one-instance replay/deduplication, attribution, context, attachments, pending approvals, and legacy `mentions` reply behavior; it did not establish distributed ordering.

Current verification:

- `python -m pytest -q tests/test_telegram_concurrency.py`: **2 passed** after reproducing duplicate sequence `[0, 0]` and duplicate first threads before the fixes.
- Isolated end-to-end smoke using real temporary PostgreSQL and local model/Telegram adapters: model-call counts after partner tag, Lee untagged, Lee reply-only, Lee longer-username tag, and Lee exact tag were **`[0, 0, 0, 0, 1]`**. Four passive messages reached the authorized model request as untrusted context; all eight stored message sequences were unique; exactly one reply was sent.
- Pinned Ruff **0.12.5** check and format check passed for the five affected Python files.
- Integrated regression suite: **363 passed, 5 skipped** in **191.75 seconds**, covering Telegram plugins/concurrency/recovery/scheduling, conversation structured calls/Claude/OpenAI/attachments/runtime, import-layer contracts, and bot runtime. Command:

  ```sh
  python -m pytest -q \
    tests/test_bot_plugins.py tests/test_telegram_concurrency.py \
    tests/test_telegram_recovery.py tests/test_telegram_scheduling.py \
    tests/test_conversation_structured.py tests/test_conversation_claude_api.py \
    tests/test_conversation_openai_api.py tests/test_conversation_attachments.py \
    tests/test_conversation_runtime.py tests/test_import_layers.py tests/test_bots.py
  ```

The earlier smoke script was removed after execution. Those historical results precede the attachment audit and are not evidence of the later release.

Attachment audit verification:

- Telegram and attachment regression run: **185 passed, 5 skipped**; updated
  media-specific tests subsequently passed **19/19**.
- Isolated PostgreSQL round-trip smoke with local Telegram and model adapters:
  passive photo and audio saved their bytes with no model calls or transcript;
  an authorized message started one turn, and the saved photo was sent through
  `sendPhoto` to the linked chat. No real Telegram group was contacted.
- Pinned Ruff 0.12.5 check and format check passed for the changed Python files.

Operational prerequisites still outstanding: confirmed numeric group ID and consent; confirmed owner mapping; BotFather privacy configuration and delivery confirmation; separately authorized Storytime token provisioning through SOPS/Flux; and a single-consumer, nonoverlapping rollout plan. These are enablement prerequisites, not reasons to redesign intake for multiple webhook workers. None is a claim that infrastructure is unreachable. If later access fails, run `envcheck` and try the documented route from `/home/dev/ENVIRONMENT.md` before reporting unavailability; all Kubernetes reads must explicitly specify `--context octo` and must avoid secret values.
