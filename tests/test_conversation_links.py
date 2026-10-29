"""Pull requests a chat produced (django_ergo.conversation.links)."""

from __future__ import annotations

import json
import subprocess

import pytest
from django.contrib.auth import get_user_model

from django_ergo.conversation import links
from django_ergo.conversation.models import ConversationSession


def test_pull_request_urls_are_found_once_each():
    text = (
        "Opened https://github.com/boundcorp/ergo-bots/pull/31 and "
        "https://github.com/boundcorp/django-ergo/pull/89/files; see also "
        "https://github.com/boundcorp/ergo-bots/pull/31 again."
    )
    assert links.pull_request_urls(text) == [
        ("https://github.com/boundcorp/ergo-bots/pull/31", "boundcorp/ergo-bots", 31),
        (
            "https://github.com/boundcorp/django-ergo/pull/89",
            "boundcorp/django-ergo",
            89,
        ),
    ]
    assert links.pull_request_urls("no links") == []


@pytest.mark.django_db
def test_recording_and_refreshing_a_pull_request(monkeypatch):
    user = get_user_model().objects.create_user("lee")
    session = ConversationSession.objects.create(user=user, bot_name="devbox")
    [row] = links.record_pull_requests(
        session, "PR: https://github.com/boundcorp/django-ergo/pull/90"
    )
    assert (row.url, row.filename, row.source) == (
        "https://github.com/boundcorp/django-ergo/pull/90",
        "django-ergo#90",
        "bot",
    )
    assert links.record_pull_requests(session, row.url) == []  # already recorded

    def gh(args, **kwargs):
        assert args[:4] == ["gh", "pr", "view", row.url]
        data = {
            "title": "Sidebar",
            "state": "OPEN",
            "isDraft": False,
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS", "conclusion": ""},
            ],
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    monkeypatch.setattr(links.subprocess, "run", gh)
    assert links.refresh_pull_request(row)
    out = links.pull_request_out(row)
    assert (out["title"], out["state"], out["checks"]) == ("Sidebar", "open", "pending")
    assert list(links.pull_requests_to_refresh()) == [row]

    row.metadata["state"] = "merged"
    row.save()
    assert not links.pull_requests_to_refresh().exists()


def test_check_rollups():
    assert links.checks_state([]) == ""
    assert (
        links.checks_state([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}])
        == "passing"
    )
    assert (
        links.checks_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}])
        == "failing"
    )
    assert links.checks_state([{"state": "PENDING"}]) == "pending"


def test_a_failed_gh_call_leaves_the_row(monkeypatch):
    class Row:
        url = "https://github.com/o/r/pull/1"

    monkeypatch.setattr(
        links.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "not found"),
    )
    assert not links.refresh_pull_request(Row())


@pytest.mark.django_db
def test_open_pull_requests_feed_the_orchestrator_block():
    from types import SimpleNamespace

    user = get_user_model().objects.create_user("lee")
    devbox = ConversationSession.objects.create(user=user, bot_name="devbox")
    kitchen = ConversationSession.objects.create(user=user, bot_name="kitchen")
    [mine] = links.record_pull_requests(
        devbox, "https://github.com/boundcorp/django-ergo/pull/1"
    )
    [merged] = links.record_pull_requests(
        devbox, "https://github.com/boundcorp/django-ergo/pull/2"
    )
    merged.metadata["state"] = "merged"
    merged.save()
    [watched] = links.record_pull_requests(
        kitchen, "https://github.com/boundcorp/ergo-bots/pull/3"
    )
    watched.metadata.update(state="draft", title="Icons")
    watched.save()
    links.record_pull_requests(kitchen, "https://github.com/other/repo/pull/4")

    bot = SimpleNamespace(
        name="devbox", definition=SimpleNamespace(pull_requests=["boundcorp/ergo-bots"])
    )
    assert links.open_pull_requests(bot) == [
        {"repo": "boundcorp/ergo-bots", "number": 3, "title": "Icons", "draft": True},
        {"repo": "boundcorp/django-ergo", "number": 1, "title": "", "draft": False},
    ]


@pytest.mark.django_db
def test_finished_checks_wake_the_bot_on_its_own_pr(monkeypatch):
    from types import SimpleNamespace

    from django_ergo.bots import background
    from django_ergo.bots import messaging

    user = get_user_model().objects.create_user("lee")
    session = ConversationSession.objects.create(user=user, bot_name="ads")
    [mine, theirs] = links.record_pull_requests(
        session,
        "https://github.com/boundcorp/ergo-bots/pull/135 and "
        "https://github.com/boundcorp/ergo-bots/pull/126",
    )
    plugin = SimpleNamespace(watch_checks=True)
    bot = SimpleNamespace(registry=None, plugin=lambda name: plugin)
    monkeypatch.setattr(background, "find_bot", lambda name: bot)
    sent = []
    monkeypatch.setattr(
        messaging,
        "send",
        lambda sender, recipient, text, **kw: sent.append((recipient, text, kw)),
    )
    views = {
        mine.url: {"headRefName": "bot/ads/20261029-ads-accounts"},
        theirs.url: {"headRefName": "claude/project-thread-aon3h2"},
    }
    rollup = [{"conclusion": "IN_PROGRESS"}]

    def gh(args, **kwargs):
        data = {"title": "x", "state": "OPEN", "statusCheckRollup": rollup}
        return subprocess.CompletedProcess(
            args, 0, json.dumps({**data, **views[args[3]]}), ""
        )

    monkeypatch.setattr(links.subprocess, "run", gh)
    assert links.refresh_pull_request(mine) and links.refresh_pull_request(theirs)
    assert sent == []  # pending

    rollup = [{"conclusion": "FAILURE"}]
    links.refresh_pull_request(mine)
    links.refresh_pull_request(theirs)  # not the bot's branch: no message
    [(recipient, text, kw)] = sent
    assert recipient == session
    assert "Checks failed on boundcorp/ergo-bots#135" in text
    assert "ergo_config_repo_checkout_pr(135)" in text
    assert kw["metadata"] == {"pull_request": mine.url, "checks": "failing"}
    links.refresh_pull_request(mine)
    assert len(sent) == 1  # still failing: said once

    rollup = [{"conclusion": "SUCCESS"}]
    links.refresh_pull_request(mine)
    assert "Checks passed" in sent[-1][1]

    rollup = [{"conclusion": "FAILURE"}]
    plugin.watch_checks = False
    links.refresh_pull_request(mine)
    assert len(sent) == 2
