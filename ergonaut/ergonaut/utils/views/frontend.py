"""Serve the built web app (frontend/dist) for every path Django doesn't own."""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse


def frontend(request, path=""):
    dist = Path(settings.FRONTEND_DIST).resolve()
    target = (dist / path).resolve()
    if path and target.is_file() and target.is_relative_to(dist):
        content_type, _ = mimetypes.guess_type(target.name)
        return FileResponse(target.open("rb"), content_type=content_type)
    if path.startswith("assets/"):
        raise Http404
    index = dist / "index.html"
    if not index.is_file():
        return HttpResponse(
            "The web app isn't built. Run `npm run build` in frontend/, or use the Vite dev server.",
            status=503,
            content_type="text/plain",
        )
    # Client-side routes (/s/<id>, /sessions) all load the app.
    return HttpResponse(index.read_bytes(), content_type="text/html")
