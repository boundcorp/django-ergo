# Spec: Usage section on Ergonaut's Costs page

Repo: boundcorp/django-ergo (public; octo auto-upgrades from main, so keep deploy details out).
Files: `ergonaut/ergonaut/api/costs.py`, `ergonaut/frontend/src/pages/Costs.tsx`, `ergonaut/frontend/src/api.ts`, `ergonaut/ergonaut/apps/bots/tests/test_costs.py`, `docs/` (update the Ergonaut guide).
Do NOT touch `Sidebar.tsx` (the upgrade thread owns it, #120).

## Goal
A Usage section at the top of the Costs page, in the style of a project-usage panel: headline stats, a token-mix bar, and a per-thread table. Subscription (Claude CLI) usage is shown as tokens, not list-price dollars.

## Headline stats (range: 7/30/90 days, optional bot filter)
- Threads: distinct sessions with at least one call in range.
- Tokens: input + output + cache write + cache read.
- Cache hit: cache_read / (input + cache_write + cache_read).
- Main chats: share of tokens from main/named (window) chats vs thread chats.
- Subscription: share of tokens from sessions whose `transport_type == "cli"`.
- API spend: dollars, from API-transport calls only.
- Compaction: share of tokens from compaction calls (kind, not chat replies).
- Code changes (+/-): out of scope for v1; no data is recorded yet.

## Token-mix bar
One stacked bar of input, output, cache write, cache read, with a legend row of counts underneath (reuse the existing PARTS).

## Thread table
One row per session: title (link to the chat), bot, model, a share bar, tokens, cache hit, share of total. Cost shown as dollars only for API-transport rows; CLI rows show "sub". Sort by share by default. Group by Thread / Bot / Model. Sessions with no title fall back to bot + "main".

## Backend
Extend `GET /api/costs` (keep existing fields so the current page keeps working) with `usage`: `{headline, threads[]}`. Group `StructuredCall` by `session`; take transport from `session.transport_type`. In `pricing`/`costs.py`, stop pricing CLI-transport calls as dollars: report their tokens and `cost = 0` with a `subscription` flag, and leave them out of "estimated spend".
Superusers see everyone's calls; others see their own (unchanged).

## Tests
Extend `test_costs.py`: cache-hit math, subscription excluded from dollars but counted in tokens, grouping by session, per-user scoping.

## Acceptance
`cd ergonaut && make test` and frontend lint/typecheck pass. Screenshot of the page in light and dark attached to the PR.
