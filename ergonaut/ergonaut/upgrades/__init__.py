"""Upgrade Ergonaut when a new release is published on GitHub.

    ergonaut upgrade --check     # what's running, what's released, whether it's newer
    ergonaut upgrade             # wait for idle, then hand the release to the upgrader

With ``ERGONAUT_AUTO_UPGRADE_SECONDS`` set, beat runs the same check on that
interval (``ergonaut.auto_upgrade``); a busy instance is left alone and checked
again next time.

The steps are always the same: find the running commit (``current_version``),
find the newest release (``github.latest``), ask GitHub whether the release is
ahead of the running commit, wait until no turn or worker is running
(``wait_idle.wait_until_idle``), then call the upgrader. Only the last step is
pluggable. ``ERGONAUT_UPGRADER`` picks it:

- ``systemd``: update a git checkout, reinstall, ``systemctl restart`` (see backends.py)
- ``command``: run ``ERGONAUT_UPGRADE_COMMAND`` with the release in its environment
- ``package.module:Class`` or ``/path/to/file.py:Class``: your own ``Upgrader``
  subclass, e.g. one in your bot repo that rolls out a Kubernetes deployment
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_REPO = "boundcorp/django-ergo"
# A failed upgrade to one release isn't retried for this long.
RETRY_SECONDS = 6 * 60 * 60


@dataclass
class Release:
    """What to upgrade to."""

    tag: str  # the release tag, or the branch name for ``branch:`` channels
    sha: str  # the full commit SHA it points at
    repo: str = DEFAULT_REPO
    url: str = ""
    name: str = ""
    date: str = ""  # when it was published (a release) or committed (a branch head), ISO 8601

    def env(self) -> dict[str, str]:
        return {
            "ERGONAUT_UPGRADE_TAG": self.tag,
            "ERGONAUT_UPGRADE_SHA": self.sha,
            "ERGONAUT_UPGRADE_REPO": self.repo,
            "ERGONAUT_UPGRADE_URL": self.url,
        }


class NotReady(Exception):  # noqa: N818
    """Raised by an upgrader when the release can't be installed yet (its image
    isn't published); the next check tries again, with no failure recorded."""


class Upgrader:
    """Rolls this instance forward to a release. Subclass it and point
    ``ERGONAUT_UPGRADER`` at the subclass.

    ``upgrade`` runs after the idle gate, inside a Celery worker (beat) or
    ``ergonaut upgrade``. It may restart the process it runs in, so record
    anything you need first; return a line saying what it did, raise
    ``NotReady`` if the release can't be installed yet (checked again next
    time), and raise anything else to report a failure (the same release is
    retried after ``RETRY_SECONDS``).
    """

    name = "upgrader"

    def current_version(self) -> str | None:
        """The commit SHA running now; override if the default lookup can't see it."""
        return current_version()

    def upgrade(self, release: Release) -> str:
        raise NotImplementedError


def load_upgrader(spec: str | None = None) -> Upgrader | None:
    """The upgrader ``ERGONAUT_UPGRADER`` names, or None when it's unset."""
    spec = (spec if spec is not None else os.environ.get("ERGONAUT_UPGRADER", "")).strip()
    if not spec or spec == "none":
        return None
    from ergonaut.upgrades import backends

    if spec in backends.BUILTIN:
        return backends.BUILTIN[spec]()
    if ":" in spec:
        where, attr = spec.rsplit(":", 1)
    else:
        where, attr = spec.rsplit(".", 1)
    if where.endswith(".py") or os.sep in where:
        path = Path(where).expanduser()
        module_spec = importlib.util.spec_from_file_location(f"ergonaut_upgrader_{path.stem}", path)
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"can't load upgrader from {path}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(where)
    obj = getattr(module, attr)
    upgrader = obj() if isinstance(obj, type) else obj
    if not callable(getattr(upgrader, "upgrade", None)):
        raise TypeError(f"{spec} has no upgrade(release) method")
    return upgrader


def source_checkout() -> Path | None:
    """The git checkout this Ergonaut runs from, if it runs from one."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / ".git").exists() and (root / "ergonaut").is_dir() else None


def current_version() -> str | None:
    """The django-ergo commit running now: ``ERGONAUT_VERSION`` (set in the image),
    else the commit pip recorded for a ``git+`` install, else the checkout's HEAD."""
    if version := os.environ.get("ERGONAUT_VERSION", "").strip():
        return version
    with contextlib.suppress(Exception):
        from importlib.metadata import distribution

        direct = json.loads(distribution("django-ergo").read_text("direct_url.json") or "{}")
        if commit := direct.get("vcs_info", {}).get("commit_id"):
            return commit
    checkout = source_checkout()
    if checkout is not None:
        with contextlib.suppress(Exception):
            out = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            )
            return out.stdout.strip() or None
    return None


def settings() -> dict:
    return {
        "repo": os.environ.get("ERGONAUT_UPGRADE_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO,
        "channel": os.environ.get("ERGONAUT_UPGRADE_CHANNEL", "releases").strip() or "releases",
        "upgrader": os.environ.get("ERGONAUT_UPGRADER", "").strip(),
    }


# State: the last check and the last attempt, so a failing release isn't retried every
# tick and the web app can show what happened. In Redis when there is one (every
# workload sees it), else DATA_DIR/upgrade.json.

STATE_KEY = "ergonaut:upgrade:state"


def _state_path() -> Path:
    from django.conf import settings as django_settings

    return Path(django_settings.DATA_DIR) / "upgrade.json"


def _redis():
    from ergonaut.apps.bots import tasks

    return tasks.redis_client()


def load_state() -> dict:
    with contextlib.suppress(Exception):
        client = _redis()
        if client is not None:
            raw = client.get(STATE_KEY)
            return json.loads(raw) if raw else {}
        return json.loads(_state_path().read_text())
    return {}


def save_state(**fields) -> None:
    state = load_state() | fields
    with contextlib.suppress(Exception):
        client = _redis()
        if client is not None:
            client.set(STATE_KEY, json.dumps(state))
        else:
            _state_path().write_text(json.dumps(state, indent=2))


@contextlib.contextmanager
def single_flight():
    """Only one upgrade at a time across processes (Redis when there is one)."""
    from ergonaut.apps.bots import tasks

    client = tasks.redis_client()
    if client is None:
        yield True
        return
    key = "ergonaut:upgrade"
    got = bool(client.set(key, str(os.getpid()), nx=True, ex=60 * 60))
    try:
        yield got
    finally:
        if got:
            client.delete(key)


@dataclass
class Check:
    current: str | None
    release: Release | None
    status: str  # ahead (the release is newer), identical, behind, diverged, unknown
    upgrader: str

    @property
    def available(self) -> bool:
        return self.release is not None and self.status == "ahead"

    def describe(self) -> str:
        running = (self.current or "unknown")[:12]
        if self.release is None:
            return f"running {running}; no release found"
        target = f"{self.release.tag} ({self.release.sha[:12]})"
        verdict = {
            "ahead": "newer, upgrade available",
            "identical": "up to date",
            "behind": "running a newer commit than the release",
            "diverged": "running a commit that isn't in the release's history",
        }.get(self.status, "can't compare")
        return f"running {running}; latest {target}: {verdict}; upgrader: {self.upgrader or 'none'}"


def check(upgrader: Upgrader | None = None) -> Check:
    from ergonaut.upgrades import github

    conf = settings()
    current = upgrader.current_version() if upgrader else current_version()
    release = github.latest(conf["repo"], conf["channel"])
    status = "unknown"
    if release is not None and current:
        status = "identical" if release.sha == current else github.compare(conf["repo"], current, release.sha)
    return Check(current, release, status, getattr(upgrader, "name", "") if upgrader else "")


def run(**kwargs) -> str:
    """Upgrade if a newer release is out and nothing is running. Returns what
    happened, and records it (``checked_at``, ``result``) for the web app."""
    try:
        result = _run(**kwargs)
    except Exception as exc:
        save_state(checked_at=time.time(), result=f"check failed: {exc}"[:500])
        raise
    save_state(checked_at=time.time(), result=result[:500])
    return result


def _run(
    *,
    force: bool = False,
    wait_timeout: float = 30 * 60,
    quiet_for: float = 20,
    poll: float = 5,
    workers: bool = True,
    log=logger.info,
) -> str:
    from ergonaut.apps.bots.management.commands.wait_idle import wait_until_idle

    upgrader = load_upgrader()
    if upgrader is None:
        return "no upgrader (set ERGONAUT_UPGRADER)"
    result = check(upgrader)
    log(result.describe())
    if not result.available and not (force and result.release and result.status != "identical"):
        return result.describe()
    release = result.release
    assert release is not None
    state = load_state()
    if (
        not force
        and state.get("sha") == release.sha
        and state.get("status") == "failed"
        and time.time() - state.get("at", 0) < RETRY_SECONDS
    ):
        return f"{release.tag} failed recently ({state.get('error', '')}); retrying later"
    with single_flight() as got:
        if not got:
            return "another upgrade is running"
        if not wait_until_idle(timeout=wait_timeout, quiet_for=quiet_for, poll=poll, workers=workers, log=log):
            return f"{release.tag} is waiting: turns or workers are still running"
        save_state(status="upgrading", at=time.time(), error="", release=asdict(release), sha=release.sha)
        try:
            message = upgrader.upgrade(release)
        except NotReady as exc:
            save_state(status="waiting", at=time.time(), error=str(exc)[:500])
            return f"{release.tag} isn't ready: {exc}"
        except Exception as exc:
            logger.exception("upgrade to %s failed", release.tag)
            save_state(status="failed", at=time.time(), error=str(exc)[:500])
            raise
        save_state(status="started", at=time.time(), message=message or "")
        return message or f"upgrading to {release.tag}"


def version_info(*, cache_seconds: int = 300) -> dict:
    """What the web app shows: the running commit and its date, the newest
    release, whether it's newer, and the last check. GitHub answers are cached."""
    from django.core.cache import cache

    from ergonaut.upgrades import github

    conf = settings()
    info = cache.get("ergonaut:version")
    if info is None:
        current = current_version()
        info = {
            "commit": current or "",
            "date": "",
            "latest": None,
            "status": "unknown",
            "repo": conf["repo"],
            "channel": conf["channel"],
            "error": "",
        }
        try:
            if current:
                info["date"] = github.commit(conf["repo"], current)[1]
            result = check()
            info["status"] = result.status
            if result.release is not None:
                info["latest"] = asdict(result.release)
        except Exception as exc:
            info["error"] = str(exc)[:300]
        cache.set("ergonaut:version", info, cache_seconds)
    state = load_state()
    return info | {
        "available": info["status"] == "ahead",
        "upgrader": conf["upgrader"],
        "auto_seconds": float(os.environ.get("ERGONAUT_AUTO_UPGRADE_SECONDS") or 0),
        "last_check": {"at": state.get("checked_at"), "result": state.get("result", "")},
        "last_attempt": {
            "at": state.get("at"),
            "status": state.get("status", ""),
            "error": state.get("error", ""),
            "message": state.get("message", ""),
            "tag": (state.get("release") or {}).get("tag", ""),
        },
    }
