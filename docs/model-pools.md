# Model pools, rollover and privacy

A **pool** is a named, ordered group of model routes (a provider's models,
or one exact `provider/model`) that every model consumer picks from: bot
chats and threads, titles, browser tasks, coding agents (`ergo_agent_start`,
`orca_start_worker`, `local_shell`), background tasks and workers. Pools are
preference, not billing classes: a pool may mix subscription (CLI) and
metered (API key) routes. Billing, price, transport and account are route
metadata on the provider.

A **privacy policy** is separate. It says where a restricted origin's data
may go, intersects with whatever pool is active, and is inherited by
everything that origin starts. Switching pools never widens it.

Everything resolves through one module, `django_ergo.bots.pools`:

```text
privacy_for(bot, session)        the inherited policy (bot, folder ancestors, chat, turn)
snapshot(providers, user, chat)  pool config + runtime state (order, toggles, budgets, spend)
select_route(...) / select_agent(...)
    -> Decision (pool, route, billing, rejected candidates, receipt id, spend envelope)
    -> or PoolRefusal(code, message, next_choices)
listing(bot, session)            what the pickers show, from the same eligibility rules
```

Each selection leaves a `RouteDecision` receipt (no prompts, no secrets).

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
    routes: [openai, anthropic/claude-opus-5-5]
    budget_usd: 50 # required while the pool has a metered route
    expires: 2026-12-31 # optional; after it the pool is ineligible
    per_launch_usd: 5 # reserved per metered coding-agent launch
    max_output_tokens: 16000 # output limit set on metered requests that have none
    rollover_out: false # default: true only for subscription-only pools
    allow_auto_entry: false # default: false when any route is metered
    tiers: { medium: [openai/gpt-6.1-sol, anthropic/claude-opus-5-5] }
    agents:
      medium: [{ agent: codex, model: gpt-6.1-sol, provider: openai }]
```

| Key                | Meaning                                                                                                                                                        |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `routes`           | Provider names (all their models) or `provider/model`.                                                                                                         |
| `tiers`            | Optional. Without it a tier is the global tier's candidates that are in the pool, else the built-in catalog (`DEFAULT_TIER_MODELS`) over the pool's models. |
| `agents`           | Optional coding candidates, like the global `agents` but allowed to name metered providers. Without it: the global agents in the pool, else derived from tiers. |
| `budget_usd`       | Cumulative US dollars for the pool's metered routes (all time). No budget, no metered sends.                                                                    |
| `expires`          | Date or ISO date-time (UTC). After it the pool can't be selected and its guards refuse.                                                                         |
| `per_launch_usd`   | Reserved against the budget for each metered coding launch (and each resumed run).                                                                             |
| `rollover_out`     | This pool may hand over to the next one when exhausted.                                                                                                        |
| `allow_auto_entry` | The pool may be entered automatically by rollover.                                                                                                             |
| `enabled`          | Off = skipped in automatic order; a manual pick of it is refused.                                                                                              |

A provider takes `enabled` and `billing` (`subscription` or `metered`). A
metered model needs an exact `price` (USD per million input, cached input and
output tokens); without one it shows as unavailable and can't be pinned.

Ergonaut keeps the runtime state in the database (tables
`ModelPoolState`, `ModelPoolPreference`, `ProviderToggle`, `RouteDecision`):
order, the active pool, toggles, budget, expiry and per-launch overrides,
all versioned. providers.yaml stays the source of what exists.

## Selecting a pool

- **Automatic** (the default, "use preferred order"): the first enabled pool
  in the user's order that the origin's privacy policy allows.
- **Manual**: a user-wide default (`ModelPoolPreference.active_pool`) or a
  per-chat override (`session.metadata["pool"]`). New turns and new
  descendants of that chat use it; a running turn or an already launched
  agent is never restarted. Clearing the pick returns to preferred order;
  coming back to an earlier pool after its quota resets is always explicit.

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

Otherwise the request stops with a `pool_exhausted` refusal that names the
next pools; the chat shows it with a button to switch.

These are **never** rollover triggers; each is its own refusal: a switched
off provider (`provider_disabled`), a missing key or CLI (`unavailable`), a
model without a price (`price_unknown`), privacy (`privacy_denied`), a
missing, used or expired budget (`budget_required`, `budget_exhausted`,
`budget_expired`), a switched-off pool (`pool_disabled`), a pin outside the
pool (`pin_out_of_pool`), and for coding `no_coding_route` and
`unconfinable`.

## Metered spend

A configured API key is never authority by itself. With pools configured,
every metered API engine a bot builds carries a spend guard
(`conversation.spend_guard`):

- built from a pool decision, it reserves each request's worst-case cost
  (request bytes as uncached input tokens plus the output limit) under a
  PostgreSQL advisory lock, after re-reading the pool's budget, expiry and
  on/off state and the provider switch, and refuses (no HTTP) when it can't;
- after the response it prices the reported usage exactly; a request that
  may have reached the provider keeps its reservation;
- an engine for a metered route that wasn't selected through a pool gets a
  guard that refuses every send.

Attempts are `InferenceRequest` rows with `pool` `pool:<name>` and
`billing_mode` `metered`, so the existing cost ledger and Costs page see
them. Spend counts toward `budget_usd` for all time; raise the budget (or
set a new one) to continue.

## Coding agents

Coding workers inherit the active pool. The blanket rule that coding may
only use subscription CLIs is gone; the native-CLI mapping, login health,
OMP session-log proof, admission leases and scoped Orca checks still apply.

**Metered coding is refused on every shipped agent manager today.** The
pool can list metered coding routes (and `ergo_models_list` shows them), but a
launch on one needs a manager that declares both:

- `AgentManager.metered_confinement`: the process is held to exactly one
  provider key and model, and
- `AgentManager.metered_spend_bound`: the launch's *total* spend (every turn,
  retry and child process) is enforced, not just reserved.

| Manager       | Confinement                                                                                                                                                         | Total spend bound | Metered coding |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- | -------------- |
| `local_shell` | yes, as defense in depth: only the selected key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`/`CODEX_API_KEY`), other credentials/endpoints/cloud switches removed, job-local `HOME`, explicit `--model` | no                | refused        |
| `orca`        | no (Orca terminals own their environment)                                                                                                                           | no                | refused        |
| any other     | no (the `AgentManager` default)                                                                                                                                     | no                | refused        |

Why: an isolated environment and explicit model flags are necessary but are
not proof. A CLI can read other credential stores or project config, has its
own fallback logic, can spawn children, and runs many turns with no enforced
dollar or token ceiling; a ledger reservation (`per_launch_usd`) doesn't cap
that. omp is refused on metered routes in any case (its provider fallback
can't be switched off).

When a manager can demonstrate both, the path is already wired: the launch
reserves `per_launch_usd` against the pool budget before starting (and again
for a resumed run), the spec carries `AgentSpec.confinement`, and the
reservation is never released without billing evidence.

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
- **History**: a session under a policy (its bot's or an inherited origin)
  is readable by history search, links and file sharing only from work
  under that same policy.
- **Plugins that call models directly**: `web_search` refuses its
  `claude_code` and `anthropic` backends for restricted work and uses
  `brave` only if the policy lists it; `attachments` refuses Gemini video
  analysis.

These are in-process guards, like the private-pool grants: they stop Ergo's
own code paths. A shell or Python tool running as the same OS user is not
sandboxed by them (see [Private inference](bots.md#private-inference)).

## Ergonaut

- **Routing page → Pools**: ordered pools with up/down reorder, per-pool
  enable, rollover-out and automatic-entry toggles, budget, spend and
  reservations, expiry and status (eligible, exhausted, switched off, over
  budget, expired), the global automatic-rollover toggle and provider
  switches. Changes are version-checked; admins only.
- **Chat header**: a pool picker beside the model picker shows the active
  pool ("Auto: Subscriptions") and switches it for this chat or as the
  default; a restricted bot shows its privacy lock instead.
- **Exhausted pool**: the chat shows the refusal with a button per next pool.

The model picker lists only what the chat may use: the active pool's models
(or a restricted bot's private models).

## Rollout and rollback

1. Deploy: the migration adds four tables; with no `pools:` section and no
   rows, behavior is unchanged.
2. Add `pools:` (and prices, budgets) to providers.yaml; check the Routing
   page's Pools section; set budgets there or in YAML.
3. Rollback: remove the `pools:` section (behavior returns to legacy at the
   next reload), or revert the deploy; the new tables can stay. Provider
   switches set in the UI also apply in legacy mode; clear them on the
   Routing page.

## Example for boundcorp/ergo-bots

Fill in prices and budgets yourself before enabling; a metered model without
a price stays unusable, and a pool without a budget refuses to send.

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
    routes: [openai, anthropic] # add gemini when it lands
    rollover_out: false
    allow_auto_entry: false
    # budget_usd: <choose>        # required before anything metered sends
    # expires: <date>
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
