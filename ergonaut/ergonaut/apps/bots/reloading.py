"""Keep the web app's bots in step with their folders.

``ReloadingRegistry`` stands in for a ``BotRegistry``. On use (at most every
``check_every`` seconds) it fingerprints the bot folders, and when a file
changed it loads the bots again, so a new bot, skill, prompt or tool file is
live without a restart. A config that fails to load is logged and the
previous bots stay up.

With ``ERGONAUT_BOTS_PULL_SECONDS`` set, each bot folder's git checkout is
fast-forwarded from its upstream on that interval when it is clean: by
Celery beat (``ergonaut.pull_bot_repos``), or by a background thread when
there is no broker. A pull request merged on GitHub then
goes live on its own.

Long-running plugins (Telegram polling) run in the ``bots`` process and still
need a restart to pick up a new bot.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ergonaut.apps.bots.loading import bot_paths, load_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from django_ergo.bots.registry import BotRegistry
    from django_ergo.bots.runtime import Bot

logger = logging.getLogger(__name__)

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
MAX_FILES = 5000


def fingerprint(paths: list[Path]) -> tuple:
    """Every file under the bot paths, with its size and modification time."""
    found = []
    for root in paths:
        if not root.exists():
            continue
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                path = Path(folder) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                found.append((str(path), stat.st_mtime_ns, stat.st_size))
                if len(found) >= MAX_FILES:
                    return tuple(found)
    return tuple(found)


def keep_last_good(old: BotRegistry, new: BotRegistry) -> None:
    """A bot whose folder broke in this reload keeps running as it was (its failure
    stays in ``new.failed``, so the app can say so)."""
    broken = {Path(folder).resolve() for folder in new.failed}
    for bot in list(old):
        root = bot.definition.root_dir
        if root is not None and root.resolve() in broken and bot.name not in new:
            new.add(bot)
            logger.warning("Keeping the last good %s bot: its folder didn't load", bot.name)


class ReloadingRegistry:
    def __init__(
        self,
        loader: Callable[[], BotRegistry] = load_registry,
        paths: Callable[[], list[Path]] = bot_paths,
        check_every: float = 2.0,
    ):
        self.loader = loader
        self.paths = paths
        self.check_every = check_every
        self._registry: BotRegistry | None = None
        self._fingerprint: tuple | None = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def current(self) -> BotRegistry:
        now = time.monotonic()
        if self._registry is not None and now - self._checked < self.check_every:
            return self._registry
        with self._lock:
            self._checked = now
            seen = fingerprint(self.paths())
            if self._registry is None or seen != self._fingerprint:
                try:
                    registry = self.loader()
                except Exception:
                    if self._registry is None:
                        raise
                    logger.exception("Reloading the bots failed; keeping the loaded ones")
                else:
                    if self._registry is not None:
                        keep_last_good(self._registry, registry)
                        logger.info("Bot files changed; reloaded %s", ", ".join(b.name for b in registry))
                    self._registry = registry
                self._fingerprint = seen
        return self._registry

    # The BotRegistry interface the app uses.
    @property
    def bots(self) -> dict[str, Bot]:
        return self.current().bots

    def get(self, name: str) -> Bot:
        return self.current().get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.current()

    def __iter__(self):
        return iter(self.current())

    def __getattr__(self, name: str):
        return getattr(self.current(), name)


def git_toplevel(path: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=False
    )
    return Path(proc.stdout.strip()) if proc.returncode == 0 else None


def pull_checkout(repo: Path) -> str:
    """Fast-forward a clean checkout from its upstream. Returns what happened."""

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False, timeout=60)

    if git("status", "--porcelain").stdout.strip():
        return "skipped: uncommitted changes"
    if git("rev-parse", "--abbrev-ref", "@{upstream}").returncode != 0:
        return "skipped: no upstream branch"
    before = git("rev-parse", "HEAD").stdout.strip()
    result = git("pull", "--ff-only", "--quiet")
    if result.returncode != 0:
        return f"failed: {(result.stderr or result.stdout).strip()}"
    return "pulled" if git("rev-parse", "HEAD").stdout.strip() != before else "up to date"


def pull_seconds() -> float:
    return float(os.environ.get("ERGONAUT_BOTS_PULL_SECONDS") or 0)


def pull_all(paths: Callable[[], list[Path]] = bot_paths) -> dict[str, str]:
    """Fast-forward every clean bot checkout once. Returns repo -> outcome."""
    outcomes = {}
    repos = {top for p in paths() if p.exists() and (top := git_toplevel(p))}
    for repo in sorted(repos):
        try:
            outcomes[str(repo)] = pull_checkout(repo)
        except Exception:
            logger.exception("Pulling %s failed", repo)
            outcomes[str(repo)] = "failed: see the log"
            continue
        if outcomes[str(repo)].startswith("failed"):
            logger.warning("Pulling %s %s", repo, outcomes[str(repo)])
        elif outcomes[str(repo)] == "pulled":
            migrate_tables([p for p in paths() if p.exists() and git_toplevel(p) == repo])
    return outcomes


def migrate_tables(paths: list[Path]) -> bool:
    """Apply the bots' table migrations, in a fresh process so the new models load cleanly."""
    import sys

    if not paths:
        return True
    result = subprocess.run(  # noqa: S603 — our own management command
        [sys.executable, "-m", "ergonaut.cli", "manage", "ergo_bot_migrate", *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error("Bot table migrations failed: %s", (result.stderr or result.stdout).strip()[-2000:])
    return result.returncode == 0


def start_pulling(paths: Callable[[], list[Path]] = bot_paths, every: float | None = None) -> threading.Thread | None:
    """Pull the bot checkouts every ``ERGONAUT_BOTS_PULL_SECONDS`` seconds in a thread.

    Only for setups without a Celery broker; with one, beat runs
    ``ergonaut.pull_bot_repos`` on the same interval instead.
    """
    every = every if every is not None else pull_seconds()
    if every <= 0 or os.environ.get("CELERY_BROKER_URL"):
        return None

    def loop():
        while True:
            pull_all(paths)
            time.sleep(every)

    thread = threading.Thread(target=loop, name="ergonaut-bots-pull", daemon=True)
    thread.start()
    return thread
