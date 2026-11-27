"""Dedicated threaded WSGI entry point; never mount in the normal web process.

Example after deployment approval (not executed by tests/workers)::
    DJANGO_SETTINGS_MODULE=ergonaut.settings gunicorn --threads 8 \
        django_ergo.conversation.paid_agent_wsgi:application

Serve behind cluster-private TLS. WSGI preserves the synchronous httpx SSE
iterator; deploying this iterator under ASGI would buffer it. The registry and
service credentials must be available to the configured resolver in this service.
"""

from django.conf import settings
from django.core.wsgi import get_wsgi_application
from django.urls import include
from django.urls import path

settings.ROOT_URLCONF = __name__
urlpatterns = [path("v1/", include("django_ergo.conversation.paid_agent_urls"))]
application = get_wsgi_application()
