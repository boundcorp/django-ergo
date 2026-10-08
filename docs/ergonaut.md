# Running Ergonaut

Ergonaut is the Django project in `ergonaut/` that hosts bot folders. It
runs the web app and API, Celery workers for turns and background work,
Celery beat for schedules and housekeeping, and each bot's long-running
plugins such as Telegram. It lives in this repo so Ergo changes and the
Ergonaut code that uses them land together.

Each assistant reply has a **Context** button showing its first request's
context sections, native message count, stubbed tool results, active summary,
and the model window. Prompt tokens are the turn total from the call's usage
fields. Sections and the active summary expand to their recorded text;
click an “Earlier messages summarized” divider to read an older summary.
Older replies made before context recording have no Context button. See
[Session compaction](compaction.md) for policy keys and migration rollout.

## Which bots it runs

`ERGONAUT_BOTS` (default `/bot`) is a path, or several joined with `:`.
Every folder at or under each path with a `bot.yaml` is a bot; a bot folder
inside another is its sub-bot.

A path can instead hold an `ergonaut.yaml` listing bot folders and the
people who use them:

```yaml
bots: [kitchen, ../sysadmin]
people:
  lee: {telegram: 123456789, timezone: America/Los_Angeles, email: lee@example.com, name: Lee Bound}
```

Each person becomes a Django user (created without a password; set one with
`ergonaut manage changepassword`), and their Telegram id reaches every
Telegram plugin. `people:` may also sit in a bot's own bot.yaml.

## Commands

| Command | What it does |
| --- | --- |
| `ergonaut up [ROLES]` | Everything in one process tree (below) |
| `ergonaut web` | Migrate, then serve the web app, API, admin and webhooks |
| `ergonaut worker` / `ergonaut beat` | Celery worker and scheduler |
| `ergonaut bots` | Every bot's long-running plugins (Telegram polling or webhook setup) |
| `ergonaut check` | Load the bots; list skills, tools, plugins, people and missing secrets |
| `ergonaut chat BOT [--user NAME]` | Chat with a bot's main chat in the terminal |
| `ergonaut manage ...` | Any `manage.py` command, e.g. `ergo_bot_makemigrations`, `ergo_bot_preview` |
| `ergonaut manage wait_idle` | Wait until no bot turn or worker is running, before a restart |
| `ergonaut remote ...` | Use another Ergonaut server's bots over its API (the [`ergonaut-remote` client](agent-skills.md)) |
| `ergonaut manage api_key create USER --name WHERE` | Make an API key (also `list`, `revoke ID`); see [API keys](#api-keys) |
| `ergonaut upgrade [--check]` | Upgrade to the newest GitHub release once idle (see [Upgrading](#upgrading)) |

Commands other than `up` find a running `ergonaut up` on the same machine
(through `DATA_DIR/up.json`) and use its database, broker and storage, so
`ergonaut manage shell` next to it sees the same data and queues work to the
same workers.

## ergonaut up

`up` starts only what you haven't supplied:

| Setting | Unset | Set |
| --- | --- | --- |
| `DATABASE_URL` | embedded PostgreSQL with pgvector (pgserver) | use it |
| `CELERY_BROKER_URL` | `redis-server` if installed, else no broker | use it |
| `S3_ENDPOINT_URL` | `garage` with a generated key and bucket, if installed, else local files | use it |
| `SECRET_KEY` | generated once and kept in `DATA_DIR/secret_key` | use it |

Then it runs `migrate`, applies each bot folder's table migrations
(`ergo_bot_migrate`), and starts the web server (port `PORT`, default
8000), a Celery worker, a second worker for the `bot_tasks` queue, beat,
and the bots process. If any one exits, it stops them all.

Pass roles to run a subset: `ergonaut up web bots`.

Without a broker there is no worker or beat. Turns then run inside the
web process, and schedules, worker polling and repo pulls by beat don't
happen, so install Redis for anything beyond trying a bot.

Data lives under `DATA_DIR` (default `~/.ergonaut`, `/data` in the image).

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `ERGONAUT_BOTS` | `/bot` | bot folders to serve (`:`-separated) |
| `ERGONAUT_PUBLIC_URL` | unset | this server's public URL; turns on webhooks (`<url>/hooks/...`), so Telegram uses a webhook instead of polling |
| `ERGONAUT_BOTS_PULL_SECONDS` | unset | fast-forward each clean bot checkout from its upstream on this interval, so merged PRs go live |
| `DATA_DIR` | `~/.ergonaut` | embedded database, Redis, Garage, secret key |
| `PORT` | `8000` | web server port |
| `DATABASE_URL`, `CELERY_BROKER_URL`, `S3_ENDPOINT_URL` | unset | external services (see above) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_STORAGE_BUCKET_NAME` | | S3 credentials when `S3_ENDPOINT_URL` is set |
| `MEDIA_ROOT` | `DATA_DIR/media` under `up`, else `static/uploads` in the app | where attachments are stored without S3. Running the roles as separate containers (Kubernetes) without `up`, point it at a volume every container mounts: the default is inside the image, so each container gets its own copy and a restart wipes it |
| `SECRET_KEY` | generated by `up` | Django secret key; the web refuses the insecure default outside `DEBUG` unless `ERGONAUT_ALLOW_DEFAULT_SECRET=1` |
| `DEBUG` | false | Django debug mode |
| `ERGONAUT_BEHIND_TLS_PROXY` | unset | set to `true` behind a proxy that ends TLS (a Kubernetes ingress): trusts `X-Forwarded-Proto` so logins and CSRF work over https, and marks cookies secure |
| `ERGONAUT_ALLOWED_HOSTS` | any host | comma-separated host names the web answers to (localhost always) |
| `ERGONAUT_CSRF_TRUSTED_ORIGINS` | `BASE_URL` | extra origins (comma-separated) allowed to send writes, outside `DEBUG` |
| `TELEMETRY_METRICS_TOKEN` | unset | outside `DEBUG`, `/metrics/` answers only with `Authorization: Bearer <token>` or an admin's session |
| `SENTRY_BACKEND_URL` | unset | Sentry DSN |
| `ERGONAUT_UPGRADER`, `ERGONAUT_AUTO_UPGRADE_SECONDS` | unset | how and how often to upgrade to new releases; see [Upgrading](#upgrading) |

Which models chats may use comes from a `providers.yaml` at the top of the
bot path; see [Models and providers](building-bots.md#models-and-providers).

Bots read their own secrets (API keys, tokens) from the environment by the
names their bot.yaml and tools use: `engine.api_key_env`, `token_env`,
`ctx.secret("NAME")`, or the SDK default `OPENAI_API_KEY` (plus
`ANTHROPIC_API_KEY` if a bot uses the optional Claude engine).
`ergonaut check` lists any that are missing. Keep them in an env file
outside the bot repo.

A provider with `transport: cli` needs the Claude Code CLI installed where
turns run (the worker) and logged in, either in its `CLAUDE_CONFIG_DIR` or
with `CLAUDE_CODE_OAUTH_TOKEN`; see
[Claude on your subscription](building-bots.md#claude-on-your-subscription).
An `openai` provider with `transport: cli` needs the Codex CLI there instead,
logged in with ChatGPT (`codex login`, kept in its `CODEX_HOME`); see
[OpenAI on your ChatGPT subscription](building-bots.md#openai-on-your-chatgpt-subscription).

## The web app

Sign in with a Django user. Admins see everything; other users see the
bots whose `permissions.users` include them (or that set none), and only
their own sessions. Only admins can approve tools of bots with the `bash`,
`orca` or `bot_management` plugins. After 10 wrong passwords for one
username (or 50 from one address) in 15 minutes, the web app and `/mgmt/`
refuse logins until the window passes.

- **Sidebar and account menu**: the sidebar keeps primary navigation: Bots,
  their chats and threads, and Threads. History, Costs, Routing, and API keys
  live in the account menu in the top bar, together with theme and sign-out
  actions. The account menu is keyboard accessible; Arrow keys, Home and End
  move between menu items, and Escape closes it.
- **Top bar**: shows where you are: a chat shows its bot and parent thread
  as links, then its title; bot pages show the bot name; the home page shows
  Workspace. When queued or running workers exist, its Work control names
  the active count and opens their status (queued or running) and reported progress. When subscription
  providers (Claude Code or Codex CLIs) are configured, a usage control shows
  one ring per limit window the provider reported (5-hour, weekly, a model's
  weekly limit): the ring fills with the percentage used and turns amber
  within 15 points of the routing skip threshold and red past it, and a tick
  marks how much of the window has passed, so a fill ahead of the tick is
  burning faster than the window allows. Clicking it opens the detail: the
  tightest limit, each window's bar, reset countdown and pace, when it was
  last reported, and links to Routing and Costs. Providers only report
  utilization percentages, so there are no token or message counts left,
  and a window shows "not reported" from its reset until the next usage
  report.
- **Narrow screens**: the sidebar becomes a modal navigation drawer. Opening
  it moves focus into the drawer; Tab stays inside it and Escape or its
  backdrop closes it and returns focus to Menu.
- **Chat**: the header shows the bot's name above the thread title (both
  truncate). On a phone (640px and under) the site header hides the account
  address (it stays in the menu) and the session title, meta, actions, waiting
  list, workers and pins collapse into one bar. Opening that bar shows them in
  a sheet. The composer is a one-line box, with attach, the model line and
  send on one row. Wider screens keep the full header. The model (with a
  `providers.yaml`) shows in the composer as "Model: Sonnet 5.5"; it opens the
  thread options, where the picker is: an Options dropdown in the header, or
  the top of the phone sheet. The page itself never scrolls: the transcript
  (Markdown rendered) scrolls inside it, opening at the latest message, and
  the composer stays docked at the bottom, above the phone keyboard. The
  transcript shows tool calls you can open, images a
  tool returned under its call, files the bot made where it made them,
  approvals with
  Approve and Deny, suggested replies as buttons, file uploads and pasted
  images. Send while the bot is working to steer the running turn; Stop
  ends it, and Stop & send interrupts it with your message. Pinned files
  and pages open as tabs above the transcript. The transcript follows new
  messages only while you're at the bottom; scrolled up, it stays put and a
  "Jump to bottom" button (or "New messages") takes you back. Sending a
  message always scrolls to the bottom.
- **Forwarded messages**: a distinct incoming card names the actual author
  (including a Telegram or bot marker), the forwarding bot, and the linked
  source chat and original time. Forwarding notes and shared files are
  separate from the author's verbatim words; attribution wraps on phones.
  Ordinary bot-to-bot messages, reports and replies name the bot rather than
  the chat owner. Old messages retain their existing presentation.
- **Tool calls**: each call is one header line: status (read aloud as
  running, ok, error or needs approval), the tool name (an `mcp__server__`
  prefix is shown muted; hover for the full name), a short preview of the
  arguments and the time it took. Every call starts folded to that line and
  opens on click or tap, with the arguments as a key/value list (paths, ids and code in monospace;
  long or multiline strings clamped behind "Show more"; short scalar arrays as
  chips; nested values as indented JSON; input that is not a JSON object as
  raw text). "Raw JSON" shows the full arguments, and "Copy" copies the
  arguments or the result. The result is folded behind a one-line summary
  (size and first line) and opens on click, capped in height with its own
  scroll; a failed call's error starts open once the call is. A call you opened
  stays open as the chat streams. The earlier calls of a long run fold into a
  "+N more" row naming the tools, which unfolds them in place. Calls that need
  approval or failed, and thread cards, always show.
- **Thread cards**: work a chat sends to another chat (`ergo_thread_send`,
  `ergo_message_up`) shows as a card instead of a tool call: the chat it went
  to, with the bot's icon, a live status (queued, working, waiting on you,
  done, failed), the reply, and the pull requests that came out of it. The
  card opens that chat. Workers (`ergo_agent_start`, `orca_start_worker`, `ergo_worker_start`)
  get a card with their status, progress and PRs. An Orca worker's card also
  shows the agent's last few tool calls and output lines, how long ago it
  last did something ("stalled" past the plugin's `stall_minutes`, or
  "waiting on you" when it's parked on a prompt), and "Open full log", which
  reads its recent transcript or screen from Orca and refreshes while it
  runs. A reply from another chat
  is one row you can open, the user message whose turn sent work on says
  "Sent to" the chat, and chat ids in a bot's text are links with a status
  dot.
- **Pull requests**: a PR URL in a bot's reply or a worker's result is
  recorded as a file in that chat (see [attachments.md](attachments.md)),
  shown as a chip with its live state (open, draft, merged, closed) and CI.
  Ergonaut reads the state with `gh` every minute (`refresh-pull-requests`).
- **Files** panel: the chat's files and pull requests; files can be pinned, downloaded and archived.
- **Bot page**: description, instructions and skills (as Markdown), schedules with their next run, recent
  jobs, tables with a row browser, the bot folder's files, and for a bot
  with `bot_management`, its proposed changes with diffs and Merge, Close
  and Discard.
- **Memory**: the bot's knowledge base articles, rendered as Markdown.
- **Sessions**: every session, searchable, including threads.
- **Costs**: usage for the last 7, 30, or 90 days, optionally filtered by bot.
  Headline figures show sessions with calls, total tokens, cache hit rate,
  main-chat and subscription token shares, API spend, and compaction token
  share. A token-mix bar splits input, output, cache writes, and cache reads;
  the sortable per-thread table can be grouped by thread, bot, or model and
  links to each chat. Claude CLI subscription calls contribute tokens but
  show `sub` instead of dollars and are excluded from estimated API spend.
  **Agent sessions** follows Usage and separately shows Claude Code, Codex,
  and omp tokens that Orca workers record from the agents' session files.
  Those subscription tokens are not dollar estimates and do not affect Usage
  totals. Agent rows link to their chat and show the worker, agent, model,
  cache hit, request count, and status. Below Usage, spend remains broken down
  by day, call kind (chat replies split by bot), and model. Admins see
  everyone's calls and agent sessions; others see their own.
- **Routing & capacity**: for chats on an Auto model and Orca workers started
  with a tier. The top bar is the **source sync** health (healthy, stale,
  partial, failed or no data yet), the last successful sync time, and
  **Refresh limits**, which runs `omp usage --redact --json` now. A Celery
  beat task runs the same sync every 5 minutes
  (`ERGONAUT_USAGE_SYNC_SECONDS`, `0` turns it off). **Provider windows**
  has a card per account (Claude, Codex, Grok) with its 5-hour and 7-day
  windows: percent used, reset countdown and a meter, plus any scoped window
  the account reports (Claude's Fable weekly, Grok Build). A window the
  provider does not report, or that has not synced, is **Unavailable**, never
  0%. Values older than 15 minutes keep their number but show a **Stale**
  badge with the time they were observed; a provider whose sync failed shows
  the error. **Pay-per-token providers** lists API-key
  providers separately: chats can use them when their tier lists them,
  but coding agents use subscriptions only. **Tiers** shows every configured
  tier (including custom names, not just `low`, `medium`, and `high`) and the
  model each tier picks now; skipped candidates are struck through with the
  reason. Tier names are non-empty single path segments, selected in chats
  as `auto/<tier>`. The priorities in plain words, the compiled rules with
  each window's label, and recent switches are shown below. Admins can edit
  the priorities; the text is saved in the database and replaces the bot
  repo's `routing.md` until **Use routing.md**. The data is
  `GET /api/routing` (`PUT` and `DELETE` to save or reset the text;
  `POST /api/routing/refresh` runs the usage sync and returns the page). A turn a
  provider refused at its limit offers **Retry on** the tier's next model
  in the chat, never on its own.

Django's admin is at `/mgmt/`. The API is at `/api/` with docs at
`/api/docs`.

### Pages

Pages in the viewer (pins, chat files, `/pages/view?session=<id>&pin=<path or id>`
for "Open in a new tab") use two routes:

- `POST /api/bots/<bot>/actions/<name>` runs a [page action](bots.md#page-actions)
  as the logged-in user (or API key). The body is
  `{args, page, session_id, approval}`; the answer is `{"result": {...}}`, or
  `{"needs_approval": true, "preview": "...", "approval": "<token>"}` for an
  action that needs a confirm. Every call is stored as a `PageActionCall`,
  listed in Django admin.
- `GET /api/bots/<bot>/tables/events?tables=A,B[&since=<fingerprints>]` is a
  server-sent event stream of table changes for [live refresh](bots.md#live-refresh).
  Each event is `{"changed": [...], "fingerprints": {...}}`; the first is a
  baseline, or a catch-up when `since` is given. A fingerprint is a table's row
  count, latest `updated_at` and highest id. With `REDIS_URL` set, the stream
  wakes on notices published on the `ergonaut:tables` channel (payload
  `<bot>:<Table>`); without Redis it checks every second. Without Redis, a
  table changed only by `.update()` plus `touch()` shows up when something
  else moves its fingerprint.

Bot-folder `.jhtml` pages are served sandboxed, like chat pages (opaque
origin, no cookies, no direct API calls). Their relative asset references are
rewritten to `/api/bots/<bot>/assets/<token>/<path>`, where the token is a signed
path segment valid for an hour for that user and bot folder. It never serves
`.jhtml` files, and allows cross-origin loading so module scripts and CSS
`url()` work from the opaque origin.

## API keys

Scripts and agents use the API with a key instead of a login:
`Authorization: Bearer ergo_...`. A key acts as the user it belongs to, with
the same access, and needs no CSRF token. Make one under **API keys** in the
account menu (it is shown once), or on the server:

```bash
ergonaut manage api_key create lee@example.com --name rigel-claude   # prints the key
ergonaut manage api_key list
ergonaut manage api_key revoke <id>
```

Only a hash is stored. Keys are made and revoked from a signed-in session
(`/api/auth/keys`), not with another key. Make one per place a key is used
so each can be revoked alone. The [`ergonaut-remote` command](agent-skills.md) uses them.

## Reloading

Ergonaut fingerprints the bot folders and reloads a bot when its files
change: prompts, bot.yaml, skills and tools are live on the next turn. A
bot folder that fails to load is skipped and its last good version kept;
admins see the error in the sidebar. Long-running plugins (Telegram
polling) only pick up a new bot when `ergonaut bots` restarts.

## Restarting

A restart of the web or worker processes ends every turn in progress, in
every chat. Wait for idle first:

```bash
ergonaut manage wait_idle --quiet-for 30 && systemctl restart ergonaut   # or your run script
```

`wait_idle` exits once no turn is running and no worker is queued or
running (`--quiet-for` waits until nothing has run for that many seconds in
a row), or fails after `--timeout` (30 minutes by default).
`--ignore-workers` waits for turns only: a polling worker survives a
restart (beat resumes it), but one in the middle of a step loses that step.
Thread messages that were waiting are redelivered.

With Celery a restart is gentler on workers than that suggests: each worker
is a database row plus one short Celery step at a time, a step in flight
finishes during Celery's warm shutdown, and beat's `resume_workers`
reschedules any worker whose next step is over 3 minutes late. A worker whose
real work runs outside Ergonaut (an Orca agent in another pod) isn't touched
at all. Such a deployment can stop the automatic upgrade from waiting for
workers with `ERGONAUT_UPGRADE_WAIT_FOR_WORKERS=0`.

## Upgrading

Ergonaut can upgrade itself when a new release of django-ergo is published
on GitHub. The steps are fixed and only the last one is pluggable:

1. Find the running commit: `ERGONAUT_VERSION` (the image sets it to the
   commit it was built from), else the commit pip recorded for a `git+`
   install, else the HEAD of the checkout Ergonaut runs from.
2. Find the newest release (not a draft or prerelease) and ask GitHub
   whether it is ahead of the running commit. An instance that is up to
   date, or runs a newer commit, is left alone.
3. Wait until no turn or worker is running (the `wait_idle` gate).
4. Hand the release to the upgrader.

```bash
ergonaut upgrade --check      # running commit, latest release, whether it's newer
ergonaut upgrade              # wait for idle (--timeout), then upgrade
ergonaut upgrade --status     # the last check and attempt
```

The bottom of the web app's sidebar shows the running commit and its date,
and a notice when a newer one is out. Admins also see when the last check
ran, what it found, and a failed upgrade's error. The last check and attempt
are kept in Redis (`ergonaut:upgrade:state`), or `DATA_DIR/upgrade.json`
without it. GitHub calls use `GITHUB_TOKEN`, `GH_TOKEN` or the logged-in
`gh` CLI's token when there is one.

With `ERGONAUT_AUTO_UPGRADE_SECONDS` set (say `900`), beat runs the same
check on that interval. A busy instance waits up to a minute (`ERGONAUT_AUTO_UPGRADE_WAIT_SECONDS`) and is checked
again next time; a release whose upgrade failed is retried after six hours
(or with `ergonaut upgrade --force`).

| Variable | Default | What it does |
| --- | --- | --- |
| `ERGONAUT_UPGRADER` | unset (no upgrades) | `systemd`, `command`, or your own upgrader as `package.module:Class` or `/path/to/file.py:Class` |
| `ERGONAUT_AUTO_UPGRADE_SECONDS` | unset | check for a new release on this interval (needs beat) |
| `ERGONAUT_UPGRADE_REPO` | `boundcorp/django-ergo` | where releases come from (a fork) |
| `ERGONAUT_UPGRADE_CHANNEL` | `releases` | `releases`, or `branch:main` to follow a branch's head |
| `ERGONAUT_AUTO_UPGRADE_WAIT_SECONDS` | `60` | how long each automatic check waits for a 20-second gap with no turn running before giving up until the next check |
| `ERGONAUT_UPGRADE_WAIT_FOR_WORKERS` | `1` | `0` makes the idle gate wait for bot turns only, not queued or running workers |
| `GITHUB_TOKEN` or `GH_TOKEN` | the `gh` CLI's login | GitHub API token; without any, 60 requests an hour |

### systemd

For Ergonaut run from a git checkout under systemd, for example this unit
running `ergonaut up`:

```ini
# /etc/systemd/system/ergonaut.service
[Service]
User=ergonaut
WorkingDirectory=/srv/django-ergo/ergonaut
EnvironmentFile=/etc/ergonaut.env
ExecStart=/srv/django-ergo/ergonaut/.venv/bin/ergonaut up
Restart=always
KillMode=mixed
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
ERGONAUT_UPGRADER=systemd
ERGONAUT_AUTO_UPGRADE_SECONDS=900
```

The `systemd` upgrader checks out the release's commit (refusing if the
checkout has local changes), reinstalls it into the running virtualenv
(`uv pip` or `pip`, `-e .[legacy,bots] -e ergonaut`), rebuilds the frontend
with `npm ci && npm run build`, and runs `systemctl --no-block restart
ergonaut`. If the install fails, the checkout goes back to the old commit.
`ergonaut up` and `ergonaut web` run the migrations as they start.

| Variable | Default | What it does |
| --- | --- | --- |
| `ERGONAUT_SYSTEMD_UNITS` | `ergonaut` | units to restart (space-separated, or a target) |
| `ERGONAUT_SYSTEMD_USER` | unset | `1` for user units (`systemctl --user`) |
| `ERGONAUT_SYSTEMD_RESTART` | unset | your own restart command instead, e.g. `sudo systemctl restart ergonaut` |
| `ERGONAUT_UPGRADE_CHECKOUT` | the checkout Ergonaut runs from | the django-ergo checkout to move |
| `ERGONAUT_UPGRADE_EXTRAS` | `legacy,bots` | django-ergo extras to install |
| `ERGONAUT_UPGRADE_FRONTEND` | `1` | `0` skips the frontend build |

A system unit's own user can't restart it without permission: run it as a
user unit, or allow the restart with a sudoers line or a polkit rule and set
`ERGONAUT_SYSTEMD_RESTART`.

### Your own upgrader

`command` runs `ERGONAUT_UPGRADE_COMMAND` in a shell with
`ERGONAUT_UPGRADE_TAG`, `ERGONAUT_UPGRADE_SHA`, `ERGONAUT_UPGRADE_REPO` and
`ERGONAUT_UPGRADE_URL` set. For more, subclass `Upgrader`, for example in
your bot repo, to roll out a Kubernetes deployment:

```python
# deploy/upgrader.py; ERGONAUT_UPGRADER=/bot/deploy/upgrader.py:KubeUpgrader
import subprocess

from ergonaut.upgrades import Release, Upgrader


class KubeUpgrader(Upgrader):
    name = "kubernetes"

    def upgrade(self, release: Release) -> str:
        image = f"ghcr.io/you/ergonaut:sha-{release.sha}"
        for deployment in ["ergonaut-web", "ergonaut-worker", "ergonaut-beat", "ergonaut-bots"]:
            subprocess.run(["kubectl", "set", "image", f"deployment/{deployment}", f"*={image}"], check=True)
        return f"rolling out {release.tag}"
```

`upgrade(release)` runs after the idle gate, in a Celery worker or in
`ergonaut upgrade`, and may restart the process it runs in. Return a line
saying what it did; raise `NotReady` when the release can't be installed
yet (its image isn't published), so the next check tries again; raise
anything else to report a failure. Override `current_version()`
if the running commit is known some other way (the deployed image tag).

## Development

In `ergonaut/`:

```bash
make venv                         # Ergo editable from .. plus Ergonaut
source .venv/bin/activate
ERGONAUT_BOTS=../examples/hello ergonaut up
cd frontend && npm run dev        # Vite on :3000, proxying /api and /mgmt to :8000
make test                         # Ergonaut's tests
make format
```

Edits to Ergo under `src/` and to Ergonaut's Python are picked up when you
restart `up`; the Vite dev server reloads the frontend as you edit.
`make dev` runs the same stack in Docker Compose instead (Postgres, Redis,
Garage, worker, beat, Vite and Caddy; app at http://localhost:2228).

## Containers

The Dockerfile builds from the repo root:

```bash
docker build -f ergonaut/Dockerfile --target release -t ergonaut .
```

The image is the app alone: run it next to Postgres, Redis and S3 storage
(set `DATABASE_URL`, `CELERY_BROKER_URL` and `S3_ENDPOINT_URL`), one
container per role, as in Production below. It includes an agent toolchain
for bots that shell out: `git`, `openssh-client`, `gh`, Claude Code (`claude`), `omp` (with Bun) and the Orca
CLI (`orca`). Orca is a pinned Linux AppImage (`ORCA_VERSION`,
`ORCA_LINUX_APPIMAGE_SHA256` build args), checksum-verified, extracted to
`/opt/orca`. Pushes to `main` publish the `release` image to
`ghcr.io/boundcorp/ergonaut` as `sha-<commit>` and move the `main` tag to it,
once a smoke test (`ergonaut up` with `examples/hello`) passes. The
all-in-one `aio` image, which bundled Redis and Garage, is gone; run
`ergonaut up` on a host instead.

## Production

Run the `release` image as separate workloads with external Postgres
(with pgvector), Redis and S3:

| Workload | Command | Replicas |
| --- | --- | --- |
| web | `ergonaut web` (or `infra/prod/start-uvicorn.sh`) | any (two or more for rolling updates) |
| worker | `ergonaut worker -Q celery,bot_tasks` (or `infra/prod/start-celery-worker.sh`) | any |
| beat | `ergonaut beat` | exactly one |
| bots | `ergonaut bots` | exactly one |

Mount or check out the bot repo at `ERGONAUT_BOTS` in every workload, run
`ergonaut manage ergo_bot_migrate <bot paths>` on deploy (`web` only runs
Django's own migrations), and set `ERGONAUT_PUBLIC_URL` so channels use
webhooks. With `ERGONAUT_BOTS_PULL_SECONDS`, each workload's checkout must
be able to pull.

To keep the web app answering through an upgrade, run two or more web
replicas and roll them one at a time (on Kubernetes, `maxUnavailable: 0`,
`maxSurge: 1`) with a readiness check on `GET /api/healthz`, so the old pods
serve until a new one is ready. Workers end the turns they are running
when they stop, so gate the rollout with the upgrade's idle check (above),
or `ergonaut manage wait_idle` in a pre-stop hook with a long enough grace
period.
