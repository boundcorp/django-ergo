"""Logical, case-sensitive paths; no filesystem access or filename inference."""

import re
import unicodedata

from .schema import require

MAX_PATH_LENGTH = 1024


def validate_path(path, *, allow_empty=False):
    require(isinstance(path, str), "Path must be text")
    if allow_empty and path == "":
        return path
    require(
        bool(path) and len(path) <= MAX_PATH_LENGTH,
        "Path must contain 1 to 1024 characters",
    )
    require(unicodedata.normalize("NFC", path) == path, "Path must use NFC Unicode")
    require(
        path.isprintable() and not any(char in path for char in "\\:%?#`"),
        "Unsafe logical path",
    )
    require(
        all(
            part and part == part.strip() and part not in {".", ".."}
            for part in path.split("/")
        ),
        "Path must be relative with no empty, dot or traversal segments",
    )
    return path


def within(path, prefix):
    return bool(path) and (
        not prefix or path == prefix or path.startswith(prefix + "/")
    )


def placement(*, path=None, parent_path=None, name=None):
    if path is not None:
        require(
            parent_path is None and name is None,
            "Provide path or parent_path plus name",
        )
        return validate_path(path)
    validate_path(parent_path, allow_empty=True)
    validate_path(name)
    require("/" not in name, "Placement name must be a single segment")
    return validate_path(f"{parent_path}/{name}" if parent_path else name)


def tree_block(prefix, title, description, entries=()):
    validate_path(prefix)
    require(
        all(
            isinstance(value, str) and "\n" not in value and "\r" not in value
            for value in (title, description, *entries)
        ),
        "Tree fields must be single-line text",
    )
    return f"\n### Path tree `{prefix}`: {title}\n- {description}\n" + "".join(
        f"  - {entry}\n" for entry in entries
    )


def tree_status(strategy, paths):
    matches = re.findall(r"^### Path tree `([^`]+)`:", strategy, flags=re.MULTILINE)
    require(len(matches) == len(set(matches)), "Duplicate strategy tree path")
    prefixes = set(matches)
    return [
        {
            "path": validate_path(prefix),
            "article_count": sum(within(path, prefix) for path in paths),
        }
        for prefix in sorted(prefixes)
    ]


def move_tree_references(strategy, prefix, destination):
    def replace_heading(match):
        path = match.group(1)
        return f"### Path tree `{destination + path[len(prefix) :] if within(path, prefix) else path}`:"

    return re.sub(
        r"^### Path tree `([^`]+)`:", replace_heading, strategy, flags=re.MULTILINE
    )
