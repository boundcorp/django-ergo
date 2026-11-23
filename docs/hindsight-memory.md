# Hindsight working memory

Hindsight is an opt-in, automatic memory layer alongside the git/Markdown KB. It does not change `ergo_kb` or the KB files. Add it to a bot's `bot.yaml`:

```yaml
plugins:
  - name: hindsight_memory
    bank: ergonaut
    retain: after_turn       # after_turn | off
    prefetch: every_turn     # new_session | every_turn | off
    top_k: 5
    budget: low              # low | mid | high
    timeout_seconds: 3
    scope: bot_first         # bot_first | user
    min_score: 0.5           # reranker floor, bot-scoped recall (0 disables)
    min_score_shared: 0.8    # reranker floor for the user-wide pass
    telegram_users: []       # Telegram sender ids to treat as the chat's human
    mental_models: []        # optional, off by default (see below)
    top_k_with_models: 3
```

Set `HINDSIGHT_URL` in Ergonaut's environment, for Octo `http://hindsight.llm.svc.cluster.local:8888`. Set `HINDSIGHT_API_KEY` only if the API is configured to require a bearer key. With no URL, the plugin disables itself and logs one warning per bot load. No bot enables it by default.

## Config is the source of truth

A bot's `bot.yaml`, `agents.md`, `docs/`, skills and `ENVIRONMENT.md` own its rules, policies, tools, tiers, hosts and procedures. Memory holds only what they do not: the user's decisions and preferences (especially ones not yet in config), outcomes, incidents, and app or project state. Copies of config in memory go stale and contradict the real thing, so the plugin keeps them out at retain time and out of recall when they are noise.

## What is retained

After a completed human turn, Ergonaut queues one document containing the user message and the cleaned assistant reply on its Celery `bot_tasks` queue. Hindsight processes it asynchronously. The document ID uses the turn ID so retries replace the same document. Each item is tagged `bot:`, `chat:`, `user:`, `source:chat` and, for child sessions, `thread:`. Its `context` names the bot, says its config and docs are authoritative, and tells the extractor to keep only decisions, preferences, outcomes, incidents and app/project state, never assistant offers, plans, check-in times, routing or relay narration, or restatements of rules or config.

What is filtered before anything is sent (deterministic, no model call):

- Empty, failed and approval-pending turns.
- Machine turns: authors that are not a person (system, bots, workers), turns with message provenance (forwarded or bot-to-bot), recurring wakes and periodic check-ins.
- Turns whose only tool calls were reading config (`ergo_self_read`, `ergo_self_files`, `ergo_skill_load`, `ergo_config_repo_status/list/read/grep/diff`), unless the user message carries a decision or preference ("from now on", "always", "use", "approved", ...). These restate config.
- From the assistant text: `Suggested replies` and similar menus, status and check-in lines (`Status: ...`, `Next check-in: ...`), trailing option lists under a closing question, and trailing offers ("Want me to ...?").

`memory_retain(text)` still saves exactly what the user asked for, tagged `source:explicit`.

### Telegram

Telegram senders are not mapped to Django users automatically; the Telegram plugin only lets the listed `respond_to.users` talk to the bot, and the chat belongs to the thread owner. The plugin treats a `telegram_user` author as a person only if its id is listed in `telegram_users`; otherwise the turn counts as a machine turn (no retain, no prefetch). List the ids of senders who are the chat's owner. Everything they say is stored under the owner's `user:` tag.

## Prefetch

Prefetch recalls relevant facts for the incoming message into the turn's context block. It does not write them to conversation history and stops waiting after `timeout_seconds`.

- **Human turns only.** It is skipped for the same turns retain skips, so worker events, self-wakes and bot-to-bot messages pay no recall latency and get no user facts.
- **Hygiene.** Requests use `prefer_observations`, `query_timestamp` (now) and a reranker floor (`min_scores.reranker`). A recall under the floor returns an empty block, not junk.
- **Bot-scoped first.** With `scope: bot_first` (default) two requests run concurrently under the same timeout. The first is bot-scoped (`bot:<self>` AND `user:<id>`, via `tag_groups`) with `min_score`, and fills at most `top_k - 2` slots. The second is user-wide with the higher `min_score_shared`, and fills the rest. Near-duplicates (token similarity of 0.7 or more) are dropped, bot-scoped first. With `scope: user` a single user-wide request with `min_score` is made.
- **Floors.** The defaults come from the audit's 60-probe set: the reranker scores are bimodal. Kitchen's best score was 0.04 (all junk), design's mostly 0.0 to 0.2 except ficsit "design" facts at 0.94 to 1.0, devbox/storytime/ficsit relevant hits mostly 0.5 to 1.0. 0.5 removes all kitchen noise and keeps 74% to 98% of devbox, storytime and ficsit top-5 slots; 0.8 for the shared pass keeps only strong cross-bot facts (the "Grok allowance" kind). A semantically strong fact from another bot cannot be separated by a floor; use `scope: user` or a bot-specific bank if a bot must never see them. Set `min_score: 0` to turn floors off.
- Each injected fact's id, text and scope (`bot`, `shared`, `user`) is saved in the turn's `StructuredCall.metadata["context"]["sections"]` under `details.hindsight`, so later analysis can attribute usage.

The skill offers `memory_recall(query)`, `memory_reflect(question)` (slower), and `memory_retain(text)` for explicit memory requests. `memory_recall` is user-wide with no floor. Bank selection comes only from bot configuration. Recall and reflect require the current user's tag so memories cannot cross users. A future bank template can be added at the plugin's `_bank` method without changing model-visible tools.

## Mental models (optional, off by default)

`mental_models: [id, ...]` fetches those Hindsight mental models (content cached for 10 minutes) and injects them as a stable "Working memory" block before the recalled facts, which then drop to `top_k_with_models` (default 3). A cold fetch never waits past `timeout_seconds`: it finishes in the background and the next turn uses the cache (a stale copy is used if a refresh fails). Models are bank-wide; keep config and rules out of them.

## Metrics

Ergonaut exposes `ergonaut_memory_prefetch_total{bot,outcome,reason}` (outcome `ok`, `empty`, `timeout`, `error`, `skipped`), `ergonaut_memory_prefetch_duration_seconds{bot}`, `ergonaut_memory_prefetch_facts{bot}` and `ergonaut_memory_retain_total{bot,outcome,reason}` (outcome `queued` or `skipped`; reasons such as `config_only`, `provenance`, `author`, `recurring_wake`, `telegram_unmapped`, `empty`, `failed`). The library emits `django_ergo.signals.memory_event`; Ergonaut turns it into the metrics above.

The API uses the [Hindsight retain](https://hindsight.vectorize.io/developer/api/retain), [recall](https://hindsight.vectorize.io/developer/api/recall), and [reflect](https://hindsight.vectorize.io/developer/api/reflect) endpoints.
