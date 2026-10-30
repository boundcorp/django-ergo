"""Links a chat produced: pull requests a bot or worker reported.

A pull request is a chat file with no stored bytes: ``url`` is the PR's URL
and ``metadata["link"]`` is ``"github_pr"``, with the PR's ``repo``,
``number``, ``title``, ``state`` (``open``, ``draft``, ``merged`` or
``closed``) and ``checks`` (``passing``, ``failing``, ``pending`` or ``""``).
``record_pull_requests`` adds one for each new PR URL in a reply or worker
result; ``refresh_pull_request`` reads its live state with the GitHub CLI
(``gh``), which apps run on a schedule. When the checks of a PR a bot opened
(branch ``bot/<bot>/...``) finish, ``announce_checks`` tells the chat that
recorded it, so the bot can fix a failure or say the PR is ready.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

from django.utils import timezone

from django_ergo.protected_env import scrubbed_env

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


def record_outputs(session: ConversationSession, text: str) -> None:
    """``record_pull_requests`` for a finished turn or worker: never raises,
    since the outputs are extra to work that already finished."""
    try:
        record_pull_requests(session, text)
    except Exception:  # noqa: BLE001
        logger.warning("Couldn't record pull requests", exc_info=True)


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


def checks_state(rollup: list) -> str:
    """``passing``, ``failing``, ``pending`` or ``""`` for a ``statusCheckRollup``."""
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
                "title,state,isDraft,headRefName,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            check=False,
            env=scrubbed_env(),
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
    before = (row.metadata or {}).get("checks", "")
    row.metadata = {
        **(row.metadata or {}),
        "title": data.get("title") or "",
        "state": state,
        "branch": data.get("headRefName") or "",
        "checks": checks_state(data.get("statusCheckRollup") or []),
        "checked_at": timezone.now().isoformat(timespec="seconds"),
    }
    row.save(update_fields=["metadata", "updated_at"])
    if row.metadata["checks"] != before:
        try:
            announce_checks(row)
        except Exception:  # noqa: BLE001 - the refresh itself worked
            logger.warning("Couldn't announce the checks of %s", row.url, exc_info=True)
    return True


def announce_checks(row: ConversationAttachment) -> bool:
    """Tell the bot chat that recorded one of its own open PRs that its checks finished.

    Only for a PR on the bot's own branch (``bot/<bot>/...``, which
    bot_management's publish makes) and a bot whose bot_management plugin has
    ``watch_checks`` on. The message starts a turn, so a failure gets fixed
    (``ergo_config_repo_checkout_pr``) and a pass gets reported.
    """
    from django_ergo.bots import messaging
    from django_ergo.bots.background import find_bot

    meta = row.metadata or {}
    checks, bot_name = meta.get("checks"), row.session.bot_name
    if (
        checks not in ("passing", "failing")
        or meta.get("state") not in ("open", "draft")
        or not bot_name
        or not str(meta.get("branch") or "").startswith(f"bot/{bot_name}/")
    ):
        return False
    try:
        bot = find_bot(bot_name)
    except LookupError:
        return False
    plugin = bot.plugin("bot_management")
    if plugin is None or not getattr(plugin, "watch_checks", False):
        return False
    number, name = meta.get("number"), f"{meta.get('repo', '')}#{meta.get('number')}"
    if checks == "failing":
        text = (
            f"Checks failed on {name} ({row.url}), a pull request you opened. Read the "
            f"failures with ergo_config_repo_pr_status({number}). To fix them, "
            f"ergo_config_repo_checkout_pr({number}), change the files and "
            "ergo_config_repo_publish: that pushes to the same PR, so don't open "
            "another. Tell the user what failed and what you did."
        )
    else:
        text = (
            f"Checks passed on {name} ({row.url}), a pull request you opened. Tell the "
            "user, in a line, that it's ready to review and merge."
        )
    messaging.send(
        None,
        row.session,
        text,
        registry=bot.registry,
        metadata={"pull_request": row.url, "checks": checks},
    )
    return True


def open_pull_requests(bot) -> list[dict]:
    """``DJANGO_ERGO["OPEN_PRS"]`` from recorded pull requests: the open ones reported in
    the bot's chats, or in any chat for a repo in the bot's ``pull_requests``, newest
    first, as {repo, number, title, draft}."""
    from django.db.models import Q

    from django_ergo.conversation.models import ConversationAttachment

    rows = (
        ConversationAttachment.objects.filter(
            metadata__link=GITHUB_PR, metadata__state__in=["open", "draft", ""]
        )
        .filter(
            Q(session__bot_name=bot.name)
            | Q(metadata__repo__in=list(bot.definition.pull_requests))
        )
        .order_by("-created_at")
    )
    seen, out = set(), []
    for row in rows[:100]:
        if row.url in seen:
            continue
        seen.add(row.url)
        meta = row.metadata or {}
        out.append(
            {
                "repo": meta.get("repo", ""),
                "number": meta.get("number"),
                "title": meta.get("title", ""),
                "draft": meta.get("state") == "draft",
            }
        )
    return out


def pull_requests_to_refresh():
    """Recorded pull requests that may still change (not merged or closed)."""
    from django_ergo.conversation.models import ConversationAttachment

    return ConversationAttachment.objects.filter(metadata__link=GITHUB_PR).exclude(
        metadata__state__in=FINAL_STATES
    )
