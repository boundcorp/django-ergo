"""Pull request outcomes into Hindsight memory. See docs/hindsight-memory.md.

A periodic job (Ergonaut's beat runs ``sync_all``) reads each configured repo's
recently updated pull requests with the gh CLI, as bot.yaml's ``pull_requests``
already does for the open-PR context, and writes one short factual document per
pull request. Nothing from the PR body, comments or diff is ever sent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from django.utils import timezone

from django_ergo import signals
from django_ergo.plugins._common import clip
from django_ergo.protected_env import scrubbed_env

logger = logging.getLogger(__name__)

# Banks the job never writes to, whatever the config says.
PROTECTED_BANKS = frozenset({"personal"})
STATES = ("open", "merged", "closed")
DEFAULT_LOOKBACK_DAYS = 14
OVERLAP = timedelta(minutes=10)  # re-read a little before the cursor
GH_TIMEOUT = 60
PAGE_LIMIT = 200
BATCH = 20
BASE_FIELDS = "number,title,state,isDraft,mergedAt,closedAt,updatedAt,mergeCommit"
CONTEXT = (
    "GitHub pull request record. The source of truth is GitHub; this is a snapshot "
    "of the pull request's state at the time shown. Keep it as the outcome of that "
    "pull request (open, merged or closed)."
)


class PrEventsConfig:
    """The ``pr_events`` block of a hindsight_memory plugin's config."""

    def __init__(self, raw: dict, default_repos: list[str]):
        self.enabled = bool(raw)
        self.repos = [str(r) for r in raw.get("repos") or default_repos]
        self.users = [str(u) for u in raw.get("users") or []]
        self.lookback_days = max(
            1, int(raw.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
        )
        self.states = [str(s) for s in raw.get("states") or STATES]
        bad = [s for s in self.states if s not in STATES]
        if bad:
            msg = f"hindsight_memory pr_events states must be among {', '.join(STATES)}"
            raise ValueError(msg)
        self.apps = {str(k): str(v) for k, v in (raw.get("apps") or {}).items()}
        self.rules = [dict(r) for r in raw.get("rules") or []]
        for rule in self.rules:
            if not (rule.get("title_prefix") or rule.get("files_under")):
                msg = (
                    "hindsight_memory pr_events rules need title_prefix or files_under"
                )
                raise ValueError(msg)
            if rule.get("action", "skip") not in {"skip", "bank"} or (
                rule.get("action") == "bank" and not rule.get("bank")
            ):
                msg = "hindsight_memory pr_events rule action is skip, or bank with a bank"
                raise ValueError(msg)

    @property
    def needs_files(self) -> bool:
        return any(r.get("files_under") for r in self.rules)


# -- rules and content ------------------------------------------------------------


def _matches(rule: dict, pr: dict, repo: str) -> bool:
    if rule.get("repo") and rule["repo"] != repo:
        return False
    prefix = str(rule.get("title_prefix") or "").lower()
    if prefix and str(pr.get("title") or "").lower().startswith(prefix):
        return True
    under = str(rule.get("files_under") or "")
    paths = [str(f.get("path") or "") for f in pr.get("files") or []]
    return bool(under and paths and all(p.startswith(under) for p in paths))


def route(config: PrEventsConfig, bank: str, pr: dict, repo: str) -> str | None:
    """The bank this PR goes to, or None to skip it. The first matching rule wins."""
    for rule in config.rules:
        if _matches(rule, pr, repo):
            return None if rule.get("action", "skip") == "skip" else str(rule["bank"])
    return bank


def pr_state(pr: dict) -> str:
    state = str(pr.get("state") or "").lower()
    return state if state in STATES else "closed"


def _when(value: str | None) -> str:
    if not value:
        return ""
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def event_time(pr: dict) -> str:
    state = pr_state(pr)
    key = {"merged": "mergedAt", "closed": "closedAt"}.get(state, "updatedAt")
    return pr.get(key) or pr.get("updatedAt") or ""


def content_for(repo: str, pr: dict) -> str:
    """One factual line: repo, number, title, state, time, merge commit, draft."""
    state = pr_state(pr)
    title = clip(pr.get("title") or "", 200)
    line = f'GitHub pull request {repo}#{pr["number"]} "{title}" is {state}'
    when = _when(event_time(pr))
    if state == "merged":
        sha = str((pr.get("mergeCommit") or {}).get("oid") or "")[:7]
        line += f" (merged {when}" + (f", merge commit {sha})" if sha else ")")
    elif state == "closed":
        line += f" (closed without merging {when})"
    else:
        line += f" (last updated {when})"
    return line + f". Draft: {'yes' if pr.get('isDraft') else 'no'}."


def build_item(
    bot_name: str, config: PrEventsConfig, repo: str, pr: dict, users
) -> dict:
    tags = ["source:github", f"repo:{repo}", f"bot:{bot_name}"]
    tags += [f"user:{u}" for u in users]
    if app := config.apps.get(repo):
        tags.append(f"app:{app}")
    return {
        "content": content_for(repo, pr),
        "timestamp": event_time(pr) or timezone.now().isoformat(),
        "context": CONTEXT,
        "tags": tags,
        "metadata": {"source": "github", "repo": repo, "number": str(pr["number"])},
        "document_id": f"pr:{repo}#{pr['number']}",
        "update_mode": "replace",
    }


def _digest(bank: str, item: dict) -> str:
    keys = (bank, item["content"], sorted(item["tags"]))
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]


# -- GitHub -----------------------------------------------------------------------


def list_pull_requests(repo: str, since: datetime, *, files: bool) -> list[dict] | None:
    """Pull requests of every state updated since ``since`` (gh CLI), or None on failure."""
    gh = shutil.which("gh")
    if gh is None:
        return None
    stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, repo from bot.yaml
            [
                gh,
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--search",
                f"updated:>={stamp}",
                "--limit",
                str(PAGE_LIMIT),
                "--json",
                BASE_FIELDS + (",files" if files else ""),
            ],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            check=False,
            env=scrubbed_env(),
        )
        if proc.returncode != 0:
            logger.warning(
                "Couldn't list pull requests of %s: %s", repo, proc.stderr.strip()[:200]
            )
            return None
        return json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.warning("Couldn't list pull requests of %s: %s", repo, e)
        return None


# -- the job ----------------------------------------------------------------------


def _emit(bot_name: str, outcome: str, reason: str = "", count: int = 1) -> None:
    if not count:
        return
    try:
        signals.memory_event.send_robust(
            sender=None,
            plugin="hindsight_memory",
            bot_name=bot_name,
            event="pr_sync",
            outcome=outcome,
            reason=reason,
            duration=0.0,
            count=count,
        )
    except Exception:  # noqa: BLE001 - observation never breaks the job
        logger.debug("memory_event receiver failed", exc_info=True)


def _user_ids(names: list[str]) -> list[str]:
    from django.contrib.auth import get_user_model

    found = get_user_model().objects.filter(is_active=True, username__in=names)
    return [str(pk) for pk in found.order_by("pk").values_list("pk", flat=True)]


def _write(bank: str, items: list[dict], post) -> int:
    """Send ``items`` in batches; the number that failed."""
    failed = 0
    for start in range(0, len(items), BATCH):
        batch = items[start : start + BATCH]
        try:
            post(bank, "memories", {"items": batch, "async": True}, 30)
        except Exception:
            logger.exception("Hindsight PR write failed for bank %s", bank)
            failed += len(batch)
    return failed


def _target(config: PrEventsConfig, bank: str, pr: dict, repo: str):
    """(bank, "") for a PR to write, or (None, why) for one to skip."""
    target = route(config, bank, pr, repo)
    if target is None:
        return None, "rule"
    if target in PROTECTED_BANKS:
        return None, "protected_bank"
    if pr_state(pr) not in config.states:
        return None, "state"
    return target, ""


def sync_repo(  # noqa: PLR0913
    bot_name: str, bank: str, config: PrEventsConfig, repo: str, users, post, now=None
) -> dict[str, int]:
    from django_ergo.conversation.models import HindsightPrSync

    now = now or timezone.now()
    state, _ = HindsightPrSync.objects.get_or_create(bot_name=bot_name, repo=repo)
    since = (
        (state.cursor - OVERLAP)
        if state.cursor
        else now - timedelta(days=config.lookback_days)
    )
    prs = list_pull_requests(repo, since, files=config.needs_files)
    counts = {"written": 0, "skipped": 0, "failed": 0}
    if prs is None:
        counts["failed"] += 1
        _emit(bot_name, "failed", "github")
        return counts
    hashes = dict(state.hashes or {})
    pending: dict[str, list[tuple[str, dict, str]]] = {}
    for pr in prs:
        target, reason = _target(config, bank, pr, repo)
        if reason:
            counts["skipped"] += 1
            _emit(bot_name, "skipped", reason)
            continue
        item = build_item(bot_name, config, repo, pr, users)
        digest = _digest(target, item)
        if hashes.get(str(pr["number"])) == digest:
            counts["skipped"] += 1
            _emit(bot_name, "skipped", "unchanged")
            continue
        pending.setdefault(target, []).append((str(pr["number"]), item, digest))
    for target, rows in pending.items():
        failed = _write(target, [r[1] for r in rows], post)
        counts["failed"] += failed
        if failed:
            continue  # retried next run; the cursor stays put
        counts["written"] += len(rows)
        hashes.update({number: digest for number, _, digest in rows})
    _emit(bot_name, "written", count=counts["written"])
    _emit(bot_name, "failed", "write", count=counts["failed"])
    if not counts["failed"]:
        state.cursor = now
    state.hashes = hashes
    state.save()
    return counts


def sync_bot(bot, plugin, post) -> dict[str, int]:
    """Sync one bot's repos into the bank of its hindsight_memory plugin."""
    config: PrEventsConfig = plugin.pr_events
    totals = {"written": 0, "skipped": 0, "failed": 0}
    if plugin.bank in PROTECTED_BANKS:
        _emit(bot.name, "skipped", "protected_bank")
        return totals
    names = config.users or list(bot.definition.allowed_users)
    users = _user_ids(names)
    if not users:
        _emit(bot.name, "skipped", "no_users")
        return totals
    for repo in config.repos:
        counts = sync_repo(bot.name, plugin.bank, config, repo, users, post)
        for key, value in counts.items():
            totals[key] += value
    return totals


def sync_all(registry, post=None) -> dict[str, dict[str, int]]:
    """Run the sync for every bot with hindsight_memory and ``pr_events`` set."""
    from django_ergo.plugins import hindsight_memory

    post = post or hindsight_memory._post  # noqa: SLF001
    out = {}
    for name in list(registry):
        bot = registry.get(name)
        for plugin in getattr(bot, "plugins", []):
            if getattr(plugin, "name", "") != "hindsight_memory":
                continue
            if not (plugin.enabled and plugin.pr_events.enabled):
                continue
            try:
                out[name] = sync_bot(bot, plugin, post)
            except Exception:
                logger.exception("Hindsight PR sync failed for bot %s", name)
                _emit(name, "failed", "error")
    return out
