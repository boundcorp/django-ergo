# Model routing review — 2026-11-09

## Before this change

- **Bot/assistant turns:** `bots/runtime.py` resolves `auto/<tier>` through `bots/routing.py::pick_model`. `providers.py` supplies subscription-only built-in small/medium/large/xlarge candidate lists; providers.yaml can replace each chat or worker list independently. Explicit model pins bypass automatic selection. Selection retained the current model whenever it was under raw percentage caps, otherwise chose the first eligible list entry. If every entry exceeded a cap, it chose the greatest raw remaining percentage. Default catalog ordering and the deployment's lists put Claude first.
- **Agents/workers:** `bots/agents.py::start` calls the same router's `pick_agent` for a tier, which supplies agent, model and effort to managers such as `plugins/orca.py`. `orca_start_worker(tier=...)` reaches that path; explicit model/agent arguments remain available. Worker candidates must be CLI subscriptions, not API keys. The deployment uses omp selectors for both Anthropic and Codex. Tier names describe capability; effort remains independent. Existing aliases and custom tiers are preserved.
- **Deployment guidance:** ergo-bots `providers.yaml`, `devbox/agents.md`, and `devbox/tools/models.py` previously reinforced Claude-first or raw-cap selection. Devbox's `models` and `limits` skills come from its tool files. `ModelsAvailable` is discovery/catalog data, not the core execution allowlist; `AccountLimit` is the devbox's historical usage snapshot, not the router's source of truth. The instruction/config change uses capability tiers by default while retaining explicitly requested model overrides.
- **Usage:** `bots/usage_sync.py` periodically runs `omp usage --redact --json` and maps CLI accounts into `ProviderUsage`. Conversation `usage_windows.py` also merges provider events between syncs. Full snapshots remove obsolete counters; each window has its own observation time. Routing text in `routing.md`, or the saved Routing page override, compiles into optional policy rules. Previously `current_usage()` discarded reset/duration metadata before selection even though the UI could draw elapsed-window pacing markers. Devbox separately refreshes `AccountLimit` through `devbox/tools/limits.py` and renders `devbox/pages/limits.jhtml`.

## Reset-time discrepancy

Fresh read-only upstream evidence at **2026-11-09 18:03:27.762 UTC**, from the authenticated Codex `wham/usage` endpoint:

| Field | Observed value |
| --- | --- |
| Plan | `prolite` |
| Primary used | 23% |
| Primary duration | 604800 seconds (7 days) |
| Primary `reset_at` | 1794413616 = **2026-11-11 16:13:36 UTC** |
| Primary `reset_after_seconds` | 166209 |
| Secondary window | `null` |
| Additional rate limits | `null` |

OMP's redacted observation at **18:00:09.881 UTC** independently returned `openai-codex:primary`, window `7d`, duration 604800000 ms and the same reset. One day of redacted history, Nov 8 18:57:58.376 through Nov 9 18:00:09.881 UTC, contained only that primary weekly limit. OMP handles primary, secondary and additional windows; Ergo iterates every reported limit. **No short Codex window was dropped, and this was not merely a stale local snapshot or a timestamp-unit conversion error.**

The UI's reported Nov 9 ~19:20 reset does not match any freshly fetched upstream window. Distinguishing a different UI/account context from an upstream/UI disagreement still requires the corresponding UI response and account context; this change does not invent a short window or override the upstream reset. Both the UI-reset scenario and the verified weekly-reset scenario select Codex under the new policy.

An actual parsing defect was found and fixed: arbitrary explicit durations were discarded. Canonical `period_seconds` now preserves OMP `durationMs` and Codex CLI `windowDurationMins`. Generic scoped limits such as Grok credits constrain every model; only explicit model scopes constrain a particular family.

## Pace policy

1. For each fresh applicable window, compute `elapsed_frac = clamp(1 - seconds_to_reset / period_seconds, 0, 1)`, `used_frac = used / 100`, `pace_delta = elapsed_frac - used_frac`, and `hours_to_reset`. Positive delta is spare allowance.
2. Weekly score: positive delta times `1 + min(7, 24 / max(1, hours_to_reset))`; negative delta times two. Use the worst applicable weekly window, so a generous scoped budget cannot hide a tighter one. This makes expiring unused quota valuable without unbounded urgency.
3. Short-window score: twice the negative part of delta; it cannot supply a positive reward. More than ten percentage points ahead of short-window pace excludes the candidate. Exhausted applicable windows always exclude it.
4. Apply existing optional raw-percentage caps. If all candidates exceed these soft caps, rank only non-exhausted, short-window-safe candidates. If none remain, report no safe provider instead of choosing an exhausted account.
5. Add optional `routing.soft_preferences` offsets, clamped to ±0.1; default `{}`. Highest score wins; exact ties retain the current chat model, then use the configured capability/cost list order. A cache cannot suppress materially better subscription headroom. All candidates are scored against one clock instant.

Unknown/stale timing earns no urgency bonus. Known utilization still enforces percentage caps/exhaustion; expired windows are omitted pending refresh. Every pick logs its pace reason, worker picks are recorded, and the Routing report includes elapsed/used fractions, pace delta and reset hours. The devbox page shows the same quantities at its observation time, alongside an actual reset countdown, rather than advancing stale usage as if it were fresh.

## Compatibility and operational boundaries

- Existing providers.yaml tier lists, agent entries, aliases, percentage caps, custom tiers, explicit model pins and custom `MODEL_ROUTER(candidates, usage, rules, current)` signatures remain valid. Numeric usage mappings remain numeric for custom routers; timing is attached separately for the built-in scorer. Custom routers intentionally replace built-in selection policy.
- Compiled text policies are versioned to recompile with pace-aware instructions. Saved Routing-page text still overrides file policy and should be reviewed if it explicitly requests old provider-specific percentage caps.
- Deploy the matching library before the bot config: devbox limits imports the new shared `window_pace` helper. No database migration or secrets change is needed.
- Explicit retry model selections apply to the next turn before normal pace routing resumes. Agent tiers do not require the provider CLI on the Ergonaut host; remote managers execute the agent elsewhere. Legacy model-scoped windows without metadata remain scoped by name.
- Devbox's fresh limits check also syncs the report to the router; a cached dashboard reading does not prove the router snapshot is fresh, and sync errors are surfaced.
- Draft PRs only. No deployments, pod restarts, live policy edits or secret changes were performed.

## Verification

- Django-Ergo affected suites: **183 passed** (`test_bot_routing`, `test_routing_tiers`, `test_bot_providers`, `test_private_inference`, `test_usage_sync`, `test_bot_agents`, `test_import_layers`). One environment warning: XDG_RUNTIME_DIR fallback.
- Ergo-bots affected suites: **35 passed** (`devbox/tools/test_models.py`, `devbox/tools/test_limits.py`) with PYTHONPATH explicitly pointing at the matching library worktree.
- Standalone configured-routing smoke: Lee's hypothetical near-reset Codex window selected `chatgpt/gpt-6.1-sol` (76% under weekly pace, 1.4h remaining); verified upstream reset also selected it (49% under pace, 46.8h remaining); Claude short-window overpace avoided Claude; all exhausted returned no candidate. Fresh redacted live usage selected ChatGPT, about 50% under weekly pace with 46.1h remaining.
- Actual devbox template rendered through Django's page renderer and viewed in Chromium: positive/negative pace, elapsed markers, custom duration, unknown usage and reset-due states observed. This was a local sample-data smoke, not a deployed dashboard check.
- Changed Django-Ergo Python files pass the repository-pinned Ruff 0.12.5 check; changed ergo-bots Python files pass Ruff. No deployment or live-session invocation was needed for verification.
