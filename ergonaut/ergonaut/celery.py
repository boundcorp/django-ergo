import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")

app = Celery("ergonaut")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

import ergonaut.observability.celery  # noqa: E402,F401

# Workers and beat report the revision they run, for deploy watches.
from ergonaut.runtime_report import connect_celery  # noqa: E402

connect_celery()
