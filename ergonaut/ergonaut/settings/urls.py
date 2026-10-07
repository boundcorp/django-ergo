from django.conf import settings
from django.urls import include, path, re_path

from ergonaut.api import api
from ergonaut.api.page_events import table_events
from ergonaut.api.stream import session_events
from ergonaut.observability.views import metrics_view
from ergonaut.utils.admin import admin_site
from ergonaut.utils.views.frontend import frontend

urlpatterns = [
    path("api/sessions/<uuid:session_id>/events", session_events),
    path("api/bots/<str:bot_name>/tables/events", table_events),
    path("api/", api.urls),
    path("mgmt/", admin_site.urls),
    path("hooks/", include("django_ergo.bots.urls")),
    path(f"{settings.TELEMETRY_METRICS_PATH}/", metrics_view),
]
# Uploads are not served from MEDIA_URL: chat files go through the API, which
# checks who may see them.

# Everything else is the web app.
urlpatterns += [re_path(r"^(?P<path>(?!api/|mgmt/|hooks/|dj-static/).*)$", frontend)]
