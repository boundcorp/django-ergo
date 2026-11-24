"""Ergonaut's processes report their own build revision; deploy watches use
only those reports (django_ergo.bots.live_watch)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django_ergo.bots import live_watch
from django_ergo.conversation.models import ConversationSession, LiveWatch, RuntimeReport, ThreadMessage

from ergonaut import runtime_report
from ergonaut.upgrades import github, revisions

NEW = "b" * 40
OLD = "a" * 40
QUEUED: list[str] = []


def record(message_id):
    """THREAD_MESSAGE_RUNNER that only records (no delivery thread racing teardown)."""
    QUEUED.append(message_id)


@pytest.fixture
def roles(settings):
    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "LIVE_ROLES": ["web", "celery", "bot-tasks", "beat", "bots"]}
    live_watch._unknown.clear()
    yield
    runtime_report.stop()


def test_production_watches_every_role_and_resolves_through_github():
    ergo = settings.DJANGO_ERGO
    assert ergo["REVISION_RESOLVER"] == "ergonaut.upgrades.revisions.resolve"
    assert ergo["REVISION_ANCESTRY"] == "ergonaut.upgrades.revisions.contains"
    assert settings.CELERY_BEAT_SCHEDULE["check-live-watches"] == {
        "task": "ergonaut.check_live_watches",
        "schedule": 30,
    }


def test_the_build_file_wins_over_the_environment(tmp_path, monkeypatch):
    baked = tmp_path / "BUILD_REVISION"
    baked.write_text(NEW.upper() + "\n")
    monkeypatch.setenv("ERGONAUT_BUILD_REVISION_FILE", str(baked))
    monkeypatch.setenv("ERGONAUT_VERSION", OLD)
    assert runtime_report.build_revision() == NEW

    baked.write_text("\n")  # a build without a version: fall back to the env
    assert runtime_report.build_revision() == OLD


def test_worker_roles_come_from_its_queues(monkeypatch):
    assert runtime_report.worker_roles(["celery"]) == ["celery"]
    assert runtime_report.worker_roles(["bot_tasks"]) == ["bot-tasks"]
    assert runtime_report.worker_roles(["turns", "celery", "bot_tasks"]) == ["celery", "bot-tasks"]
    assert runtime_report.worker_roles([]) == ["celery"]
    # A maintenance worker is a role of its own, which deploy watches don't require.
    assert runtime_report.worker_roles(["runpod_maintenance"]) == ["runpod-maintenance"]
    consumer = SimpleNamespace(task_consumer=SimpleNamespace(queues=[SimpleNamespace(name="bot_tasks")]))
    assert runtime_report._consumed_queues(consumer) == ["bot_tasks"]
    monkeypatch.setenv("ERGONAUT_ROLE", "voice, web")
    assert runtime_report.roles_for(["celery"]) == ["voice", "web"]


@pytest.mark.django_db(transaction=True)
def test_a_reporter_reports_withdraws_and_is_off_without_roles(roles, monkeypatch, settings):
    monkeypatch.setenv("ERGONAUT_VERSION", NEW)
    monkeypatch.setenv("ERGONAUT_BUILD_REVISION_FILE", "/nonexistent")
    monkeypatch.setenv("ERGONAUT_RUNTIME_REPORT_SECONDS", "3600")
    runtime_report.on_worker_ready(
        SimpleNamespace(task_consumer=SimpleNamespace(queues=[SimpleNamespace(name="bot_tasks")]))
    )
    report = RuntimeReport.objects.get()
    assert (report.role, report.revision, report.ready) == ("bot-tasks", NEW, True)
    assert report.instance == runtime_report.instance_id()

    # A pool child forked from this worker exits: the worker's report stays.
    reporter = runtime_report._reporter
    real_pid = reporter.pid
    reporter.pid = real_pid + 1
    reporter.stop()
    assert RuntimeReport.objects.count() == 1
    reporter.pid = real_pid

    runtime_report.on_shutdown()
    assert not RuntimeReport.objects.exists()

    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "LIVE_ROLES": []}
    assert runtime_report.start(["web"], ready=True) is None
    assert not RuntimeReport.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_bots_report_starting_until_ready(roles, monkeypatch):
    monkeypatch.setenv("ERGONAUT_VERSION", NEW)
    monkeypatch.setenv("ERGONAUT_BUILD_REVISION_FILE", "/nonexistent")
    monkeypatch.setenv("ERGONAUT_RUNTIME_REPORT_SECONDS", "3600")
    runtime_report.start(["bots"], ready=False)
    assert RuntimeReport.objects.get().ready is False
    runtime_report.mark_ready()
    assert RuntimeReport.objects.get().ready is True


@pytest.mark.django_db(transaction=True)
def test_the_beat_task_tells_the_chat_once(roles, settings):
    from ergonaut.apps.bots.tasks import check_live_watches

    importlib.import_module("ergonaut.tests.test_runtime_report").QUEUED.clear()
    settings.DJANGO_ERGO = {
        **settings.DJANGO_ERGO,
        "REVISION_ANCESTRY": None,
        "REVISION_RESOLVER": None,
        "THREAD_MESSAGE_RUNNER": "ergonaut.tests.test_runtime_report.record",
    }
    user = get_user_model().objects.create(username="lee")
    session = ConversationSession.objects.create(user=user, bot_name="ergo")
    watch, _ = live_watch.watch(session, NEW)
    for role in ["web", "celery", "bot-tasks", "beat"]:
        live_watch.report(f"{role}-pod", [role], NEW, ready=True)
    assert check_live_watches() == []  # bots hasn't reported
    live_watch.report("bots-pod", ["bots"], NEW, ready=True)
    assert check_live_watches() == [str(watch.id)]
    assert check_live_watches() == []
    (message,) = ThreadMessage.objects.filter(recipient_session=session)
    # The runner as Django imported it (pytest may load this file under another name).
    recorder = importlib.import_module("ergonaut.tests.test_runtime_report")
    assert recorder.QUEUED == [str(message.id)]
    watch.refresh_from_db()
    assert watch.status == "live"
    assert LiveWatch.objects.count() == 1


def test_short_shas_resolve_through_github(monkeypatch):
    monkeypatch.setattr(github, "commit", lambda repo, ref: (NEW, ""))
    assert revisions.resolve("BBBBBBB") == NEW
    assert revisions.resolve(NEW) == NEW
    monkeypatch.setattr(github, "commit", lambda repo, ref: (OLD, ""))
    with pytest.raises(ValueError, match="isn't that commit"):
        revisions.resolve("bbbbbbb")

    def missing(repo, ref):
        raise OSError("404")

    monkeypatch.setattr(github, "commit", missing)
    with pytest.raises(ValueError, match="Give the full commit SHA"):
        revisions.resolve("bbbbbbb")
    with pytest.raises(ValueError, match="not a commit SHA or an image digest"):
        revisions.resolve("main")


def test_ancestry_follows_githubs_compare(monkeypatch):
    calls = []

    def compare(repo, current, target):
        calls.append((current, target))
        return status

    monkeypatch.setattr(github, "compare", compare)
    for status, answer in [
        ("ahead", True),
        ("identical", True),
        ("behind", False),
        ("diverged", False),
        ("unknown", None),
    ]:
        assert revisions.contains(OLD, NEW) is answer
    # GitHub is asked how the running commit relates to the target.
    assert calls[0] == (OLD, NEW)


def test_an_image_digest_resolves_to_the_commit_it_was_built_from(monkeypatch):
    digest = "sha256:" + "d" * 64
    child = "sha256:" + "e" * 64
    config = "sha256:" + "f" * 64
    seen = []

    def fake_open(url, headers):
        seen.append(url)
        if "/token?" in url:
            return {"token": "t"}
        assert headers["Authorization"] == "Bearer t"
        if url.endswith(f"/manifests/{digest}"):
            return {
                "manifests": [
                    {"digest": "sha256:" + "0" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
                    {"digest": child, "platform": {"os": "linux", "architecture": "amd64"}},
                ]
            }
        if url.endswith(f"/manifests/{child}"):
            return {"config": {"digest": config}}
        if url.endswith(f"/blobs/{config}"):
            return {"config": {"Env": ["PATH=/x", f"ERGONAUT_VERSION={NEW}"]}}
        raise AssertionError(url)

    monkeypatch.setattr(revisions, "_open", fake_open)
    monkeypatch.setattr(github, "token", lambda: "")
    monkeypatch.delenv("ERGONAUT_IMAGE", raising=False)
    assert revisions.resolve(f"ghcr.io/boundcorp/ergonaut@{digest}") == NEW
    assert seen[1] == f"https://ghcr.io/v2/boundcorp/ergonaut/manifests/{digest}"

    def no_commit(url, headers):
        if "/token?" in url:
            return {"token": "t"}
        if "/manifests/" in url:
            return {"config": {"digest": config}}
        return {"config": {"Env": ["ERGONAUT_VERSION="]}}

    monkeypatch.setattr(revisions, "_open", no_commit)
    with pytest.raises(ValueError, match="doesn't record the commit"):
        revisions.resolve(digest)

    def denied(url, headers):
        raise OSError("denied")

    monkeypatch.setattr(revisions, "_open", denied)
    with pytest.raises(ValueError, match="Give the django-ergo commit SHA instead"):
        revisions.resolve(digest)


def test_image_reference_parsing(monkeypatch):
    monkeypatch.setenv("ERGONAUT_IMAGE", "registry.example.com:5000/team/ergonaut:main")
    assert revisions.image() == ("registry.example.com:5000", "team/ergonaut")
    monkeypatch.setenv("ERGONAUT_IMAGE", "ghcr.io/boundcorp/ergonaut")
    assert revisions.image() == ("ghcr.io", "boundcorp/ergonaut")
