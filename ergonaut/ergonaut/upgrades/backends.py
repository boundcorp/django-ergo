"""The built-in upgraders: ``systemd`` and ``command``.

``systemd`` is for Ergonaut running from a git checkout under systemd (a
VPS): it moves the checkout to the release's commit, reinstalls it into the
running virtualenv, rebuilds the frontend, and restarts the units. The
restart is ``--no-block``, so it lands after this returns; ``ergonaut web``
and ``ergonaut up`` run the migrations as they start.

    ERGONAUT_UPGRADER=systemd
    ERGONAUT_SYSTEMD_UNITS=ergonaut            # space-separated units (or a target)
    ERGONAUT_SYSTEMD_USER=1                    # user units: systemctl --user
    ERGONAUT_SYSTEMD_RESTART="sudo systemctl restart ergonaut"   # or any restart command
    ERGONAUT_UPGRADE_CHECKOUT=/srv/django-ergo # default: the checkout Ergonaut runs from
    ERGONAUT_UPGRADE_EXTRAS=legacy,bots        # django-ergo extras to install

``command`` runs ``ERGONAUT_UPGRADE_COMMAND`` in a shell with
``ERGONAUT_UPGRADE_TAG``, ``_SHA``, ``_REPO`` and ``_URL`` set, for anything
else (a deploy script, a Kubernetes rollout through kubectl).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ergonaut.upgrades import Release, Upgrader, source_checkout


def sh(args: list[str], *, cwd: Path | None = None, env: dict | None = None) -> str:
    out = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{shlex.join(args)} failed: {(out.stderr or out.stdout).strip()[-500:]}")
    return out.stdout.strip()


class CommandUpgrader(Upgrader):
    name = "command"

    def upgrade(self, release: Release) -> str:
        command = os.environ.get("ERGONAUT_UPGRADE_COMMAND", "").strip()
        if not command:
            raise RuntimeError("ERGONAUT_UPGRADE_COMMAND is not set")
        out = sh(["sh", "-c", command], env=os.environ | release.env())
        return out.splitlines()[-1] if out else f"ran the upgrade command for {release.tag}"


class SystemdUpgrader(Upgrader):
    name = "systemd"

    def checkout(self) -> Path:
        path = os.environ.get("ERGONAUT_UPGRADE_CHECKOUT", "").strip()
        checkout = Path(path).expanduser() if path else source_checkout()
        if checkout is None or not (checkout / ".git").exists():
            raise RuntimeError("Ergonaut isn't running from a git checkout; set ERGONAUT_UPGRADE_CHECKOUT")
        return checkout

    def install(self, checkout: Path) -> None:
        extras = os.environ.get("ERGONAUT_UPGRADE_EXTRAS", "legacy,bots").strip()
        targets = ["-e", f"{checkout}[{extras}]" if extras else str(checkout), "-e", str(checkout / "ergonaut")]
        if shutil.which("uv"):
            sh(["uv", "pip", "install", "--python", sys.executable, *targets])
        else:
            sh([sys.executable, "-m", "pip", "install", *targets])
        frontend = checkout / "ergonaut" / "frontend"
        if (frontend / "package.json").exists() and os.environ.get("ERGONAUT_UPGRADE_FRONTEND", "1") != "0":
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError(
                    "npm isn't installed, so the frontend can't be rebuilt (ERGONAUT_UPGRADE_FRONTEND=0 skips it)"
                )
            sh([npm, "ci"], cwd=frontend)
            sh([npm, "run", "build"], cwd=frontend)

    def restart_command(self) -> list[str]:
        if custom := os.environ.get("ERGONAUT_SYSTEMD_RESTART", "").strip():
            return ["sh", "-c", custom]
        units = os.environ.get("ERGONAUT_SYSTEMD_UNITS", "ergonaut").split()
        scope = ["--user"] if os.environ.get("ERGONAUT_SYSTEMD_USER", "") not in {"", "0", "false"} else []
        return ["systemctl", *scope, "--no-block", "restart", *units]

    def upgrade(self, release: Release) -> str:
        checkout = self.checkout()
        git = ["git", "-C", str(checkout)]
        if sh([*git, "status", "--porcelain", "--untracked-files=no"]):
            raise RuntimeError(f"{checkout} has local changes; commit or stash them first")
        before = sh([*git, "rev-parse", "HEAD"])
        sh([*git, "fetch", "--tags", "origin"])
        sh([*git, "checkout", "--detach", release.sha])
        try:
            self.install(checkout)
        except Exception:
            sh([*git, "checkout", "--detach", before])
            raise
        sh(self.restart_command())
        return f"installed {release.tag} ({release.sha[:12]}) from {before[:12]}; restarting"


BUILTIN = {"systemd": SystemdUpgrader, "command": CommandUpgrader}
