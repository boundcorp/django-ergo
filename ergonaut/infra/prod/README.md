# Production start scripts

Production runs the `release` image as separate workloads, each with its
own start command:

- `start-uvicorn.sh`: the web app (any number of replicas)
- `start-celery-worker.sh`: a worker for the `celery` and `bot_tasks` queues
- `start-celery-beat.sh`: beat (exactly one)

Ergonaut also needs `ergonaut bots` (exactly one) for long-running bot
plugins, and `ergonaut manage ergo_bot_migrate <bot paths>` on each deploy
for bot tables.

Keep web, worker and beat as separate, independently restartable
workloads; don't background a worker inside the web pod. Beat must run, or
schedules, archival and worker polling stop.

There is no deploy pipeline in this repo yet. See
[docs/ergonaut.md](../../../docs/ergonaut.md#production).
