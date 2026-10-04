"""Links a chat produced: pull requests a bot or worker reported.

A pull request is a chat file with no stored bytes: ``url`` is the PR's URL
and ``metadata["link"]`` is ``"github_pr"``, with the PR's ``repo``,
``number``, ``title``, ``state`` (``open``, ``draft``, ``merged`` or
``closed``) and ``checks`` (``passing``, ``failing``, ``pending`` or ``""``).
``record_pull_requests`` adds one for each new PR URL in a reply or worker
result; ``refresh_pull_request`` reads its live state with the GitHub CLI
(``gh``), which apps run on a schedule.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from django_ergo.conversation.models import ConversationAttachment
    from django_ergo.conversation.models import ConversationSession

logger = logging.getLogger(__name__)

GITHUB_PR = "github_pr"
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
FINAL_STATES = ("merged", "closed")
GH_TIMEOUT = 20


def pull_request_urls(text: str) -> list[tuple[str, str, int]]:
    """(url, repo, number) for each pull request linked in ``text``, in order, once each."""
    found: dict[str, tuple[str, str, int]] = {}
    for match in PR_URL.finditer(text or ""):
        repo, number = match.group(1), int(match.group(2))
        url = f"https://github.com/{repo}/pull/{number}"
        found.setdefault(url, (url, repo, number))
    return list(found.values())


def record_pull_requests(
    session: ConversationSession, text: str
) -> list[ConversationAttachment]:
    """Add the pull requests linked in ``text`` to ``session``'s files (new ones only)."""
    from django_ergo.conversation.models import ConversationAttachment

    added = []
    for url, repo, number in pull_request_urls(text):
        if session.attachments.filter(url=url).exists():
            continue
        added.append(
            ConversationAttachment.objects.create(
                session=session,
                message_sequence=None,
                source="bot",
                kind="document",
                media_type="text/uri-list",
                url=url,
                filename=f"{repo.split('/')[-1]}#{number}",
                metadata={
                    "link": GITHUB_PR,
                    "repo": repo,
                    "number": number,
                    "title": "",
                    "state": "",
                    "checks": "",
                },
            )
        )
    return added


def pull_request_out(row: ConversationAttachment) -> dict:
    """A recorded pull request for apps: url, repo, number, title, state, checks."""
    meta = row.metadata or {}
    return {
        "id": str(row.id),
        "url": row.url,
        "repo": meta.get("repo", ""),
        "number": meta.get("number"),
        "title": meta.get("title", ""),
        "state": meta.get("state", ""),
        "checks": meta.get("checks", ""),
    }


def _checks(rollup: list) -> str:
    states = {
        str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        for c in rollup or []
    }
    if not states:
        return ""
    if states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return "failing"
    if states <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "passing"
    return "pending"


def refresh_pull_request(row: ConversationAttachment) -> bool:
    """Read the PR's title, state and checks with ``gh``; False if that failed."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [  # noqa: S607 - gh from PATH, as bot_management runs it
                "gh",
                "pr",
                "view",
                row.url,
                "--json",
                "title,state,isDraft,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("Couldn't read %s: %s", row.url, e)
        return False
    if proc.returncode != 0:
        logger.warning("Couldn't read %s: %s", row.url, proc.stderr.strip()[:300])
        return False
    data = json.loads(proc.stdout or "{}")
    state = str(data.get("state") or "").lower()
    if state == "open" and data.get("isDraft"):
        state = "draft"
    row.metadata = {
        **(row.metadata or {}),
        "title": data.get("title") or "",
        "state": state,
        "checks": _checks(data.get("statusCheckRollup") or []),
        "checked_at": timezone.now().isoformat(timespec="seconds"),
    }
    row.save(update_fields=["metadata", "updated_at"])
    return True


def pull_requests_to_refresh():
    """Recorded pull requests that may still change (not merged or closed)."""
    from django_ergo.conversation.models import ConversationAttachment

    return ConversationAttachment.objects.filter(metadata__link=GITHUB_PR).exclude(
        metadata__state__in=FINAL_STATES
    )
