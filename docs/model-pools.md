# Model pools, rollover and privacy

A **pool** is a named, ordered group of model routes (a provider's models,
or one exact `provider/model`) that every model consumer picks from: bot
chats and threads, titles, browser tasks, coding agents (`ergo_agent_start`,
`orca_start_worker`, `local_shell`), background tasks and workers. Pools are
preference, not billing classes: a pool may mix subscription (CLI) and
metered (API key) routes. Billing, price, transport and account are route
metadata on the provider.

Pools are **user-level groupings**. A pool with an `owner` belongs to that
user: they (and users they grant spend on it) see and select it; nobody
else does. A pool without an owner is a deployment pool everyone may use,
and it can only hold subscription routes. **Spend authority is a grant**
(`SpendGrant`): an immutable ceiling for one grantee on an exact set of
routes, with a required validity window. A preference or a pick never
mints authority.

A **privacy policy** is separate. It says where a restricted origin's data
may go, intersects with whatever pool is active, and is inherited by
everything that origin starts. Switching pools never widens it.

Everything resolves through one module, `django_ergo.bots.pools`:

```text
privacy_for(bot, session)        the inherited policy (bot, folder ancestors, chat, turn)
snapshot(providers, user, chat)  the pools this user may see, runtime gates, their grants and spend
select_route(...) / select_agent(...)
    -> Decision (pool, route, billing, grant, revision, rejected candidates, receipt id)
    -> or PoolRefusal(code, message, next_choices)
listing(bot, session)            what the pickers show: selectable now vs configured
issue_grant / revoke_grant       spend authority, by a pool's owner (or an admin)
switch_pool                      the active pool for a chat or the user, atomically
capture_policy / policy_scope    the pool and grant queued work runs under
```

Each selection leaves a `RouteDecision` receipt (no prompts, no secrets)
with the grant and the revision (user preference, pool gates, chat override
and, for queued work, its captured policy) it was made from.

## Without a `pools:` section nothing changes

A providers.yaml without `pools:` behaves exactly as before: one synthesized
pool, `subscription`, holds every provider; the global `tiers` and `agents`
apply; pinned API models still work; there is no budget enforcement and no
rollover. Two things apply in both modes:

- the provider **on/off switch** (providers.yaml `enabled`, or the Routing
  page) removes a provider from listings, pins, retries, tiers and new or
  resumed coding workers;
- **privacy inheritance** (below).

## Configuration

```yaml
auto_rollover: false # global gate, default off

providers:
  openai:
    type: openai
    api_key_env: OPENAI_API_KEY # billing defaults: api -> metered, cli -> subscription
    models:
      - { name: gpt-6.1-sol, price: { input: 1.25, cached: 0.125, output: 10 } }
  gemini:
    type: google # routes once a ("google", "api") engine is registered
    api_key_env: GEMINI_API_KEY
    models: [gemini-3-pro]

pools:
  - name: subscriptions
    label: Subscriptions
    routes: [claude, chatgpt, xai-oauth]
  - name: paid-api
    label: Paid API
    owner: lee # username; required when the pool has a metered route
    routes: [openai, anthropic/claude-opus-5-5]
    budget_usd: 50 # configured ceiling only; explicit funding is still required
    expires: 2026-12-31 # required with budget_usd; the grant ends here
    per_launch_usd: 5 # reserved per metered coding-agent launch
    max_output_tokens: 16000 # ceiling every metered request is clamped to
    rollover_out: false # default: true only for subscription-only pools
    allow_auto_entry: false # default: false when any route is metered
    tiers: { medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5] }
    agents:
      medium: [{ agent: codex, model: gpt-6.1-sol, provider: openai }]
```

| Key                 | Meaning                                                                                                                                                         |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `owner`             | Username of the user the pool belongs to. Required with a metered route; without it the pool is a deployment pool (subscription routes only).                   |
| `routes`            | Provider names (all their models) or `provider/model`.                                                                                                          |
| `tiers`             | Optional. Without it a tier is the global tier's candidates that are in the pool, else the built-in catalog (`DEFAULT_TIER_MODELS`) over the pool's models.     |
| `agents`            | Optional coding candidates, like the global `agents` but allowed to name metered providers. Without it: the global agents in the pool, else derived from tiers. |
| `budget_usd`        | The owner's own spend grant on the pool's metered routes. Changing it (or `expires`, `per_launch_usd`, the routes) adds a revision; spend stays cumulative.     |
| `expires`           | Date or ISO date-time (UTC). Required with `budget_usd`. After it the grant can't be used and its guards refuse.                                                |
| `per_launch_usd`    | Reserved against the grant for each metered coding launch (and each resumed run). At most `budget_usd`.                                                         |
| `max_output_tokens` | Output ceiling: every metered request's output limit is clamped to it (and to the grant's) before reserving.                                                    |
| `rollover_out`      | This pool may hand over to the next one when exhausted.                                                                                                         |
| `allow_auto_entry`  | The pool may be entered automatically: by rollover, past a switched-off pool, or as the first pool when it can spend money and the user hasn't saved an order. |
| `enabled`           | Off = a boundary: automatic selection doesn't skip past it into a pool that needs a manual pick, and a manual pick of it is refused.                            |

A provider takes `enabled` and `billing` (`subscription` or `metered`). A
metered model needs an exact `price`; without one it shows as unavailable
and can't be pinned. The price schema is the one every spend adapter prices
with (`pricing.ExactPrice`), USD per million tokens:

```yaml
price:
  input: 1.25 # required: uncached input
  output: 10 # required: output, reasoning included
  cached: 0.125 # cached input (default: input)
  cache_write: 1.5625 # optional; without it a request that can write cache isn't sent
  # cache_write is the 5-minute rate: Anthropic 1-hour cache writes are refused
  long_context: # optional: the whole request's rates once its prompt passes threshold
    threshold: 200000
    input: 2.5
    cached: 0.25
    output: 15
    cache_write: 3.125
```

Ergonaut keeps the runtime state in the database (tables `ModelPoolState`,
`ModelPoolPreference`, `ProviderToggle`, `SpendGrant`, `RouteDecision`):
order, the active pool, the pool gates and provider switches (all
versioned; every write needs the version last read) and the grants.
providers.yaml stays the source of what exists.

### Grants

A grant (`SpendGrant`) is the only spend authority. It names the pool, its
owner, the grantee, the exact `provider/model` routes, `budget_usd`,
`per_launch_usd`, `max_output_tokens` and `valid_from`/`expires_at` (both
required). Grant rows are immutable **revisions** of a **spending envelope**
(`SpendEnvelope`): the envelope is the stable identity that holds the
cumulative ledger and the lock, and every revision of it shares both.

- **The owner's own authority requires explicit funding.** A configured
  `budget_usd`/`expires` is only a ceiling. Config sync creates an **unfunded**
  envelope; it cannot create consent. The owner or an authenticated admin must
  call `approve_funding` (Ergonaut: the Funding form) with a positive cumulative
  dollar cap, a future expiry, and a nonempty selection of exact account/model
  identities. The amount and expiry fields start empty: the application never
  fills in a person's authorization. Each approval stores the actor, terms and
  audit on the same envelope. Its effective grant intersects the approved cap,
  window and accounts with current config. Raising a config ceiling, adding a
  route, changing accounts, or reloading never increases that approval.
  Lowering and restoring a ceiling may return only to the independently approved
  limit; every earlier spent, pending and unknown amount still counts.
  Funding revisions never reset use. Human revocation survives funding changes,
  config changes and continuation; only an admin's distinct **Restore** action
  lifts it. Restoration alone never funds an envelope.
  Pre-upgrade rows receive **no** inferred funding: they deny until reviewed and
  explicitly reconciled. The migration pauses existing pool-governed installs.
- **The envelope outlives the pool's name.** Each config envelope records
  the metered accounts it has funded (provider names, `api_key_env` names,
  `account` names; the list only grows). A pool with no envelope yet whose
  accounts overlap an existing config envelope's gets no new money from
  config sync:
  - when exactly one envelope overlaps, it is the same owner's and no
    current pool still uses it (a rename, or a pool removed and re-added
    under another name), the pool **continues** that envelope: it is
    re-keyed to the new name, and its spend, holds and any person's
    revocation carry over;
  - anything else (another owner's envelope, as after an ownership move;
    an envelope a current pool still uses; several overlapping envelopes,
    as when two funded pools are merged) waits, refused with
    `budget_required` and a reason saying an admin must link it, until an
    admin calls `pools.link_config_envelope` (Ergonaut:
    `POST /api/pools/{name}/envelope`, superusers only) to continue one
    envelope or to fund it separately (explicit new money, recorded with
    who did it and when).

  New accounts can receive an empty envelope, never config-created money.
  Parentless issued history blocks automatic creation because its ledger cannot
  safely be attributed. An admin must reconcile such history through the
  separate continuation/funding workflow. **Continue existing family** preserves
  its funding and ledger; **Prepare separate family** records an admin decision
  but leaves it unfunded until explicit amount/window/account approval. A
  continuation never expands the approved account set. **Restore** only lifts
  revocation. These are distinct operations with separate audit events.
  Runtime switches continue to follow pool/provider renames.
- **Issued grants**: the owner (or an admin) issues grants to other users
  with `pools.issue_grant` (Ergonaut: the Pools page). Each is a new
  envelope **drawn from the owner's config envelope** (its `parent`):
  issuing needs the owner's own `budget_usd`/`expires`, and every send on
  the grant is intersected with the owner's current authority (budget,
  expiry, routes, output ceiling, per-launch amount, ownership, switches).
  The owner's current budget caps the cumulative spend of the owner and
  every grant they issued on the pool together, so lowering, removing,
  expiring or revoking the owner's authority narrows or stops every grant
  at once, guards already built included. A new grant for the same
  grantee and pool revokes their earlier issued one, also one issued under
  the pool's earlier name (never the owner's own config authority), whose
  ledger stays; the owner can revoke and list those too. An issued grant also works only
  while its issuer owns the pool and the pool exists under its name: after
  a move to another owner or a rename it stops at once and works again only
  if that is undone. Config sync never revokes it, so a process still on an
  older providers.yaml can't destroy the current owner's grants.

The current config is applied whenever pools are read (`pools.snapshot`)
and again at every send (below). A grantee sees the pool,
can pick it, and uses only their grant's routes, within their grant's
budget. Their spend totals are theirs alone: listings show a user only their
own grant (an owner also sees the grants they issued), never another user's
spend.

## Selecting a pool

- **Automatic** (the default, "use preferred order"): the first pool in the
  user's order. If it is switched off (or not allowed by privacy), a later
  pool is entered only when its `allow_auto_entry` permits; otherwise
  selection stops with `pool_disabled` and names the next choices. Disabling
  a pool never silently moves work into a paid one. A first pool that can
  spend money (a metered route an engine can guard) needs `allow_auto_entry`
  too, unless the user saved their own order. A metered route no engine can
  guard is refused per route with `unguarded`, for every consumer.
- **Order**: a user orders the pools they can see. Pools hidden from them
  keep their places, and a hidden pool's name is never written into their
  order.
- **Manual**: a user-wide default (`ModelPoolPreference.active_pool`) or a
  per-chat override (`session.metadata["pool"]`, versioned by
  `pool_version`). Only pools the user may see can be picked. New turns and
  new descendants of that chat use it; a running turn or an already launched
  agent is never restarted. `pools.switch_pool` changes a scope atomically:
  switching the user default from a chat also clears that chat's override in
  the same transaction, or neither changes. Clearing the pick returns to
  preferred order.

Within the active pool the capability tier (`auto/cheap`, `auto/medium`, …,
with the `small`/`large`/`low`/`high` aliases) and effort stay separate
controls, and the existing pace scorer (`routing.choose`) ranks the
candidates.

**Pins** are constrained to the active pool and the privacy policy. A pin
outside the active pool is refused (`pin_out_of_pool`), never swapped.

## Exhaustion and rollover

A pool is **exhausted** for a request when it has compatible, authorized
candidates and none has capacity (every candidate's usage window is spent or
ahead of pace). Then, and only then, selection may move to the next enabled
pool, and only when all three gates permit:

1. the global `auto_rollover` (per user; default off);
2. the current pool's `rollover_out`;
3. the next pool's `allow_auto_entry` (and the next pool is itself usable:
   in budget, not expired).

A switched-off next pool is a boundary: the rollover stops there instead
of skipping it. Otherwise the request stops with a `pool_exhausted` refusal
that names the next pools; the chat shows it with a button to switch.

After a rollover the reached pool is **persisted** as the active pool in the
scope that was in effect (the chat's override, else the user's default),
with `rolled_from` recorded. Work stays there when the earlier pool's quota
resets; the person returns by switching back or picking "Use preferred
order". There is no invisible return.

These are **never** rollover triggers; each is its own refusal: a switched
off provider (`provider_disabled`), a missing key or CLI (`unavailable`), a
model without a price (`price_unknown`), a metered route on an engine that
can't enforce spend (`unguarded`), privacy (`privacy_denied`), a missing,
used, not-yet-valid or expired grant (`budget_required`,
`budget_exhausted`, `grant_not_yet_valid`, `budget_expired`), a pool or
route that isn't the user's (`not_authorized`), a switched-off pool
(`pool_disabled`), a pin outside the pool (`pin_out_of_pool`), and for
coding `no_coding_route` and `unconfinable`.

## Metered spend

A configured API key is never authority by itself; a grant is. With pools
configured:

- a chat turn's engine is built from the pool decision's exact route and
  checked against it before anything is sent (pool membership, provider,
  model, engine type, transport, billing); a chat engine without a decision
  refuses;
- a metered route is only selectable on an engine that implements the spend
  adapter contract (below). A metered CLI route, or any engine without an
  adapter, is refused (`unguarded`) at selection and again when the engine
  is built, for every consumer;
- the engine carries a spend guard (`conversation.spend_guard`) for the
  decision's grant revision. For each attempt, under its envelope's
  PostgreSQL advisory lock (an issued grant's takes its owner's envelope
  lock first, so the owner and all their grantees serialize on one lock),
  it re-reads the revision (revoked? this user?), re-reads providers.yaml
  as it is now (pool, owner, budget, expiry, routes, ceiling, and the pool's
  and provider's `enabled`; revoking the envelope if the config no longer
  backs it), and uses the **intersection** of the revision it holds with
  the current one, and for an issued grant also with the owner's current
  authority (narrowing only: the lower budget and ceiling, the earlier
  expiry, the common routes). "As it is now" means the file: every guarded
  send (and every pools read) hashes providers.yaml (one small read) and
  parses it again when it differs from what this process loaded, so a
  process that hasn't reloaded (the `ergonaut bots` process, a Celery
  worker, the voice bridge) still acts on the current file. All Ergonaut
  roles mount the same bot folders, so this is one source of truth. If the
  file can't be read or doesn't validate, metered sends are refused and
  config sync writes nothing until it loads again; subscription routes
  don't depend on it and keep working. The price is re-read the same way:
  a send reserves and prices at the current file's exact price, and
  refuses when it has none. It checks the
  pool and provider switches, clamps the output limit to that ceiling, asks
  the adapter for a bound on the request,
  reserves the worst case at the exact tariff (the dearest input rate that
  can apply: cache writes, the long-context tier), and refuses (no HTTP)
  when it can't;
- one guarded call is one HTTP attempt: the SDK's own retries are off under
  a guard, so every permitted retry is a new call with its own reservation;
- after the response it prices the adapter's parsed usage exactly. Missing,
  partial, negative or inconsistent usage, usage billed at rates the tariff
  doesn't have (audio, images, cache writes without a `cache_write` rate)
  and server-side tool fees keep the reservation (cost `unknown`); so does
  any send that may have reached the provider and failed;
- request classes that can't be bounded are refused before sending:
  non-text input (images, audio, files, remote URLs), server-side tools (web
  search), several choices, audio output and other billing-changing options.

Attempts are `InferenceRequest` rows with `pool` `grant:<revision id>`
and `billing_mode` `metered`, so the existing cost ledger and Costs page
see them. Spend and holds count toward the envelope for all time, summed
over all its revisions (revoked ones too): an uncertain hold stays counted
after revocation, and a request already sent keeps its hold. A newly
issued grant starts its own ledger, but still within its owner's budget,
which counts the owner's and every grantee's spend.

### The spend adapter contract (for new engines)

An engine can carry a metered pool route only when its class sets
`spend_adapter` to an object with:

```python
class SpendAdapter(Protocol):
    output_key: str  # the request's output-token limit key (informational)

    def bound(self, kwargs: dict, ceiling: int) -> RequestBound:
        """Clamp the request's output limit to `ceiling` IN PLACE (set it when
        absent); return RequestBound(input_tokens, output_tokens, cache_write)
        upper bounds. Raise UnsupportedRequest for anything it can't bound
        (modalities, server-side tool fees, several candidates, opaque options)."""

    def usage(self, response) -> Usage | None:
        """Usage(uncached, cached, cache_write, output) of a response, or None
        when it is missing, partial, negative or inconsistent, or reports fees
        the tariff can't price. None keeps the reservation."""
```

and sends each single HTTP attempt as
`await self.spend_guard.call(kwargs, self.spend_adapter, send)` with its
client's automatic retries off. `OpenAIChatAdapter` and
`AnthropicMessagesAdapter` are the reference implementations; CLI engines
set `spend_adapter = None`. A Gemini engine implements the same contract over
`contents`/`systemInstruction`/`generationConfig.maxOutputTokens` and
`usageMetadata` (cached prompt tokens split out, thoughts counted in output),
and prices with the same `price:` schema (`cache_write` and `long_context`
included).

### Queued work

Background tasks (`ctx.tasks`) and durable workers capture, when they are
queued, the pool new work would use, the user's grant on it and the
revision (`pools.capture_policy`), and run under it (`pools.policy_scope`)
in whichever process picks them up. That can only narrow: a revoked grant,
a switched-off pool or provider refuses; each send intersects the captured
revision with the current config (a later, smaller budget applies, a larger
one doesn't); a later, wider user choice or a new grant is never picked up,
and queued work never rolls over.

## Coding agents

Coding workers inherit the active pool. The blanket rule that coding may
only use subscription CLIs is gone; the native-CLI mapping, login health,
OMP session-log proof, admission leases and scoped Orca checks still apply.

**Metered coding is refused on every shipped agent manager today.** The
pool can list metered coding routes (and `ergo_models_list` shows them), but a
launch on one needs a manager that declares both:

- `AgentManager.metered_confinement`: the process is held to exactly one
  provider key and model, and
- `AgentManager.metered_spend_bound`: the launch's _total_ spend (every turn,
  retry and child process) is enforced, not just reserved.

| Manager       | Confinement                                                                                                                                                                                         | Total spend bound | Metered coding |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- | -------------- |
| `local_shell` | yes, as defense in depth: only the selected key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`/`CODEX_API_KEY`), other credentials/endpoints/cloud switches removed, job-local `HOME`, explicit `--model` | no                | refused        |
| `orca`        | no (Orca terminals own their environment)                                                                                                                                                           | no                | refused        |
| any other     | no (the `AgentManager` default)                                                                                                                                                                     | no                | refused        |

Why: an isolated environment and explicit model flags are necessary but are
not proof. A CLI can read other credential stores or project config, has its
own fallback logic, can spawn children, and runs many turns with no enforced
dollar or token ceiling; a ledger reservation (`per_launch_usd`) doesn't cap
that. omp is refused on metered routes in any case (its provider fallback
can't be switched off).

When a manager can demonstrate both, the path is already wired: the launch
reserves `per_launch_usd` against the grant before starting (and again for a
resumed run), the spec carries `AgentSpec.confinement`, and the reservation
is never released without billing evidence. For Orca the pool receipt is in
the launch journal (`AgentLaunch.watch_state`) before Orca runs; replaying a
launch key never selects, reserves or launches again.

No compatible coding route in the active pool is a refusal, never a switch
to another provider.

## Privacy

Every `private_pools` entry is a privacy policy of the same name (its bots
are the pool's `allowed_bots`, and it allows only that pool). Explicit
policies go under `privacy:`:

```yaml
privacy:
  personal:
    bots: [personal] # default: the private pool's allowed_bots
    allow: [personal] # private pool names, providers, provider/model
    web_search: [brave] # non-LLM search backends it may use
    memory: [personal-memory] # Hindsight banks it may retain in and recall from
    memory_reflect: [] # banks whose reflect (LLM synthesis) it may use
```

A bot is covered by a policy when it, or a folder ancestor, is in the
policy's `bots`. Nested folder bots inherit their parent's private pool and
policy. The effective policy of any work is the intersection of:

- the bot's (and its ancestors') policies;
- the chat's recorded origin (`session.metadata["privacy_origin"]`, set when
  a thread is created under a restricted parent);
- the turn being handled (a message from a restricted origin carries it;
  `pools.origin_scope`);
- for background tasks and workers, the origin captured when they started
  (stored with the job, restored in whichever process runs it).

What it governs:

- **Inference**: chat turns, titles, compaction, browser drivers and coding
  agents may only use routes the policy allows. Personal stays on its
  Fireworks routes whatever pool the user picks, and can't start coding
  agents unless a privacy-approved coding route exists.
- **Delegation**: a restricted chat can't create threads for, or message,
  a bot outside its policy (the one exit is a user-approved private export).
- **History and summaries**: a session under a policy (its bot's or an
  inherited origin) is readable by history search, links and file sharing,
  and appears in the orchestration overview, `ergo_thread_list` and
  `ergo_portfolio_status` (threads, workers, files), only for work under
  that same policy.
- **Memory**: Hindsight retain, recall, reflect and prefetch for restricted
  work only use banks the policy lists (`memory`; reflect, which is LLM
  synthesis, also needs `memory_reflect`). A bank a policy lists belongs to
  it: other work neither writes nor reads it. A queued retain carries its
  policy, and the worker posts it only to the approved bank.
- **Async and queued work**: worker steps (async ones included) and
  background tasks run inside the origin they were started under.
- **Plugins that call models directly**: `web_search` refuses its
  `claude_code` and `anthropic` backends for restricted work and uses
  `brave` only if the policy lists it; `attachments` refuses Gemini video
  analysis. With pools configured, for public work too: `claude_code` search
  needs a usable Claude subscription in the active pool (a receipt is
  recorded), `anthropic` search (per-search fees no grant bounds) and Gemini
  video (video and audio tokens have no exact bound or rate, so even the google
  spend adapter refuses them) are refused before sending.

These are in-process guards, like the private-pool grants: they stop Ergo's
own code paths. A shell or Python tool running as the same OS user is not
sandboxed by them (see [Private inference](bots.md#private-inference)).

## Ergonaut

- **Routing page → Pools**: the pools you may see, ordered, with up/down
  reorder, status (eligible, exhausted, switched off, over budget, expired,
  not yours), what is selectable now, your grant (budget, spend,
  reservations, window) and, for a pool you own, the grants you issued with
  a form to issue or revoke one. The owner (or an admin) toggles enable,
  rollover-out and automatic entry; admins switch providers. Every change
  sends the version it was made from.
- **Chat header**: a pool picker beside the model picker shows the active
  pool ("Auto: Subscriptions") and switches it for this chat or as the
  default; a restricted bot shows its privacy lock instead.
- **Exhausted pool**: the chat shows the refusal with a button per next pool.

The model picker lists only what the chat may use: the active pool's models
(or a restricted bot's private models).

## Rollout and rollback

1. Deploy: the migrations (`0059_model_pools`, after main's
   `0058_agent_launch_intent`, then `0060_spend_envelopes` and
   `0061_spend_envelope_families`, and `0062_explicit_funding_and_paid_latch`) add the tables; Google #406 adds `0063_google_engine_signatures` on top; with no `pools:` section
   and no rows, behavior is unchanged. `0061` links each issued grant to its
   owner's config envelope (a grant whose owner has none stops working).
   A config revision an owner gave up before the upgrade (under 0060, an
   owner issuing a grant to themselves revoked it) stays revoked until an
   admin restores it.
2. Add `pools:` (owners, prices, budgets with an end date) to providers.yaml;
   check the Routing page's Pools section; issue grants there.
3. Rollback is a **paid-dispatch pause**, retaining schema, pools, grants,
   funding revisions and every unknown hold. An authenticated admin can use
   `set_paid_pause(paused=True, version=<current state version>, by_id=<admin>)` (Ergonaut: Pause paid dispatch); this is a
   code-only control until an operator authorizes its use. Guarded sends and OpenAI, Claude, Google, Fireworks, OpenAI transcription,
   Gemini video and Anthropic web-search attempts recheck the latch. Governed
   ancillary adapters and private Fireworks currently refuse even when unpaused,
   because they lack a pool-bounded authority; privacy remains independently enforced. Do not remove pools,
   clear switches or serve pre-guard code to regain capacity. First pool
   activation sets a durable latch; removing pools afterwards refuses new paid
   routing and unguarded API sends. Never-activated legacy installations retain
   legacy compatibility while the latch is verifiably unactivated. If the state
   or schema cannot be read, paid sends deny.
4. Migration 0062 adds explicit funding, its audit, and the durable latch after
   0061. Existing envelopes activate the latch and start paused; no funding is
   backfilled. Schema upgrade/reversal is a maintenance procedure under an
   independently enforced paid-egress pause across every dispatch role. Reversing
   the migration removes protections from older code: the in-schema pause cannot
   protect a process running code that predates it. Keep external paid egress
   blocked for rollback, preserve a database backup with all ledgers, and do not
   resume before installing guard-capable code and reconciling consent/holds.
5. Paid coding remains unsupported. Native CLI flags, the latch and this pause
   are not certification of arbitrary subprocess/plugin egress. Google #406 must
   be rebased with its signature migration renamed to 0063 after the core 0062;
   its adapter must call the shared `MeteredGuard.check_transport` hook using
   immutable non-secret identity before each attempt. No activation is authorized
   by this implementation or its local test results.

## Example for boundcorp/ergo-bots

Fill in prices and budgets yourself before enabling; a metered model without
a price stays unusable, and a pool without a grant refuses to send.

```yaml
providers:
  # ...existing openai, claude, chatgpt and xai-oauth entries...
  openai:
    type: openai
    api_key_env: OPENAI_API_KEY
    models: # the existing four; add price: {input, cached, output} to each you'll use
      - { name: gpt-6-luna, label: GPT-6 Luna }
      - { name: gpt-6.1-terra, label: GPT-6.1 Terra }
      - { name: gpt-6.1-sol, label: GPT-6.1 Sol }
      - { name: gpt-6-astra, label: GPT-6 Astra }
  anthropic:
    type: claude
    api_key_env: ANTHROPIC_API_KEY
    config: { max_tokens: 32000 }
    models:
      - { name: claude-opus-5-5, label: Opus 5.5 } # add price
      - { name: claude-sonnet-5-5, label: Sonnet 5.5 }
  # once the Gemini engine lands:
  # gemini:
  #   type: google
  #   api_key_env: GEMINI_API_KEY
  #   models: [{name: gemini-3-pro, price: {...}}]

auto_rollover: false
pools:
  - name: subscriptions
    label: Subscriptions
    routes: [claude, chatgpt, xai-oauth]
  - name: paid-api
    label: Paid API
    owner: <username> # the Ergonaut user whose money it is
    routes: [openai, anthropic] # add gemini when it lands
    rollover_out: false
    allow_auto_entry: false
    # budget_usd: <choose>        # the owner's grant; required before anything metered sends
    # expires: <date>             # required with budget_usd
    # per_launch_usd: <choose>    # for metered coding launches (local_shell only)
    tiers:
      cheap: [openai/gpt-6-luna]
      medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5]
      xlarge: [openai/gpt-6-astra]
    agents:
      medium:
        - { agent: codex, model: gpt-6.1-sol, provider: openai }
        - { agent: claude, model: claude-opus-5-5, provider: anthropic }

privacy:
  personal:
    web_search: [brave] # and in personal/bot.yaml: web_search backend: brave (needs BRAVE_API_KEY)
```

`private_pools.personal` stays as it is; it is already the personal privacy
policy. Also update the stale comment above `private_pools` in providers.yaml
(private routing is enforced).
