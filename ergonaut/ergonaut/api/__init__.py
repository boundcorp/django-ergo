from django.contrib.admin.views.decorators import staff_member_required
from ninja import NinjaAPI

# The schema browser (/api/docs) is for staff; every endpoint checks its own auth anyway.
api = NinjaAPI(title="ergonaut", version="1.0.0", docs_decorator=staff_member_required)


@api.get("/healthz")
def healthz(request):
    import logging

    from django.db import connection

    try:
        connection.ensure_connection()
    except Exception:
        # Anyone can call this, so the error (hosts, users) goes to the log only.
        logging.getLogger(__name__).exception("healthz: database unreachable")
        return {"status": False}
    return {"status": True}


# Import routers
from ergonaut.api.auth import router as auth_router
from ergonaut.api.bots import router as bots_router
from ergonaut.api.costs import router as costs_router
from ergonaut.api.version import router as version_router

api.add_router("/auth/", auth_router)
api.add_router("/", bots_router)
api.add_router("/", costs_router)
api.add_router("/", version_router)
