import hmac

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest


def may_scrape(request) -> bool:
    """Open in DEBUG; otherwise a bearer token (TELEMETRY_METRICS_TOKEN) or an admin's session."""
    if settings.DEBUG:
        return True
    token = settings.TELEMETRY_METRICS_TOKEN
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token and given and hmac.compare_digest(given, token):
        return True
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_superuser)


def metrics_view(request):
    if not settings.TELEMETRY_METRICS_ENABLED or not may_scrape(request):
        return HttpResponseNotFound("Telemetry disabled.")
    return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)
