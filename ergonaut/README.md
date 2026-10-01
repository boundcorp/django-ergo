# Ergonaut

Ergonaut hosts [Ergo](../README.md) bots. You point it at a bot folder
(`bot.yaml`, `agents.md`, `tools/`, `kb/`) and it runs the bot: root
sessions, plugins such as Telegram, scheduled jobs and the admin. The plan
is in the project's "Ergonaut plan" doc.

It is generated from
[cookiecutter-django-boundcorp](https://github.com/boundcorp/cookiecutter-django-boundcorp)
and lives in the django-ergo repo, so Ergo changes and the Ergonaut code
that uses them land together:

- `make venv` installs Ergo editable from `..` plus Ergonaut.
- Images build from the repo root: `docker build -f ergonaut/Dockerfile .`
- Dev compose mounts the repo at `/ergo` and installs Ergo editable on start.
- Plugin webhooks are served at `/hooks/<bot>/<plugin>/<name>/`. Set
  `ERGONAUT_PUBLIC_URL` to this server's public address to use them.
- Ergo's models are in the admin at `/mgmt/`.

## Quick Start (Zero-Dep Mode)

    make venv
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py runserver

No Docker needed. Uses embedded PostgreSQL via pgserver, filesystem storage, and Celery eager mode.

- Celery tasks run synchronously in-process when `CELERY_BROKER_URL` is not set
- Media files use local filesystem when `S3_ENDPOINT_URL` is not set
- Data stored in `~/.ergonaut/pgdata`

## Full Stack (Docker Compose)

    make dev

Starts: Django, PostgreSQL 16 with pgvector, Redis 7, Garage (S3), Celery worker, Celery beat, Vite dev server, and Caddy ingress.

| Service | Internal Port | Host Port |
|---------|--------------|-----------|
| Django | 8000 | 8822 |
| Vite | 3000 | 2288 |
| Caddy (ingress) | 80 | 2228 |
| Garage S3 API | 3900 | 3900 |

Access the app at `http://localhost:2228`.

### Garage S3 Setup

After first `docker compose up`, Garage needs a one-time layout configuration:

    # Get the node ID
    docker exec <garage-container> /garage status

    # Assign layout (replace NODE_ID)
    docker exec <garage-container> /garage layout assign -z dc1 -c 1G NODE_ID
    docker exec <garage-container> /garage layout apply --version 1

    # Create API key and bucket
    docker exec <garage-container> /garage key create dev-key
    docker exec <garage-container> /garage bucket create ergonaut-media
    docker exec <garage-container> /garage bucket allow --read --write --owner ergonaut-media --key dev-key

Update `infra/dev/.env` with the generated key ID and secret.

## Frontend

    cd frontend
    npm install
    npm run dev

Vite dev server with React 19 + TypeScript. Proxies `/api/*` and `/mgmt/*` to Django.

## API

Built with [django-ninja](https://django-ninja.dev). Interactive docs at `http://localhost:8822/api/docs`.

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /api/healthz` | No | Health check (DB connectivity) |
| `GET /api/auth/profile` | JWT | Current user profile |

JWT tokens via django-ninja-jwt — see `/api/docs` for token endpoints.

## Celery

Tasks are defined with `@shared_task` in any app's `tasks.py`. Celery autodiscovers tasks.

    # Zero-dep mode: tasks run synchronously (CELERY_TASK_ALWAYS_EAGER)
    # Compose mode: tasks sent to Redis broker, processed by celery worker
    # Periodic tasks require the separate celery beat process

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | (pgserver) | PostgreSQL connection URL |
| `CELERY_BROKER_URL` | (eager mode) | Redis URL for Celery |
| `S3_ENDPOINT_URL` | (filesystem) | S3-compatible endpoint (Garage/MinIO/AWS) |
| `AWS_ACCESS_KEY_ID` | `dev` | S3 access key |
| `AWS_SECRET_ACCESS_KEY` | `dev12345678` | S3 secret key |
| `AWS_STORAGE_BUCKET_NAME` | `ergonaut-media` | S3 bucket name |
| `SECRET_KEY` | `secret` | Django secret key |
| `DEBUG` | (false) | Django debug mode |
| `APP_ENV` | `development` | Environment: development/staging/production |
| `SENTRY_BACKEND_URL` | (disabled) | Sentry DSN for error tracking |
| `TELEMETRY_METRICS_ENABLED` | `true` | Enable Prometheus metrics middleware and `/metrics/` |
| `TELEMETRY_METRICS_PATH` | `metrics` | HTTP path segment for the Prometheus scrape endpoint |
| `TELEMETRY_NAMESPACE` | `ergonaut` | Prefix used for Prometheus metric names |

## Testing

    make test                    # run all tests
    make test_backend_coverage   # with coverage report

## Formatting

    make format    # ruff format + ruff check --fix

## Deployment

Production deploys via Helm to Kubernetes. Run three separate workloads from the same image:

- `main` serves ASGI traffic via `infra/prod/start-uvicorn.sh`
- `celery` runs worker jobs via `infra/prod/start-celery-worker.sh`
- `beat` runs `CELERY_BEAT_SCHEDULE` entries via `infra/prod/start-celery-beat.sh`

GitHub Actions handles the pipeline:

- **PR to main** triggers deploy-staging
- **Merge to main** triggers deploy-production
- **Merge to main** also triggers `sync-grafana-dashboards.yml` for app-owned dashboards in `docs/dashboards/`

Secrets are managed with SOPS + Age encryption. See `helm-values.*.secrets.yaml` and [`infra/prod/README.md`](infra/prod/README.md) for the deploy contract.

## Telemetry

The template exposes Prometheus metrics at `/metrics/` and ships managed Grafana dashboard JSON in `docs/dashboards/`.

- HTTP metrics: `ergonaut_http_requests_total` and `ergonaut_http_request_duration_seconds`
- Celery metrics: `ergonaut_celery_task_started_total`, `ergonaut_celery_task_succeeded_total`, `ergonaut_celery_task_failures_total`, and `ergonaut_celery_task_latency_seconds`
- Grafana sync: `.github/workflows/sync-grafana-dashboards.yml` uploads those dashboards from CI using `GRAFANA_SA_TOKEN`; it defaults to the Grafana folder title `owner/repo` and can be overridden with the repo variable `GRAFANA_FOLDER_TITLE`
