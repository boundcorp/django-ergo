"""The newest release on GitHub, and whether it's ahead of the running commit.

Uses the REST API with ``GITHUB_TOKEN`` (or ``GH_TOKEN``) when set, else
anonymously (60 requests an hour, three per check).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ergonaut.upgrades import Release

API = "https://api.github.com"


def get(path: str):
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ergonaut-upgrade"},
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed https host)
        return json.load(response)


def commit_sha(repo: str, ref: str) -> str:
    return get(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")["sha"]


def latest(repo: str, channel: str = "releases") -> Release | None:
    """The newest published (not draft or prerelease) release, or with
    ``channel="branch:NAME"`` the head of that branch. None if there is none."""
    if channel.startswith("branch:"):
        branch = channel.split(":", 1)[1]
        sha = commit_sha(repo, branch)
        return Release(tag=branch, sha=sha, repo=repo, url=f"https://github.com/{repo}/commit/{sha}")
    try:
        data = get(f"/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    tag = data["tag_name"]
    return Release(
        tag=tag, sha=commit_sha(repo, tag), repo=repo, url=data.get("html_url", ""), name=data.get("name") or ""
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
