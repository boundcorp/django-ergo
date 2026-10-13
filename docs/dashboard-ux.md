# Main dashboard UX

## Scope and current capabilities

The signed-in landing route `/` is a Workspace page, not the main dashboard.
It greets the user, offers a new-thread link for every bot, and shows the first
four sessions in API order, with attention, busy, unread and last-updated hints.
The main dashboard is `/threads`: all thread sessions plus open main/named chats,
optionally filtered by the `bot` URL parameter.

- Groups: Pinned, Ready for review, Waiting on you, Working, Idle, Resolved.
  Pinning overrides group placement. Rows sort by last activity, falling back
  to updated time. Idle and Resolved initially show five rows; Show more reveals
  the rest. Group expansion persists at `ergonaut.threads.page`.
- Thread rows open chats, show bot identity, status/approval/question/failure,
  unread replies, running workers and up to two reported PR links with CI/state,
  last activity, resolved tooltip and pin/unpin. The close control goes back.
- Sidebar: bot main/named chats (including starting missing chats), new threads
  for orchestrating bots, bot tools/skills/settings, per-bot thread filters,
  Memory, pinned files/pages opening inside the owning chat, bot-load errors,
  version footer and Threads waiting count.
- Shell: breadcrumbs/home, active worker progress, subscription utilization,
  account/theme/sign-out, History message search and status/bot filters, Costs,
  Routing and API keys. Phone navigation is a focus-trapped modal drawer.
- App refresh reads `/api/bots`, `/api/sessions`, `/api/pins`,
  `/api/bot-errors` and `/api/workers`; it polls every 3 seconds while
  any session is busy, otherwise every 20 seconds. Chat streaming is separate.
  These existing server-derived states and permissions remain authoritative.
- Backend `ergonaut/ergonaut/api/bots.py` derives session state from the latest
  `chat_reply` StructuredCall, delegated ThreadMessages, workers and recorded PR
  attachments. Completed outranks waiting, waiting outranks working; review
  requires an open PR. `/sessions` still caps at 200 by `updated_at` before
  frontend grouping/activity sort. Admins see all permitted sessions; other
  users see their own. `/pins` is owner-only even for admins. No new endpoint,
  websocket feed or backend behavior is introduced.

## Friction → direction

| Friction | Change |
| --- | --- |
| Greeting dominates the dashboard; next action is buried in lists | Task-first Threads heading, compact activity jumps (including pinned attention items) and a direct New thread jump |
| Landing page prioritizes a large decorative start card over existing work | Put recent conversations first; add a clear route to the full dashboard |
| Bot starts and pinned dashboards require the phone navigation drawer | Compact workspace rail exposes existing new-thread links and pinned files/pages |
| Dense rows truncate status and hide PR/worker evidence on phones | Responsive two-line rows with wrapping status, visible PR/worker chips, and explicit unread/working text |
| Pin control depends on hover, has no accessible name and weak touch target | Always-visible named pin button with a 44px target and failure feedback |
| Every group header looks identical | Clear section labels, counts and restrained attention/review accents, preserving order |
| Initial fetch appears empty; failures disappear | Explicit loading and retryable error states; retained data stays visible after refresh failure |
| Icon-only close is ambiguous | Labeled Back action with existing browser-history behavior |

## Recommendation and unchanged defaults

Keep `/` as a lightweight launchpad and `/threads` as the operational dashboard.
Do not redirect login or change the default route. Keep Pinned first, even if a
pinned item is waiting: this honors the current mental model and saved choices.
Use existing Tailwind semantic tokens, BotBadge/PrChip and shell conventions;
no new framework or backend API is necessary. Desktop uses a main thread column
and a narrow launch/pins rail; phone uses the same DOM order, one column.

No sort, filter, group order, cap, expansion setting, session inclusion or
permission defaults change. The new dashboard rail deliberately repeats existing
navigation capabilities outside the drawer. Landing recent sessions remain capped
at four, in API order. Counters describe loaded sessions, not global totals.
Activity jumps target the first loaded matching item's group, including Pinned;
they do not change filters or override remembered collapsed groups.

### Open decisions for Lee

- Should `/threads` eventually become the signed-in landing page? This pass
  keeps the current route and makes the dashboard one explicit click away.
- Should Waiting on you outrank Pinned? This pass retains Pinned first.
- The workspace rail shows new-thread links for every loaded bot, matching the
  current landing page; the server still decides whether thread creation is allowed.

## Visual evidence

Synthetic local bots and sessions only; desktop 1440×1000, phone 390×844.
Before captures use origin/main `1cea2e5254456834adec8d191e19100d2996eb39`.
Screenshots live in [screenshots/dashboard-ux/](screenshots/dashboard-ux/).

| State | Desktop before / after | Phone before / after |
| --- | --- | --- |
| Dashboard | [Before](screenshots/dashboard-ux/before-desktop.png) / [After](screenshots/dashboard-ux/after-desktop.png) | [Before](screenshots/dashboard-ux/before-mobile.png) / [After](screenshots/dashboard-ux/after-mobile.png) |
| Workspace | [Before](screenshots/dashboard-ux/before-home-desktop.png) / [After](screenshots/dashboard-ux/after-home-desktop.png) | [Before](screenshots/dashboard-ux/before-home-mobile.png) / [After](screenshots/dashboard-ux/after-home-mobile.png) |
| Empty dashboard | [Before](screenshots/dashboard-ux/before-empty-desktop.png) / [After](screenshots/dashboard-ux/after-empty-desktop.png) | [Before](screenshots/dashboard-ux/before-empty-mobile.png) / [After](screenshots/dashboard-ux/after-empty-mobile.png) |
| Expanded many-item list | [Before](screenshots/dashboard-ux/before-many-desktop.png) / [After](screenshots/dashboard-ux/after-many-desktop.png) | [Before](screenshots/dashboard-ux/before-many-mobile.png) / [After](screenshots/dashboard-ux/after-many-mobile.png) |
| Refresh failure, retained items | [After](screenshots/dashboard-ux/after-error-desktop.png) | [After](screenshots/dashboard-ux/after-error-mobile.png) |
| Loading | [After](screenshots/dashboard-ux/after-loading-desktop.png) | [After](screenshots/dashboard-ux/after-loading-mobile.png) |

The 27-conversation fixture includes three bots, open main chats, approval,
question, failure, running workers, delegation, unread replies, open/draft PRs,
pinned files/pages, idle and resolved threads. The empty user has no sessions.
Headless Chromium uses dark theme, UTC/en-US, device scale 1, reduced motion and
a fixed client clock. Real isolated PostgreSQL and authenticated Django APIs
serve the data; no production data, secrets, model calls or committed browser
dependency. Loading/error captures deliberately hold/fail the session-list
request; error recovery and pin persistence were also exercised against Django.

## Verification

- `npm test`: 12 Node tests and 115 Bun tests passed, including six new
  dashboard behavior tests.
- `npx tsc --noEmit` and `npm run build`: passed.
- Prettier 3.3.3 check on all changed frontend source: passed. No ESLint or
  frontend lint script exists; Prettier is the repo's frontend lint convention.
- `ruff check ergonaut/ergonaut` and `ruff format --check ergonaut/ergonaut`:
  passed, 100 Python files already formatted.
- Backend `test_api.py`, `test_stream.py`, `test_reloading.py`: 49 passed.
  Warnings: local default secret/static directory/runtime-dir and a test DB
  teardown connection warning; no backend source or settings changed.
- Browser smoke: bot filter, five-to-seventeen Idle expansion, persisted
  collapse, activity/New thread focus jumps, API-persisted pin/unpin round trip,
  phone drawer Escape/focus return, recent-four landing, retry with retained
  items and initial loading. No horizontal overflow at 1440, 390 or 320px.
- Vite's >500 kB chunk warning also occurs on base main: base JS 668.88 kB,
  redesigned JS 675.25 kB. Both production builds succeed.
- `pre-commit run --files` for changed frontend/docs/backlog files: all
  applicable hooks passed (whitespace, EOF, JSON, case conflicts, secret-key
  detection, Prettier); Python/template hooks had no matching changed files.
