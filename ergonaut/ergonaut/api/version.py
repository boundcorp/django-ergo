"""The running Ergonaut version and whether a newer one is out (sidebar footer)."""

from ninja import Router
from ninja.security import django_auth

router = Router(tags=["version"], auth=django_auth)


@router.get("/version")
def version(request):
    """Running commit and date, the newest release, and (for admins) how
    upgrades are set up and what the last check and attempt did."""
    from ergonaut import upgrades

    info = upgrades.version_info()
    if not request.auth.is_superuser:
        info = {key: info[key] for key in ("commit", "date", "latest", "available", "repo")}
    return info
