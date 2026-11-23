"""Pull request outcomes go to Hindsight as one replaceable document each."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from django_ergo import signals
from django_ergo.conversation.models import HindsightPrSync
from django_ergo.plugins import hindsight_memory as memory
from django_ergo.plugins import hindsight_pr_events as events

REPO = "acme/widget"
MERGED = {
    "number": 12,
    "title": "Fix login redirect",
    "state": "MERGED",
    "isDraft": False,
    "mergedAt": "2026-11-20T14:03:09Z",
    "closedAt": "2026-11-20T14:03:09Z",
    "updatedAt": "2026-11-20T14:05:00Z",
    "mergeCommit": {"oid": "abcdef1234567890"},
    "body": "SECRET BODY",
}
CLOSED = {
    **MERGED,
    "number": 13,
    "state": "CLOSED",
    "mergedAt": None,
    "closedAt": "2026-11-21T09:00:00Z",
    "mergeCommit": None,
}
OPEN = {
    **MERGED,
    "number": 14,
    "state": "OPEN",
    "isDraft": True,
    "mergedAt": None,
    "closedAt": None,
    "mergeCommit": None,
    "updatedAt": "2026-11-22T08:30:00Z",
}


def config(**extra):
    return events.PrEventsConfig({"users": ["alice"], **extra}, [REPO])


def plugin_for(bank="ergonaut", **pr_events):
    bot = SimpleNamespace(
        name="helper",
        definition=SimpleNamespace(pull_requests=[REPO], allowed_users=[]),
    )
    p = memory.HindsightMemoryPlugin(bot, {"bank": bank, "pr_events": pr_events})
    p.on_load()
    p.enabled = True
    return p, bot


@pytest.fixture
def alice(db):
    return get_user_model().objects.create_user("alice", password="x")


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(events, "list_pull_requests", lambda *a, **k: list(PRS))
    return calls


PRS: list = []


def post_to(calls):
    return lambda bank, path, payload, timeout: calls.append((bank, path, payload))


def run(alice, calls, prs, bank="ergonaut", **pr_events):
    PRS[:] = prs
    p, bot = plugin_for(bank, users=["alice"], **pr_events)
    return events.sync_bot(bot, p, post_to(calls))


def test_content_is_a_short_factual_line():
    assert events.content_for(REPO, MERGED) == (
        'GitHub pull request acme/widget#12 "Fix login redirect" is merged '
        "(merged 2026-11-20 14:03 UTC, merge commit abcdef1). Draft: no."
    )
    assert "closed without merging 2026-11-21 09:00 UTC" in events.content_for(
        REPO, CLOSED
    )
    text = events.content_for(REPO, OPEN)
    assert "is open" in text and "Draft: yes." in text


def test_item_tags_document_and_no_body(alice, sent):
    calls = []
    run(alice, calls, [MERGED], apps={REPO: "app-a"})
    ((bank, path, payload),) = calls
    item = payload["items"][0]
    assert (bank, path) == ("ergonaut", "memories")
    assert item["document_id"] == "pr:acme/widget#12"
    assert item["update_mode"] == "replace"
    assert set(item["tags"]) == {
        "source:github",
        "repo:acme/widget",
        "bot:helper",
        f"user:{alice.pk}",
        "app:app-a",
    }
    assert "GitHub" in item["context"] and "source of truth" in item["context"]
    assert "SECRET BODY" not in str(payload)


def test_rerun_is_idempotent_and_changes_replace(alice, sent):
    calls = []
    first = run(alice, calls, [MERGED, CLOSED])
    assert first == {"written": 2, "skipped": 0, "failed": 0}
    second = run(alice, calls, [MERGED, CLOSED])
    assert second == {"written": 0, "skipped": 2, "failed": 0}
    assert len(calls) == 1
    changed = {**OPEN, "state": "MERGED", "mergedAt": "2026-11-23T01:00:00Z"}
    run(alice, calls, [changed])
    assert calls[-1][2]["items"][0]["document_id"] == "pr:acme/widget#14"


def test_first_run_backfills_then_uses_cursor(alice, monkeypatch):
    seen = []
    monkeypatch.setattr(
        events,
        "list_pull_requests",
        lambda repo, since, files: seen.append(since) or [],
    )
    p, bot = plugin_for(users=["alice"])
    before = timezone.now()
    events.sync_bot(bot, p, post_to([]))
    assert (
        before - timedelta(days=14, seconds=5) <= seen[0] <= before - timedelta(days=13)
    )
    cursor = HindsightPrSync.objects.get(bot_name="helper", repo=REPO).cursor
    events.sync_bot(bot, p, post_to([]))
    assert seen[1] == cursor - events.OVERLAP


def test_failed_write_is_counted_and_keeps_cursor(alice, sent):
    PRS[:] = [MERGED]
    p, bot = plugin_for(users=["alice"])

    def boom(*args):
        msg = "down"
        raise RuntimeError(msg)

    assert events.sync_bot(bot, p, boom)["failed"] == 1
    row = HindsightPrSync.objects.get(bot_name="helper")
    assert row.cursor is None and row.hashes == {}
    calls = []
    assert events.sync_bot(bot, p, post_to(calls))["written"] == 1


def test_github_failure_is_counted(alice, monkeypatch):
    monkeypatch.setattr(events, "list_pull_requests", lambda *a, **k: None)
    p, bot = plugin_for(users=["alice"])
    assert events.sync_bot(bot, p, post_to([]))["failed"] == 1


def test_personal_bank_is_never_written(alice, sent):
    calls = []
    assert run(alice, calls, [MERGED], bank="personal")["written"] == 0
    assert calls == []
    # not even through a rule that names it
    out = run(
        alice,
        calls,
        [MERGED],
        rules=[{"title_prefix": "fix", "bank": "personal", "action": "bank"}],
    )
    assert out["written"] == 0 and calls == []


def test_rules_skip_or_reroute(alice, sent):
    area = {
        **MERGED,
        "number": 20,
        "title": "Area one: tweak",
        "files": [{"path": "area/a.md"}],
    }
    mixed = {
        **MERGED,
        "number": 21,
        "title": "other",
        "files": [{"path": "area/a.md"}, {"path": "src/x.py"}],
    }
    calls = []
    rules = [
        {"files_under": "area/", "repo": REPO},
        {"title_prefix": "route", "action": "bank", "bank": "side"},
    ]
    routed = {**MERGED, "number": 22, "title": "Route me", "files": []}
    out = run(alice, calls, [area, mixed, routed], rules=rules)
    assert out["written"] == 2 and out["skipped"] == 1
    assert sorted(c[0] for c in calls) == ["ergonaut", "side"]
    ids = {i["document_id"] for c in calls for i in c[2]["items"]}
    assert ids == {"pr:acme/widget#21", "pr:acme/widget#22"}


def test_states_filter(alice, sent):
    calls = []
    out = run(alice, calls, [MERGED, CLOSED, OPEN], states=["merged", "closed"])
    assert out == {"written": 2, "skipped": 1, "failed": 0}


def test_unknown_users_skip_the_bot(db, sent):
    calls = []
    assert run(None, calls, [MERGED]) == {"written": 0, "skipped": 0, "failed": 0}
    assert calls == []


def test_metrics_signal(alice, sent):
    got = []

    def receiver(sender, **kw):
        got.append((kw["event"], kw["outcome"], kw["reason"], kw["count"]))

    signals.memory_event.connect(receiver, dispatch_uid="t-pr")
    try:
        run(alice, [], [MERGED])
        run(alice, [], [MERGED])
    finally:
        signals.memory_event.disconnect(dispatch_uid="t-pr")
    assert ("pr_sync", "written", "", 1) in got
    assert ("pr_sync", "skipped", "unchanged", 1) in got


@override_settings(HINDSIGHT_URL="http://hindsight.test:8888")
def test_sync_all_only_runs_for_opted_in_bots(alice, sent):
    PRS[:] = [MERGED]
    on, bot_on = plugin_for(users=["alice"])
    off, bot_off = plugin_for()
    bot_on.plugins, bot_off.plugins = [on], [off]
    bot_off.name = "quiet"
    calls = []
    out = events.sync_all({"helper": bot_on, "quiet": bot_off}, post_to(calls))
    assert list(out) == ["helper"] and len(calls) == 1


def test_bad_config_is_rejected():
    with pytest.raises(ValueError, match="states"):
        events.PrEventsConfig({"states": ["merged", "bogus"]}, [])
    with pytest.raises(ValueError, match="rules need"):
        events.PrEventsConfig({"rules": [{"action": "skip"}]}, [])
