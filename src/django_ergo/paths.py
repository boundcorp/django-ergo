"""Canonical Article path helpers."""

from __future__ import annotations

import posixpath


def validate_relative_path(relative_path: str) -> str:
    """Validate and normalize a repository-relative POSIX path."""
    value = relative_path.strip().replace("\\", "/")
    if "\x00" in value or any(part == ".." for part in value.split("/")):
        raise ValueError(f"Invalid relative path: {relative_path!r}")
    normalized = posixpath.normpath(value)
    if not value or normalized in {".", ".."} or normalized.startswith("../"):
        raise ValueError(f"Invalid relative path: {relative_path!r}")
    if normalized.startswith("/"):
        raise ValueError(f"Relative path must not be absolute: {relative_path!r}")
    return normalized
