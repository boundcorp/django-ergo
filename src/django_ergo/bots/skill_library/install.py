#!/usr/bin/env python3
"""Install Ergo's agent skills for Claude Code and Codex.

The skills in this folder whose front matter says ``install: [claude, codex]``
(ergo-client, ergo-hosting, ergo-bot-development, ergo-developer) are
symlinked into ``~/.claude/skills`` (``$CLAUDE_CONFIG_DIR/skills``) and
``~/.codex/skills`` (``$CODEX_HOME/skills``), so ``git pull`` keeps them
current. Standard library only; run it from a django-ergo checkout::

    python3 src/django_ergo/bots/skill_library/install.py                  # both agents
    python3 src/django_ergo/bots/skill_library/install.py --bin ~/.local/bin   # and the ergo command
    python3 src/django_ergo/bots/skill_library/install.py --target codex --copy
    python3 src/django_ergo/bots/skill_library/install.py --uninstall
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

LIBRARY = Path(__file__).resolve().parent
MARKER = ".ergo-installed"  # in copies, so --uninstall and reinstalls know they're ours
INSTALL_LINE = re.compile(r"^install:\s*\[([^\]]*)\]\s*$", re.MULTILINE)


def targets() -> dict[str, Path]:
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return {"claude": claude / "skills", "codex": codex / "skills"}


def installable(target: str) -> list[Path]:
    """Skill folders whose front matter lists ``target`` under ``install``."""
    found = []
    for skill in sorted(LIBRARY.iterdir()):
        text = (
            (skill / "SKILL.md").read_text() if (skill / "SKILL.md").is_file() else ""
        )
        front = text.split("\n---", 1)[0] if text.startswith("---") else ""
        match = INSTALL_LINE.search(front)
        if match and target in [t.strip() for t in match.group(1).split(",")]:
            found.append(skill)
    return found


def install(skill: Path, dest: Path, copy: bool, force: bool) -> str:
    link = dest / skill.name
    if link.is_symlink() or (link / MARKER).is_file() or (link.exists() and force):
        shutil.rmtree(
            link
        ) if link.is_dir() and not link.is_symlink() else link.unlink()
    elif link.exists():
        return f"skipped {link}: it exists and isn't ours (--force replaces it)"
    if copy:
        shutil.copytree(
            skill, link, ignore=shutil.ignore_patterns("__pycache__", "tools.py")
        )
        (link / MARKER).write_text(
            f"Copied from {skill}; install.py --uninstall removes it.\n"
        )
        return f"copied {skill.name} -> {link}"
    link.symlink_to(skill, target_is_directory=True)
    return f"linked {link} -> {skill}"


def uninstall(skill: Path, dest: Path) -> str | None:
    link = dest / skill.name
    if link.is_symlink() and link.resolve() == skill:
        link.unlink()
        return f"removed {link}"
    if link.is_dir() and (link / MARKER).is_file() and not link.is_symlink():
        shutil.rmtree(link)
        return f"removed {link}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--target", choices=["claude", "codex", "all"], default="all")
    parser.add_argument(
        "--dest",
        type=Path,
        help="install into this skills folder instead (with one --target)",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy instead of symlinking (won't follow git pull)",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace a folder of the same name"
    )
    parser.add_argument(
        "--bin",
        type=Path,
        help="also link the ergo command into this folder, e.g. ~/.local/bin",
    )
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--list", action="store_true", help="show what would be installed"
    )
    args = parser.parse_args(argv)

    chosen = (
        targets() if args.target == "all" else {args.target: targets()[args.target]}
    )
    if args.dest:
        if args.target == "all":
            parser.error("--dest needs --target claude or --target codex")
        chosen = {args.target: args.dest.expanduser()}
    for target, dest in chosen.items():
        skills = installable(target)
        if args.list:
            print(f"{target} ({dest}): {', '.join(s.name for s in skills)}")
            continue
        if args.uninstall:
            for skill in skills:
                if done := uninstall(skill, dest):
                    print(done)
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for skill in skills:
            print(install(skill, dest, args.copy, args.force))
    script = LIBRARY / "ergo-client" / "scripts" / "ergo.py"
    if args.bin and not args.list:
        command = args.bin.expanduser() / "ergo"
        if args.uninstall:
            if command.is_symlink() and command.resolve() == script:
                command.unlink()
                print(f"removed {command}")
        elif command.exists() and not command.is_symlink():
            print(f"skipped {command}: a file is already there")
        else:
            command.parent.mkdir(parents=True, exist_ok=True)
            if command.is_symlink():
                command.unlink()
            command.symlink_to(script)
            print(f"linked {command} -> {script}")
    if not args.list and not args.uninstall:
        print(
            "\nNext: ergo login <server-url> (make an API key under API keys in the web app)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
