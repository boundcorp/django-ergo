---
name: ergo-hosting
description: Set up, run and look after an Ergonaut server. Covers picking a VPS devbox, installing Ergonaut, model providers and subscriptions, users and API keys, testing it works, upgrades, restarts, backups and troubleshooting. Use it before installing, upgrading, restarting or diagnosing an Ergonaut deployment.
install: [claude, codex]
---
# Hosting Ergonaut

Ergonaut is the Django app in `django-ergo/ergonaut/` that runs bot folders.
It has a web app and API, Celery workers (turns and background work), Celery
beat (schedules, worker polling, upgrades) and a bots process (channels such
as Telegram). The reference guide is `docs/ergonaut.md` in the django-ergo
repo. Read it for anything this skill leaves out.

Act carefully on a live server. Restarting it, upgrading it, deleting data
or changing secrets affects everyone using the bots. Do those only when the
person asked for that step. Wait for idle before a restart (below).

## The recommended shape: one devbox

The simplest setup is Ergonaut as a system service on one Linux VPS that
doubles as a dev box. Give it a home directory for checkouts and the agent
CLIs: `git`, `gh`, `claude` (Claude Code), `codex`, and `orca` for coding
workers. Bots that shell out or start coding agents then work as they
would on your own machine. A Docker image (`ergonaut/Dockerfile`, the
`release` target) and Kubernetes are supported too. See
"Containers" and "Production" in `docs/ergonaut.md`.

Choosing a VPS:

- **Providers:** Hetzner (Cloud or a dedicated box) gives the most for the
  money. DigitalOcean, Vultr, Linode (Akamai) and OVHcloud are fine too.
  Pick a region near the people using it.
- **Size:** 4 vCPU, 8 GB RAM and 80 GB+ SSD for one person or a small team.
  Each Claude Code CLI turn uses about 600 MB while it runs, so allow
  roughly 1 GB per concurrent turn. Add more if bots start coding agents on
  the box.
- **OS:** Ubuntu 24.04 LTS or Debian 12. Create a non-root user to run
  Ergonaut. Use SSH keys only and a firewall that opens 22, 80 and 443. Put
  the web app behind HTTPS (Caddy or nginx with Let's Encrypt, or a
  Cloudflare tunnel). Tailscale is a good way to reach it privately.

## Install

```bash
sudo apt install -y git redis-server build-essential curl
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv; Node 20+ is also needed for the web app
git clone https://github.com/boundcorp/django-ergo ~/django-ergo
cd ~/django-ergo/ergonaut && make venv && source .venv/bin/activate
(cd frontend && npm ci && npm run build)
export ERGONAUT_BOTS=~/bots            # a bot folder or repo; ../examples/hello to try it
ergonaut check                          # loads every bot; lists missing secrets
ergonaut up                             # embedded Postgres (pgvector) under ~/.ergonaut unless DATABASE_URL is set
ergonaut manage createsuperuser         # in a second shell; it finds the running `up`
```

For a service, use the systemd unit in `docs/ergonaut.md` ("systemd"). It
runs `ergonaut up` with an `EnvironmentFile` such as `/etc/ergonaut.env`.
Set at least `ERGONAUT_BOTS`, `SECRET_KEY`, `ERGONAUT_PUBLIC_URL` (enables
webhooks), `ERGONAUT_ALLOWED_HOSTS`, `ERGONAUT_BEHIND_TLS_PROXY=true` behind
a TLS proxy, and the bots' secrets. Keep secrets in that env file, never in
the bot repo.

Install Redis. Without a broker, turns run in the web process and schedules
never fire. With `ERGONAUT_BOTS_PULL_SECONDS` set, merged bot-repo PRs go
live on their own.

## Models: providers and subscriptions

`providers.yaml` at the top of the bot path lists the models chats may use.
Its format is under "Models and providers" in `docs/building-bots.md`.

- **OpenAI:** `OPENAI_API_KEY` (or the name in `api_key_env`). This is the
  default engine.
- **Claude on a subscription:** use a provider with `transport: cli`.
  Install Claude Code where turns run (`npm install -g @anthropic-ai/claude-code`),
  then run `claude auth login` as the service user, or set
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. Anthropic allows this
  only for your own login, so keep subscription models to bots only you
  use.
- **Claude by API key:** a `type: claude` provider with `ANTHROPIC_API_KEY`.

The **Costs** page shows token use per bot and model. Subscription calls
show tokens, not dollars.

## Users and keys

- `ergonaut manage createsuperuser`, or `people:` in `ergonaut.yaml`.
  Admins see everything and are the only ones who can approve `bash`,
  `orca` and `bot_management` tool calls.
- API keys for agents and scripts: **API keys** in the web app, or
  `ergonaut manage api_key create <user> --name <where>`. Also
  `ergonaut manage api_key list` and `ergonaut manage api_key revoke <id>`.
  Make one key per place it is used, so each can be revoked on its own.
  The `ergo-client` skill uses them.

## Testing it works

1. `curl -s https://<host>/api/healthz` returns `{"status": true}`.
2. `ergonaut check` lists every bot and reports no missing secrets.
3. Sign in to the web app and send a message in a main chat. Check that a
   reply arrives and the sidebar spinner clears.
4. From another machine, `ergonaut-remote login <url>`, then `ergonaut-remote whoami`, then
   `ergonaut-remote new <bot> "Say hello" --wait`.
5. If a bot has schedules, check the bot page's next-run times, and its
   jobs list after one fires (this needs beat and Redis).

## Upgrades, restarts and backups

- **Restart only when idle:**
  `ergonaut manage wait_idle --quiet-for 30 && sudo systemctl restart ergonaut`.
  A restart ends every turn in progress.
- **Upgrades:** run `ergonaut upgrade --check`, then `ergonaut upgrade`. To
  upgrade automatically, set `ERGONAUT_UPGRADER=systemd` and
  `ERGONAUT_AUTO_UPGRADE_SECONDS=900`. `ERGONAUT_UPGRADE_CHANNEL=branch:main`
  follows main instead of releases. The sidebar footer shows the running
  commit and any newer one.
- **Backups:** back up the database (`DATABASE_URL`, or the embedded one
  under `DATA_DIR`, default `~/.ergonaut`), media/S3, and the env file. The
  bot folders live in git. Take a backup before an upgrade that has
  migrations.

## When something is wrong

- **A turn failed:** `ergonaut-remote show <session>` gives the error summary and
  hint; `ergonaut-remote resume <session>` retries it. A call's full prompt and
  transcript: `ergonaut-remote api GET /calls/<id>`.
- **A bot is missing from the sidebar:** it failed to load. Admins see the
  error in the sidebar, and `ergonaut check` prints it. The last good
  version stays loaded.
- **Turns stuck "working":** check that Celery workers are running and
  Redis is reachable. A Celery worker killed for running out of memory ends
  its turn, so look at memory use (Claude CLI turns are heavy).
- **Logs:** `journalctl -u ergonaut -f`, or the container logs.
  `/metrics/` has Prometheus metrics (admins, or `TELEMETRY_METRICS_TOKEN`).
