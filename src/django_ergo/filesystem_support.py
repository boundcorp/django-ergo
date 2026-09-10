"""Dependency boundary for optional legacy filesystem utilities."""

try:
    import yaml
except ImportError as exc:
    raise ImportError(
        "Filesystem utilities require django-ergo[filesystem]; repository DB indexes also require [legacy]."
    ) from exc

__all__ = ["yaml"]
