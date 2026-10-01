import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ergonaut.settings")

app = Celery("ergonaut")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

import ergonaut.observability.celery  # noqa: E402,F401
