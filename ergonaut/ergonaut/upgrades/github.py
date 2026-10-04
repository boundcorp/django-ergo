"""The newest release on GitHub, and whether it's ahead of the running commit.

Uses the REST API with ``GITHUB_TOKEN`` (or ``GH_TOKEN``) when set, else the
logged-in ``gh`` CLI's token, else anonymously (60 requests an hour).
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from ergonaut.upgrades import Release

API = "https://api.github.com"


@functools.cache
def token() -> str:
    if env := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return env
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def get(path: str):
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ergonaut-upgrade"},
    )
    if auth := token():
        request.add_header("Authorization", f"Bearer {auth}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed https host)
        return json.load(response)


def commit(repo: str, ref: str) -> tuple[str, str]:
    """The full SHA and committer date (ISO 8601) of ``ref``."""
    data = get(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")
    return data["sha"], data.get("commit", {}).get("committer", {}).get("date", "")


def latest(repo: str, channel: str = "releases") -> Release | None:
    """The newest published (not draft or prerelease) release, or with
    ``channel="branch:NAME"`` the head of that branch. None if there is none."""
    if channel.startswith("branch:"):
        branch = channel.split(":", 1)[1]
        sha, date = commit(repo, branch)
        return Release(tag=branch, sha=sha, repo=repo, url=f"https://github.com/{repo}/commit/{sha}", date=date)
    try:
        data = get(f"/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    tag = data["tag_name"]
    sha, _ = commit(repo, tag)
    return Release(
        tag=tag,
        sha=sha,
        repo=repo,
        url=data.get("html_url", ""),
        name=data.get("name") or "",
        date=data.get("published_at") or "",
    )


def compare(repo: str, current: str, target: str) -> str:
    """How ``target`` relates to ``current``: ahead, behind, identical or diverged
    (GitHub's compare status), or unknown if GitHub can't find ``current``."""
    try:
        return get(f"/repos/{repo}/compare/{current}...{target}")["status"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "unknown"
        raise
