"""Provider usage windows: parse CLI rate-limit reports and store them.

The Claude Code and Codex engines report their subscription windows on every
call; ``bots.routing`` and ``bots.usage_sync`` read and refresh the same rows
(``ProviderUsage``). This lives in the conversation layer so engines can
record usage without importing bots.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

WINDOW_NAMES = {
    "five_hour": "5-hour window",
    "weekly": "weekly window",
    "weekly_fable": "7-day Fable window",
}


def window_period(name: str) -> str:
    """The length a window name stands for: ``5h``, ``7d``, or "" when unknown."""
    if name == "five_hour":
        return "5h"
    if name == "weekly" or name.startswith("weekly_"):
        return "7d"
    return ""


def window_entry(name: str, used, resets, **metadata) -> dict:
    return {
        "label": metadata.get("label")
        or WINDOW_NAMES.get(name, name.replace("_", " ")).removesuffix(" window"),
        "used": used,
        "remaining": 100 - used if used is not None else None,
        "resets_at": resets,
        "status": metadata.get("status", ""),
        "model": metadata.get("model", ""),
        "period": metadata.get("period") or window_period(name),
        "observed_at": metadata.get("observed_at"),
    }


def codex_windows(rate_limits: dict | None) -> dict:
    """Only windows in Codex's snapshot; primary need not be a 5-hour limit."""
    out = {}
    for key in ("primary", "secondary"):
        window = (rate_limits or {}).get(key)
        if not window:
            continue
        minutes = window.get("windowDurationMins")
        name = {300: "five_hour", 10080: "weekly"}.get(minutes)
        name = name or (f"minutes_{minutes}" if minutes else key)
        label = window.get("label") or (
            f"{minutes} minutes" if minutes and name.startswith("minutes_") else ""
        )
        used = window.get("usedPercent")
        out[name] = window_entry(
            name,
            float(used) if used is not None else None,
            window.get("resetsAt"),
            label=label,
            status=window.get("status") or "",
        )
    return out


def claude_windows(info: dict | None) -> dict:
    """Preserve every reported Claude window, including model-specific limits."""
    info = info or {}
    names = {"seven_day": "weekly"}
    out = {}
    for key, window in (info.get("unifiedWindows") or {}).items():
        if not window:
            continue
        name = names.get(key, key.replace("seven_day_", "weekly_", 1))
        model = key.removeprefix("seven_day_") if key.startswith("seven_day_") else ""
        out[name] = window_entry(
            name,
            _percent(window.get("utilization")),
            window.get("resetsAt"),
            label=window.get("label") or "",
            status=window.get("status") or "",
            model=model,
        )
    key = info.get("rateLimitType") or ""
    if key:
        name = names.get(key, key.replace("seven_day_", "weekly_", 1))
        used = _percent(info.get("utilization"))
        if info.get("status") == "rejected":
            used = 100.0
        if used is not None:
            model = (
                key.removeprefix("seven_day_") if key.startswith("seven_day_") else ""
            )
            out[name] = window_entry(
                name,
                used,
                info.get("resetsAt"),
                status=info.get("status") or "",
                model=model,
            )
    return out


def _percent(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value * 100 if value <= 1 else value


def record_usage_windows(
    provider: str, windows: dict, *, full_snapshot: bool = False
) -> None:
    """Replace authoritative snapshots; merge partial per-call rate-limit events.

    Every window carries the time it was observed, so a partial event that
    refreshes one window never makes the others it leaves alone look current."""
    if not provider or (not windows and not full_snapshot):
        return
    from django_ergo.conversation.models import ProviderUsage

    seen = time.time()
    windows = {
        name: {**window, "observed_at": window.get("observed_at") or seen}
        for name, window in windows.items()
    }
    row, _ = ProviderUsage.objects.get_or_create(provider=provider)
    row.windows = windows if full_snapshot else {**(row.windows or {}), **windows}
    row.save(update_fields=["windows", "updated_at"])


async def arecord_usage_windows(
    provider: str, windows: dict, *, full_snapshot: bool = False
) -> None:
    if not provider or (not windows and not full_snapshot):
        return
    from asgiref.sync import sync_to_async

    try:
        await sync_to_async(record_usage_windows, thread_sensitive=True)(
            provider, windows, full_snapshot=full_snapshot
        )
    except Exception:  # noqa: BLE001 - usage tracking never fails a turn
        logger.warning("Couldn't record usage windows for %s", provider, exc_info=True)
