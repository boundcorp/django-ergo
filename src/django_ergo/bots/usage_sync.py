"""Subscription limits fetched on a schedule, for the Routing page and the router.

A CLI engine only learns its subscription's windows as a side effect of a
turn, so a provider nobody chatted with (or whose events omit a window) kept
old numbers, or none. This module asks ``omp usage --redact --json`` for every
account instead (``DJANGO_ERGO["USAGE_COMMAND"]``), maps each reported limit to
a window and stores it in :class:`ProviderUsage`::

    anthropic     5h -> five_hour   7d -> weekly   7d:fable -> weekly_fable
    openai-codex  7d -> weekly
    xai-oauth     credits:1w -> weekly_credits   product:grokbuild:1w -> weekly_grokbuild

``anthropic`` feeds the CLI providers of type ``claude``, ``openai-codex`` those
of type ``openai``; an account with no such provider (Grok) is stored under
its own name and only shown. Every stored window carries ``observed_at``, so a
window that stops being refreshed is shown as stale, never as current.
:func:`sync_usage` never raises: a failed fetch leaves the old windows (now
aging) and records why in :class:`UsageSync`; one account that errors leaves
the other accounts' fresh values alone. A limit without a usable amount is a
window with ``used: None`` (unavailable), never 0.
"""

from __future__ import annotations

import json
import logging
import math
import shlex
import subprocess
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from django_ergo.bots.routing import USAGE_STALE_SECONDS
from django_ergo.bots.routing import _window
from django_ergo.bots.routing import record_usage_windows
from django_ergo.bots.routing import window_view

if TYPE_CHECKING:
    from django_ergo.bots.providers import Providers

logger = logging.getLogger(__name__)

# omp provider -> (card title, the providers.yaml type of the CLI that uses it).
ACCOUNTS = {
    "anthropic": ("Anthropic · Claude", "claude"),
    "openai-codex": ("OpenAI · Codex", "openai"),
    "xai-oauth": ("xAI · Grok", ""),
}
PERIODS = {"5h": "five_hour", "7d": "weekly", "1w": "weekly"}
PERIOD_MS = {18_000_000: "5h", 604_800_000: "7d"}
# Parts of a limit id that name no scope: "openai-codex:primary", "xai-oauth:product:grokbuild:1w".
PLAIN = {"primary", "secondary", "product"}
CLAIM_SECONDS = 150  # a sync that started longer ago died; another may start


class UsageSyncError(RuntimeError):
    """The usage command failed or printed something unusable."""


@dataclass
class Account:
    """One omp provider's report: its windows, or why it has none."""

    provider: str
    windows: dict = field(default_factory=dict)
    fetched_at: float | None = None
    error: str = ""


def fetch_report(command: str | None = None, timeout: float | None = None) -> dict:
    """Run the usage command and parse its JSON."""
    from django_ergo.settings import api_settings

    command = command or api_settings.USAGE_COMMAND
    timeout = timeout or api_settings.USAGE_TIMEOUT
    argv = shlex.split(command)
    try:
        proc = subprocess.run(  # noqa: S603 - the operator's own command
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        msg = f"{argv[0]} isn't installed"
        raise UsageSyncError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"{argv[0]} usage took over {timeout:g}s"
        raise UsageSyncError(msg) from exc
    except OSError as exc:
        msg = f"couldn't run {argv[0]}: {exc}"
        raise UsageSyncError(msg) from exc
    if proc.returncode:
        tail = (proc.stderr or proc.stdout).strip()[-300:]
        msg = f"{command} exited {proc.returncode}: {tail}"
        raise UsageSyncError(msg)
    start = proc.stdout.find("{")
    if start < 0:
        msg = f"{command} printed no JSON"
        raise UsageSyncError(msg)
    try:
        report, _ = json.JSONDecoder().raw_decode(proc.stdout[start:])
    except ValueError as exc:
        msg = f"{command} printed invalid JSON"
        raise UsageSyncError(msg) from exc
    return report


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _percent(amount: dict) -> float | None:
    """Used percent of a limit; None when it doesn't say (never 0)."""
    used = None
    if amount.get("unit") == "percent":
        used = _number(amount.get("used"))
        if used is None and (remaining := _number(amount.get("remaining"))) is not None:
            used = 100 - remaining
    if used is None and (fraction := _number(amount.get("usedFraction"))) is not None:
        used = fraction * 100
    if (
        used is None
        and (fraction := _number(amount.get("remainingFraction"))) is not None
    ):
        used = 100 - fraction * 100
    return None if used is None else min(100.0, max(0.0, round(used, 2)))


def _period(window: dict) -> str:
    by_id = window.get("id")
    if by_id in ("5h", "7d", "1w"):
        return "7d" if by_id == "1w" else by_id
    duration = _number(window.get("durationMs"))
    return PERIOD_MS.get(int(duration), "") if duration else ""


def _name(provider: str, limit: dict, window: dict, period: str) -> str:
    """The window's key: its period, plus whatever scopes the limit
    (``anthropic:7d:fable`` -> ``weekly_fable``)."""
    base = PERIODS.get(window.get("id") or "") or {
        "5h": "five_hour",
        "7d": "weekly",
    }.get(period)
    ids = str(limit.get("id") or "").lower().split(":")
    skip = {provider.lower(), str(window.get("id") or "").lower(), *PLAIN}
    scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
    parts = [p for p in ids if p and p not in skip]
    tier = str(scope.get("tier") or "").lower()
    if tier and tier not in parts:
        parts.append(tier)
    qualifier = "_".join(parts).replace("-", "_")
    base = base or str(window.get("id") or "window").lower().replace("-", "_")
    return f"{base}_{qualifier}" if qualifier else base


def parse_limits(provider: str, entry: dict, seen: float) -> Account:
    """One report entry's windows; a malformed entry is an error, not zero."""
    fetched = _number(entry.get("fetchedAt"))
    account = Account(provider, fetched_at=fetched / 1000 if fetched else seen)
    problem = entry.get("error") or entry.get("errors")
    if problem:
        account.error = str(problem)[:300]
        return account
    limits = entry.get("limits")
    if not isinstance(limits, list) or not limits:
        account.error = "no limits reported"
        return account
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        window = limit.get("window") if isinstance(limit.get("window"), dict) else {}
        amount = limit.get("amount") if isinstance(limit.get("amount"), dict) else {}
        period = _period(window)
        name = _name(provider, limit, window, period)
        count = 1
        while name in account.windows:
            count += 1
            name = f"{name.rstrip('_0123456789')}_{count}"
        resets = _number(window.get("resetsAt"))
        scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
        account.windows[name] = _window(
            name,
            _percent(amount),
            resets / 1000 if resets else None,
            label=str(limit.get("label") or ""),
            status=str(limit.get("status") or ""),
            model=str(scope.get("tier") or ""),
            period=period,
            observed_at=account.fetched_at,
        )
    if not account.windows:
        account.error = "no limits reported"
    return account


def parse_report(report, seen: float | None = None) -> dict[str, Account]:
    """Every provider in an ``omp usage`` report, keyed by omp provider."""
    seen = time.time() if seen is None else seen
    entries = report.get("reports") if isinstance(report, dict) else None
    if not isinstance(entries, list):
        msg = "usage output has no reports list"
        raise UsageSyncError(msg)
    found: dict[str, Account] = {}
    for entry in entries:
        name = entry.get("provider") if isinstance(entry, dict) else None
        if isinstance(name, str) and name:
            found[name] = parse_limits(name, entry, seen)
    return found


def targets_for(providers: Providers, account: str) -> list[str]:
    """The CLI providers (providers.yaml) whose subscription is this omp account."""
    kind = ACCOUNTS.get(account, ("", ""))[1]
    if not kind:
        return []
    return [
        name
        for name, p in providers.providers.items()
        if p.transport == "cli" and p.type == kind
    ]


def _claim(now: float):
    from django.db.models import Q

    from django_ergo.conversation.models import UsageSync

    UsageSync.objects.get_or_create(pk=1)
    claimed = (
        UsageSync.objects.filter(pk=1)
        .filter(
            Q(running_since__isnull=True) | Q(running_since__lt=now - CLAIM_SECONDS)
        )
        .update(running_since=now)
    )
    return UsageSync.objects.get(pk=1), bool(claimed)


def sync_usage(providers: Providers, *, fetch=None, now: float | None = None):
    """Fetch every account's windows now and store them; returns the
    :class:`UsageSync` row. Does nothing while another sync is running."""
    now = time.time() if now is None else now
    state, claimed = _claim(now)
    if not claimed:
        return state
    fetch = fetch or fetch_report
    try:
        state.attempted_at, state.error = now, ""
        accounts = state.accounts or {}
        try:
            found = parse_report(fetch(), now)
        except UsageSyncError as exc:
            state.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a bad fetch never takes the page down
            logger.warning("Usage fetch failed", exc_info=True)
            state.error = f"usage fetch failed: {exc}"[:300]
        else:
            accounts = _store(providers, found, accounts)
            state.accounts = accounts
            if any(not a["error"] for a in accounts.values()):
                state.succeeded_at = now
            if not found:
                state.error = "usage output listed no accounts"
            elif all(a["error"] for a in accounts.values()):
                state.error = "no account reported usage"
    finally:
        state.running_since = None
        state.save()
    return state


def _store(providers: Providers, found: dict[str, Account], before: dict) -> dict:
    """Write each healthy account's windows; keep an errored account's last
    ones (they age) and say why it's behind."""
    expected = {a for a in ACCOUNTS if targets_for(providers, a)} | set(before)
    accounts = {}
    for name in [*found, *sorted(expected - set(found))]:
        old = before.get(name) or {}
        account = found.get(name) or Account(name, error="missing from usage output")
        targets = targets_for(providers, name) or [name]
        if account.error:
            accounts[name] = {
                "error": account.error,
                "fetched_at": old.get("fetched_at"),
                "targets": old.get("targets") or targets,
            }
            continue
        for target in targets:
            record_usage_windows(target, account.windows, full_snapshot=True)
        accounts[name] = {
            "error": "",
            "fetched_at": account.fetched_at,
            "targets": targets,
        }
    return accounts


# -- the Routing page ---------------------------------------------------------

PERIOD_ORDER = {"5h": 0, "7d": 1}


def _iso(epoch) -> str | None:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat() if epoch else None


def capacity_report(providers: Providers, clock: float | None = None) -> dict:
    """What the Routing page's capacity section shows: the sync's health and,
    per account, every stored window with its own freshness. ``sync.state`` is
    one of empty, healthy, stale, partial, failed."""
    from django_ergo.conversation.models import ProviderUsage
    from django_ergo.conversation.models import UsageSync

    clock = time.time() if clock is None else clock
    state = UsageSync.objects.filter(pk=1).first()
    before = (state.accounts if state else None) or {}
    rows = {row.provider: row for row in ProviderUsage.objects.all()}
    names = [a for a in ACCOUNTS if a in before or targets_for(providers, a)]
    names += sorted(set(before) - set(ACCOUNTS))
    accounts = []
    for name in names:
        info = before.get(name) or {}
        configured = targets_for(providers, name)
        target = (info.get("targets") or configured or [name])[0]
        row = rows.get(target)
        windows = [
            {"key": key, **window_view(key, seen, clock)}
            for key, seen in ((row.windows or {}) if row else {}).items()
        ]
        windows.sort(
            key=lambda w: (
                PERIOD_ORDER.get(w["period"], 2),
                w["key"] not in ("five_hour", "weekly"),
                w["key"],
            )
        )
        error = info.get("error", "")
        live = [w for w in windows if w["status"] != "reset"]
        if error:
            status = "error"
        elif not windows:
            status = "unavailable"
        elif any(w["stale"] for w in live):
            status = "stale"
        else:
            status = "ok"
        accounts.append(
            {
                "id": name,
                "name": ACCOUNTS.get(name, (name.replace("-", " ").title(), ""))[0],
                "providers": configured,
                "status": status,
                "error": error,
                "fetched_at": _iso(info.get("fetched_at")),
                "windows": windows,
            }
        )
    errored = [a for a in accounts if a["status"] == "error"]
    succeeded = state.succeeded_at if state else None
    if (state and state.error) or (accounts and len(errored) == len(accounts)):
        verdict = "failed"
    elif not any(a["windows"] for a in accounts):
        verdict = "empty"
    elif errored or any(a["status"] == "unavailable" for a in accounts):
        verdict = "partial"
    elif (
        not succeeded
        or clock - succeeded > USAGE_STALE_SECONDS
        or any(a["status"] == "stale" for a in accounts)
    ):
        verdict = "stale"
    else:
        verdict = "healthy"
    running = bool(
        state and state.running_since and clock - state.running_since < CLAIM_SECONDS
    )
    return {
        "sync": {
            "state": verdict,
            "running": running,
            "attempted_at": _iso(state.attempted_at if state else None),
            "succeeded_at": _iso(succeeded),
            "error": state.error if state else "",
            "stale_after": USAGE_STALE_SECONDS,
        },
        "accounts": accounts,
    }
