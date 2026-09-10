"""Logical hierarchy and Markdown strategy helpers, independent of storage."""

import re

from .schema import require


def allocate_code(
    existing_codes, *, hierarchy_code=None, parent_code=None, section=None
):
    require(
        sum(bool(value) for value in (hierarchy_code, parent_code, section)) == 1,
        "Provide exactly one hierarchy_code, parent_code or section",
    )
    if hierarchy_code:
        require(hierarchy_code not in existing_codes, "Hierarchy code already exists")
        return hierarchy_code
    prefix = parent_code or section
    require(
        isinstance(prefix, str) and prefix.isprintable() and bool(prefix.strip()),
        "Invalid hierarchy prefix",
    )
    if parent_code:
        require(parent_code in existing_codes, "Parent document is unavailable")
    available = next(
        (
            prefix + format(number, "X")
            for number in range(256)
            if prefix + format(number, "X") not in existing_codes
        ),
        None,
    )
    require(available is not None, "No available hierarchy codes")
    return available


def tree_block(prefix, title, description, entries=()):
    require(
        isinstance(prefix, str) and prefix.isprintable() and bool(prefix.strip()),
        "Invalid tree prefix",
    )
    require(
        all(
            isinstance(value, str) and "\n" not in value
            for value in (title, description, *entries)
        ),
        "Tree fields must be single-line text",
    )
    return f"\n### Tree #{prefix}: {title}\n- {description}\n" + "".join(
        f"  - #{prefix}XX: {entry}\n" for entry in entries
    )


def tree_status(strategy, codes):
    prefixes = set(re.findall(r"Tree #([^\s:]+)(?=[:\s]|$)", strategy))
    prefixes.update(re.findall(r"#([0-9A-Fa-f]{2})(?:XX|[0-9A-Fa-f]{2})", strategy))
    return [
        {
            "prefix": prefix,
            "article_count": sum(code.startswith(prefix) for code in codes),
        }
        for prefix in sorted(prefixes)
    ]
