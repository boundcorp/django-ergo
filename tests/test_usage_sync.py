"""The subscription limits fetch behind the Routing page (bots.usage_sync)."""

from __future__ import annotations

import json
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
import yaml

from django_ergo.bots import routing
from django_ergo.bots.providers import Providers
from django_ergo.bots.usage_sync import UsageSyncError
from django_ergo.bots.usage_sync import capacity_report
from django_ergo.bots.usage_sync import fetch_report
from django_ergo.bots.usage_sync import parse_report
from django_ergo.bots.usage_sync import sync_usage
from django_ergo.conversation.models import ProviderUsage
from django_ergo.conversation.models import UsageSync

from .test_bot_routing import PROVIDERS

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
FIVE_HOUR_RESET = int(datetime(2026, 10, 9, 0, 40, tzinfo=UTC).timestamp())
WEEK_RESET = int(datetime(2026, 10, 12, 15, 0, tzinfo=UTC).timestamp())
CODEX_RESET = int(datetime(2026, 10, 14, 3, 30, tzinfo=UTC).timestamp())
GROK_RESET = int(datetime(2026, 10, 11, 2, 43, tzinfo=UTC).timestamp())


def limit(id_, label, window, used, resets, *, tier="", status="ok"):  # noqa: PLR0913
    """One entry of ``omp usage --redact --json``'s ``limits`` (same shape as its output)."""
    scope = {"windowId": window, **({"tier": tier} if tier else {})}
    return {
        "id": id_,
        "label": label,
        "scope": scope,
        "window": {"id": window, "label": label, "resetsAt": resets * 1000},
        "amount": {
            "used": used,
            "limit": 100,
            "remaining": 100 - used,
            "usedFraction": used / 100,
            "remainingFraction": 1 - used / 100,
            "unit": "percent",
        },
        "status": status,
    }


def fetched(entry, when):
    """The entry as the provider reported it at ``when`` (omp's ``fetchedAt``)."""
    return {**entry, "fetchedAt": int(when.timestamp() * 1000)}


def anthropic(five=5, week=52, fable=0):
    return {
        "provider": "anthropic",
        "limits": [
            limit("anthropic:5h", "Claude 5 Hour", "5h", five, FIVE_HOUR_RESET),
            limit("anthropic:7d", "Claude 7 Day", "7d", week, WEEK_RESET),
            limit(
                "anthropic:7d:fable",
                "Claude 7 Day (Fable)",
                "7d",
                fable,
                WEEK_RESET,
                tier="fable",
            ),
        ],
    }


def codex(week=13):
    return {
        "provider": "openai-codex",
        "limits": [limit("openai-codex:primary", "7 days", "7d", week, CODEX_RESET)],
    }


def grok(used=32):
    return {
        "provider": "xai-oauth",
        "limits": [
            limit(
                "xai-oauth:credits:1w",
                "SuperGrok Weekly Credits",
                "1w",
                used,
                GROK_RESET,
            ),
            limit(
                "xai-oauth:product:grokbuild:1w",
                "Grok Build (Weekly)",
                "1w",
                used,
                GROK_RESET,
            ),
        ],
    }


def report(*entries):
    return {"generatedAt": int(NOW.timestamp() * 1000), "reports": list(entries)}


def providers() -> Providers:
    return Providers.from_dict(yaml.safe_load(PROVIDERS))


def sync(data, *, now=NOW, found=None):
    return sync_usage(found or providers(), fetch=lambda: data, now=now.timestamp())


def windows_of(provider: str) -> dict:
    return ProviderUsage.objects.get(provider=provider).windows


def account(page: dict, id_: str) -> dict:
    return next(a for a in page["accounts"] if a["id"] == id_)


def capacity(clock=None):
    found = providers()
    return capacity_report(found, NOW.timestamp() if clock is None else clock)


# -- parsing -----------------------------------------------------------------


def test_every_provider_and_window_of_the_report_is_mapped():
    found = parse_report(report(anthropic(), codex(), grok()), NOW.timestamp())
    assert set(found) == {"anthropic", "openai-codex", "xai-oauth"}
    claude = found["anthropic"].windows
    assert set(claude) == {"five_hour", "weekly", "weekly_fable"}
    assert (claude["five_hour"]["used"], claude["five_hour"]["remaining"]) == (5, 95)
    assert claude["five_hour"]["resets_at"] == FIVE_HOUR_RESET
    assert (claude["weekly"]["used"], claude["weekly"]["resets_at"]) == (52, WEEK_RESET)
    assert claude["weekly_fable"]["model"] == "fable"
    assert claude["weekly_fable"]["used"] == 0  # a reported zero is a value
    assert {w["period"] for w in claude.values()} == {"5h", "7d"}
    # Codex on this plan has only a weekly limit: no 5-hour window is invented.
    assert set(found["openai-codex"].windows) == {"weekly"}
    assert found["openai-codex"].windows["weekly"]["used"] == 13
    # Two weekly limits of one account stay apart.
    assert set(found["xai-oauth"].windows) == {"weekly_credits", "weekly_grokbuild"}
    assert (
        found["xai-oauth"].windows["weekly_grokbuild"]["label"] == "Grok Build (Weekly)"
    )
    assert all(
        w["observed_at"] == NOW.timestamp()
        for a in found.values()
        for w in a.windows.values()
    )


def test_window_periods_fall_back_to_the_duration_and_fractions():
    entry = anthropic()
    five = entry["limits"][0]
    five["id"] = "anthropic:burst"
    five["window"] = {"durationMs": 18_000_000, "resetsAt": FIVE_HOUR_RESET * 1000}
    five["amount"] = {"usedFraction": 0.25}
    found = parse_report(report(entry), NOW.timestamp())["anthropic"].windows
    assert found["five_hour_burst"]["period"] == "5h"
    assert found["five_hour_burst"]["used"] == 25


@pytest.mark.parametrize(
    "amount",
    [
        {},
        {"unit": "percent"},
        {"unit": "percent", "used": None},
        {"used": "n/a", "unit": "percent"},
        "oops",
        None,
    ],
)
def test_a_limit_without_a_usable_amount_is_unavailable_never_zero(amount):
    entry = codex()
    entry["limits"][0]["amount"] = amount
    window = parse_report(report(entry), NOW.timestamp())["openai-codex"].windows[
        "weekly"
    ]
    assert window["used"] is None and window["remaining"] is None


def test_remaining_alone_gives_used():
    entry = codex()
    entry["limits"][0]["amount"] = {"unit": "percent", "remaining": 87}
    assert parse_report(report(entry))["openai-codex"].windows["weekly"]["used"] == 13


@pytest.mark.parametrize(
    "entry",
    [
        {"provider": "openai-codex"},
        {"provider": "openai-codex", "limits": []},
        {"provider": "openai-codex", "limits": "none"},
        {"provider": "openai-codex", "limits": [None, 3]},
        {"provider": "openai-codex", "error": "login expired"},
    ],
)
def test_an_entry_without_limits_is_an_error_not_an_empty_success(entry):
    parsed = parse_report(report(entry, anthropic()))
    assert parsed["openai-codex"].error and not parsed["openai-codex"].windows
    assert parsed["anthropic"].windows  # its neighbour is fine


@pytest.mark.parametrize("data", [None, [], {}, {"reports": "x"}, "text"])
def test_a_report_without_a_reports_list_is_rejected(data):
    with pytest.raises(UsageSyncError):
        parse_report(data)


def test_unnamed_or_non_object_entries_are_skipped():
    assert set(
        parse_report(report("x", None, {"limits": []}, {"provider": 5}, codex()))
    ) == {"openai-codex"}


# -- fetching ----------------------------------------------------------------


def run(code: str) -> str:
    return f"{sys.executable} -c {json.dumps(code)}"


def test_fetch_report_reads_json_after_leading_noise():
    command = run(
        f"print('warming up'); print({json.dumps(json.dumps(report(codex())))})"
    )
    assert set(parse_report(fetch_report(command))) == {"openai-codex"}


@pytest.mark.parametrize(
    ("code", "why"),
    [
        ("pass", "no JSON"),
        ("print('{not json')", "invalid JSON"),
        ("import sys; sys.stderr.write('login expired'); sys.exit(3)", "login expired"),
    ],
)
def test_fetch_report_failures_say_why(code, why):
    with pytest.raises(UsageSyncError, match=why):
        fetch_report(run(code))


def test_fetch_report_missing_command_and_timeout():
    with pytest.raises(UsageSyncError, match="isn't installed"):
        fetch_report("definitely-not-a-command --json")
    with pytest.raises(UsageSyncError, match="took over"):
        fetch_report(run("import time; time.sleep(5)"), timeout=0.2)


# -- storing and freshness ---------------------------------------------------


@pytest.mark.django_db
def test_sync_feeds_the_router_and_shows_every_account(monkeypatch):
    # The router reads usage at the test's fixed clock, not today's.
    at_now = routing.current_usage
    monkeypatch.setattr(
        routing, "current_usage", lambda now=None: at_now(NOW.timestamp())
    )
    state = sync(report(anthropic(five=90), codex(), grok()))
    assert state.error == "" and state.succeeded_at == NOW.timestamp()
    assert set(windows_of("claude")) == {"five_hour", "weekly", "weekly_fable"}
    assert set(windows_of("chatgpt")) == {"weekly"}
    assert set(windows_of("xai-oauth")) == {"weekly_credits", "weekly_grokbuild"}
    assert not ProviderUsage.objects.filter(
        provider__in=["anthropic", "openai-codex"]
    ).exists()
    usage = routing.current_usage(NOW.timestamp())
    assert usage["claude"]["five_hour"] == 90
    assert (
        routing.pick_model(providers(), "medium") == "chatgpt/gpt-6-sol"
    )  # Claude's 5h is over 85%

    page = capacity()
    assert page["sync"]["state"] == "healthy"
    assert [a["id"] for a in page["accounts"]] == [
        "anthropic",
        "openai-codex",
        "xai-oauth",
    ]
    claude = account(page, "anthropic")
    assert claude["providers"] == ["claude"] and claude["status"] == "ok"
    assert [w["key"] for w in claude["windows"]] == [
        "five_hour",
        "weekly",
        "weekly_fable",
    ]
    assert all(
        "limit" not in window
        for account in page["accounts"]
        for window in account["windows"]
    )


@pytest.mark.django_db
def test_a_provider_answering_from_its_own_old_cache_is_stale_even_though_the_sync_just_ran():
    cached = fetched(codex(week=40), NOW - timedelta(hours=2))
    sync(report(anthropic(), cached))
    page = capacity()
    assert account(page, "openai-codex")["windows"][0]["stale"] is True
    assert account(page, "openai-codex")["status"] == "stale"
    assert account(page, "anthropic")["status"] == "ok"
    assert page["sync"]["state"] == "stale"
    assert (
        account(page, "openai-codex")["fetched_at"]
        == (NOW - timedelta(hours=2)).isoformat()
    )


@pytest.mark.django_db
def test_newer_data_replaces_older_and_drops_windows_the_provider_no_longer_reports():
    sync(
        report(anthropic(five=10, week=20), codex(week=40)),
        now=NOW - timedelta(hours=2),
    )
    sync(report(anthropic(five=99, week=48)))
    claude = windows_of("claude")
    assert (claude["five_hour"]["used"], claude["weekly"]["used"]) == (99, 48)
    assert claude["weekly"]["observed_at"] == NOW.timestamp()
    older = report(
        {
            **codex(week=87),
            "limits": [limit("openai-codex:5h", "5 hours", "5h", 1, FIVE_HOUR_RESET)],
        }
    )
    sync(older)
    assert set(windows_of("chatgpt")) == {"five_hour"}


@pytest.mark.django_db
def test_a_partial_event_does_not_refresh_the_windows_it_leaves_alone():
    sync(report(anthropic()), now=NOW - timedelta(hours=3))
    routing.record_usage_windows(
        "claude", {"five_hour": {"used": 40, "resets_at": FIVE_HOUR_RESET}}
    )  # a Claude turn reports only its 5-hour window, stamped now
    weekly = windows_of("claude")["weekly"]
    assert weekly["observed_at"] == (NOW - timedelta(hours=3)).timestamp()
    page = capacity_report(providers(), clock=NOW.timestamp())
    claude = account(page, "anthropic")
    assert claude["status"] == "stale"
    stale = {w["key"]: w["stale"] for w in claude["windows"]}
    assert stale["weekly"] is True and stale["five_hour"] is False


@pytest.mark.django_db
def test_data_ages_into_stale_and_never_looks_current_again_without_a_sync():
    sync(report(anthropic(), codex()))
    assert capacity(NOW.timestamp() + 60)["sync"]["state"] == "healthy"
    later = capacity(NOW.timestamp() + routing.USAGE_STALE_SECONDS + 1)
    assert later["sync"]["state"] == "stale"
    assert later["sync"]["succeeded_at"] == NOW.isoformat()
    assert all(w["stale"] for a in later["accounts"] for w in a["windows"])
    assert {a["status"] for a in later["accounts"]} == {"stale"}


@pytest.mark.django_db
def test_rows_from_before_timestamps_existed_are_stale_not_current():
    ProviderUsage.objects.create(
        provider="claude",
        windows={"weekly": {"used": 12, "resets_at": WEEK_RESET, "label": "7-day"}},
    )
    page = capacity()
    assert account(page, "anthropic")["windows"][0]["stale"] is True
    assert account(page, "anthropic")["status"] == "stale"
    assert page["sync"]["state"] != "healthy"


@pytest.mark.django_db
def test_a_window_past_its_reset_has_no_value():
    sync(report(anthropic()))
    page = capacity(FIVE_HOUR_RESET + 60)
    five = account(page, "anthropic")["windows"][0]
    assert (five["used"], five["remaining"], five["status"]) == (None, None, "reset")


# -- failure handling --------------------------------------------------------


@pytest.mark.django_db
def test_nothing_synced_yet_is_empty_even_with_configured_providers():
    page = capacity()
    assert page["sync"]["state"] == "empty"
    assert [a["status"] for a in page["accounts"]] == ["unavailable", "unavailable"]
    assert all(a["windows"] == [] for a in page["accounts"])


@pytest.mark.django_db
def test_one_account_failing_keeps_the_others_fresh_and_its_own_old_values_aging():
    sync(report(anthropic(week=20), codex(week=30)), now=NOW - timedelta(hours=1))
    state = sync(
        report(
            anthropic(week=48), {"provider": "openai-codex", "error": "token expired"}
        )
    )
    assert state.error == "" and state.succeeded_at == NOW.timestamp()
    assert windows_of("claude")["weekly"]["used"] == 48
    assert windows_of("chatgpt")["weekly"]["used"] == 30  # kept, not zeroed
    page = capacity()
    assert page["sync"]["state"] == "partial"
    codex_card = account(page, "openai-codex")
    assert codex_card["status"] == "error" and codex_card["error"] == "token expired"
    assert codex_card["windows"][0]["stale"] is True
    assert account(page, "anthropic")["status"] == "ok"
    # It recovers on the next good sync.
    sync(report(anthropic(), codex(week=15)), now=NOW + timedelta(minutes=5))
    assert windows_of("chatgpt")["weekly"]["used"] == 15
    assert capacity(NOW.timestamp() + 300)["sync"]["state"] == "healthy"


@pytest.mark.django_db
def test_a_provider_missing_from_the_output_is_an_error_for_that_provider():
    sync(report(anthropic(), codex(), grok()), now=NOW - timedelta(minutes=10))
    sync(report(anthropic()))
    page = capacity()
    assert page["sync"]["state"] == "partial"
    assert account(page, "openai-codex")["error"] == "missing from usage output"
    assert account(page, "xai-oauth")["error"] == "missing from usage output"
    assert windows_of("xai-oauth")["weekly_credits"]["used"] == 32


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad",
    [UsageSyncError("omp isn't installed"), RuntimeError("boom")],
)
def test_a_failed_fetch_keeps_old_values_marked_stale_and_says_why(bad):
    sync(report(anthropic(week=33)), now=NOW - timedelta(minutes=30))

    def fail():
        raise bad

    state = sync_usage(providers(), fetch=fail, now=NOW.timestamp())
    assert state.error and str(bad) in state.error
    assert state.attempted_at == NOW.timestamp()
    assert state.succeeded_at == (NOW - timedelta(minutes=30)).timestamp()
    assert state.running_since is None
    page = capacity()
    assert page["sync"]["state"] == "failed" and page["sync"]["error"] == state.error
    weekly = account(page, "anthropic")["windows"][1]
    assert weekly["used"] == 33 and weekly["stale"] is True


@pytest.mark.django_db
def test_a_first_sync_that_fails_shows_failed_with_no_values():
    state = sync_usage(
        providers(),
        fetch=lambda: (_ for _ in ()).throw(UsageSyncError("no omp")),
        now=NOW.timestamp(),
    )
    assert state.succeeded_at is None
    page = capacity()
    assert page["sync"]["state"] == "failed" and page["sync"]["succeeded_at"] is None
    assert all(a["windows"] == [] for a in page["accounts"])


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [
        {"reports": []},
        report({"provider": "anthropic"}, {"provider": "openai-codex", "limits": []}),
    ],
)
def test_output_with_nothing_usable_is_a_failure(data):
    state = sync(data)
    assert state.error and state.succeeded_at is None
    assert capacity()["sync"]["state"] == "failed"


@pytest.mark.django_db
def test_a_running_sync_is_not_started_twice_and_a_dead_one_is_replaced():
    UsageSync.objects.create(pk=1, running_since=NOW.timestamp())
    calls = []

    def fetch():
        calls.append(1)
        return report(codex())

    sync_usage(providers(), fetch=fetch, now=NOW.timestamp() + 10)
    assert calls == []
    assert capacity(NOW.timestamp() + 10)["sync"]["running"] is True
    sync_usage(providers(), fetch=fetch, now=NOW.timestamp() + 600)
    assert calls == [1]
    assert UsageSync.objects.get().running_since is None
