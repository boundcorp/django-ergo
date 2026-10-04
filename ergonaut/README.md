# Ergonaut

Ergonaut hosts [Ergo](../README.md) bots. Point it at bot folders and it
runs them: the web app, API and admin, Celery workers and beat, and each
bot's channels such as Telegram.

The full guide is [docs/ergonaut.md](../docs/ergonaut.md). To build bots,
start at [docs/getting-started.md](../docs/getting-started.md).

## Quick start

```bash
make venv && source .venv/bin/activate   # Ergo editable from .. plus Ergonaut
(cd frontend && npm install && npm run build)
ERGONAUT_BOTS=../examples/hello ergonaut up
ergonaut manage createsuperuser           # in a second terminal
```

Open http://localhost:8000. With no `DATABASE_URL`, `ergonaut up` runs an
embedded PostgreSQL; it also starts `redis-server` and `garage` when
they're installed.

| Command | What it does |
| --- | --- |
| `ergonaut up [ROLES]` | Backing services that have no URL set, then web, worker, beat and bots |
| `ergonaut web` | Migrate, then serve the web app, API, admin and webhooks |
| `ergonaut worker` / `ergonaut beat` | Celery worker and scheduler |
| `ergonaut bots` | Every bot's long-running plugins |
| `ergonaut check` | Load the bots; list tools, plugins, people and missing secrets |
| `ergonaut chat BOT [--user NAME]` | Chat with a bot in the terminal |
| `ergonaut manage ...` | Any `manage.py` command |
| `ergonaut manage wait_idle` | Wait until no bot turn or worker is running; run it before a restart (`ergonaut manage wait_idle && <restart>`) so running turns aren't cut off |
| `ergonaut upgrade` | Upgrade to the newest GitHub release once idle, through `ERGONAUT_UPGRADER` (`systemd`, `command` or your own); see docs/ergonaut.md |

## Layout

- `ergonaut/cli.py`, `ergonaut/up.py`: the `ergonaut` command and the
  all-in-one supervisor.
- `ergonaut/apps/bots/`: loading bot folders and people, reloading and
  repo pulls, Celery tasks for turns, schedules, workers and thread
  messages.
- `ergonaut/api/`: the django-ninja API the web app uses (`/api/docs`).
- `frontend/`: the React + Vite web app. `npm run dev` serves it on :3000
  and proxies `/api` and `/mgmt` to Django on :8000.
- `infra/`: Docker Compose for `make dev`, and start scripts for production
  workloads.
- `Dockerfile`: builds from the repo root (`docker build -f
  ergonaut/Dockerfile --target aio .`); `release` is the app alone, `aio`
  adds Redis and Garage and runs `ergonaut up`.

## Docker Compose

`make dev` runs Django, PostgreSQL 16 with pgvector, Redis 7, Garage, a
Celery worker and beat, Vite and Caddy. The app is at
http://localhost:2228 (Django on 8822, Vite on 2288).

Garage needs a one-time layout after the first `docker compose up`:

    docker exec <garage-container> /garage status        # note the node id
    docker exec <garage-container> /garage layout assign -z dc1 -c 1G NODE_ID
    docker exec <garage-container> /garage layout apply --version 1
    docker exec <garage-container> /garage key create dev-key
    docker exec <garage-container> /garage bucket create ergonaut-media
    docker exec <garage-container> /garage bucket allow --read --write --owner ergonaut-media --key dev-key

Put the key id and secret in `infra/dev/.env`.

## Tests and formatting

    make test                    # pytest (embedded PostgreSQL)
    make test_backend_coverage   # with coverage
    make format                  # ruff format + ruff check --fix

## Telemetry

Prometheus metrics are at `/metrics/` (`TELEMETRY_METRICS_ENABLED`,
`TELEMETRY_METRICS_PATH`, `TELEMETRY_NAMESPACE`): HTTP request counts and
durations, and Celery task starts, successes, failures and latency.
Grafana dashboards are in `docs/dashboards/`.
