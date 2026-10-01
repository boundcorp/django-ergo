from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from ergonaut.utils.admin import admin_site
from ergonaut.api import api
from ergonaut.observability.views import metrics_view

urlpatterns = [
    path("api/", api.urls),
    path("mgmt/", admin_site.urls),
    path("hooks/", include("django_ergo.bots.urls")),
    path(f"{settings.TELEMETRY_METRICS_PATH}/", metrics_view),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
