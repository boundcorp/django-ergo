"""The library's layers import downward only.

core (``django_ergo`` outside ``bots`` and ``plugins``) < ``bots`` < ``plugins``
< the ``ergonaut`` host. A module may import its own layer or a lower one.
``management`` commands sit on top and may import anything in the library.

The few upward imports that exist today are listed in ALLOWED with the reason
each is there. Add to it only with a reason; better, move the shared code down.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src" / "django_ergo"
RANK = {"core": 0, "bots": 1, "plugins": 2, "ergonaut": 3}

ALLOWED = {
    # agent_usage_scan is stdlib-only so Orca can ship it to a remote worktree
    # unchanged; agent history reuses its project identity rather than copy it.
    ("conversation/agent_history.py", "django_ergo.plugins.agent_usage_scan"),
    # A finished PR check wakes the bot that opened the PR (lazy imports).
    ("conversation/links.py", "django_ergo.bots"),
    ("conversation/links.py", "django_ergo.bots.background"),
}


def layer(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] == "ergonaut":
        return "ergonaut"
    if parts[0] != "django_ergo":
        return None
    if parts[1:2] == ["plugins"]:
        return "plugins"
    if parts[1:2] == ["bots"]:
        return "bots"
    return "core"


def upward_imports():
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("migrations", "management"):
            continue
        dotted = ".".join(("django_ergo", *relative.with_suffix("").parts))
        own = layer(dotted.removesuffix(".__init__"))
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            else:
                continue
            for name in names:
                target = layer(name)
                if target and RANK[target] > RANK[own]:
                    yield relative.as_posix(), name


def test_layers_import_downward_only():
    found = set(upward_imports())
    assert found - ALLOWED == set(), "new upward imports; move the shared code down"
    assert ALLOWED - found == set(), "stale ALLOWED entries; remove them"


@pytest.mark.parametrize("module", ["ergonaut"])
def test_library_never_imports_the_host(module):
    assert not [edge for edge in upward_imports() if edge[1].split(".")[0] == module]
