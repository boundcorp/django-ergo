#!/usr/bin/env bash
/app/.venv/bin/celery -A ergonaut beat -l info --schedule=/tmp/ergonaut-celerybeat-schedule
