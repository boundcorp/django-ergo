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
    assert links._checks([]) == ""
    assert (
        links._checks([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}])
        == "passing"
    )
    assert (
        links._checks([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}])
        == "failing"
    )
    assert links._checks([{"state": "PENDING"}]) == "pending"


def test_a_failed_gh_call_leaves_the_row(monkeypatch):
    class Row:
        url = "https://github.com/o/r/pull/1"

    monkeypatch.setattr(
        links.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "not found"),
    )
    assert not links.refresh_pull_request(Row())
