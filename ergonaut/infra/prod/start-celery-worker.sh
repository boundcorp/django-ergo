#!/usr/bin/env bash
/app/.venv/bin/celery -A ergonaut worker -l info -Q celery,bot_tasks
