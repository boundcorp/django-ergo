"""Dependency boundary for optional legacy filesystem utilities."""

try:
    import yaml
except ImportError as exc:
    raise ImportError(  # noqa: TRY003
        "Filesystem utilities require django-ergo[filesystem]; repository DB indexes also require [legacy]."  # noqa: EM101
    ) from exc

__all__ = ["yaml"]
