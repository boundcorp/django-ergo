#!/usr/bin/env bash
exec /app/.venv/bin/celery -A ergonaut worker -l info -Q runpod_maintenance --concurrency=1 --hostname=runpod-maintenance@%h
