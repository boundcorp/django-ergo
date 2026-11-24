"""Deploy watches (django_ergo.bots.live_watch): a chat is told once, and only
when every role's running processes report the revision and are ready."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone

from django_ergo.bots import live_watch
from django_ergo.bots import messaging
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.models import LiveWatch
from django_ergo.conversation.models import RevisionAncestry
from django_ergo.conversation.models import RuntimeReport
from django_ergo.conversation.models import ThreadMessage
from tests.test_bots import SENT
from tests.test_bots import make_bot

User = get_user_model()

ROLES = ["web", "celery", "bots", "bot-tasks", "beat"]
OLD = "a" * 40
NEW = "b" * 40
NEWER = "c" * 40


@pytest.fixture
def live(settings):
    from django.conf import settings as django_settings

    SENT.clear()
    settings.DJANGO_ERGO = {
        **getattr(django_settings, "DJANGO_ERGO", {}),
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
        "LIVE_ROLES": ROLES,
    }
    live_watch._unknown.clear()
    return settings


def chat(tmp_path, username="lee"):
    bot, _ = make_bot(tmp_path)
    user = User.objects.create(username=username)
    session = bot.sessions().create(
        user=user, bot_name=bot.name, metadata={"bot_role": "main"}
    )
    return bot, session


def roll(revision, *, roles=ROLES, pod="new", ready=True, now=None):
    for role in roles:
        live_watch.report(f"{role}-{pod}", [role], revision, ready=ready, now=now)


def events(session):
    return list(
        ThreadMessage.objects.filter(recipient_session=session).order_by("created_at")
    )


def later(minutes=0, hours=0):
    return timezone.now() + timedelta(minutes=minutes, hours=hours)


@pytest.mark.django_db(transaction=True)
def test_partial_rollout_is_not_live_until_every_role_is_ready_on_it(live, tmp_path):
    _, session = chat(tmp_path)
    roll(OLD, pod="old")
    w, created = live_watch.watch(session, NEW, note="tell Lee the fix shipped")
    assert created

    # The image was built and some pods rolled: web and celery run it, the rest don't.
    roll(NEW, roles=["web", "celery"])
    assert live_watch.check_due() == []
    v = live_watch.verdict(NEW)
    assert not v.live
    # Old web and celery pods are still running (terminating, not yet gone).
    assert "web-old runs aaaaaaaaaaaa" in v.roles["web"]["why"]

    # The old pods stop cleanly; the new bots pod is up but still starting.
    for role in ROLES:
        live_watch.withdraw(f"{role}-old")
    roll(NEW, roles=["bots", "bot-tasks", "beat"], ready=False)
    v = live_watch.verdict(NEW)
    assert not v.live
    assert v.roles["bots"]["why"] == "bots-new is still starting"
    assert live_watch.check_due() == []

    roll(NEW, roles=["bots", "bot-tasks", "beat"])
    assert live_watch.check_due() == [str(w.id)]
    assert live_watch.check_due() == []  # the next tick sends nothing

    w.refresh_from_db()
    (event,) = events(session)
    assert (w.status, w.thread_message_id) == ("live", event.id)
    assert w.evidence["live"] is True
    assert set(w.evidence["roles"]) == set(ROLES)
    assert event.text.startswith(f"[Deploy watch: {NEW[:12]} is live.")
    assert "- beat: bbbbbbbbbbbb (ok)" in event.text
    assert event.text.endswith("Your note:\ntell Lee the fix shipped")
    assert event.metadata["message_author"] == {
        "kind": "system",
        "ref": "live_watch",
        "display_name": "Deploy watch",
    }
    assert [str(event.id)] == SENT


@pytest.mark.django_db(transaction=True)
def test_a_role_that_never_reports_or_went_stale_is_not_live(live, tmp_path):
    _, session = chat(tmp_path)
    roll(NEW, roles=["web", "celery", "bots", "bot-tasks"])
    live_watch.watch(session, NEW)
    v = live_watch.verdict(NEW)
    assert not v.live
    assert v.missing() == ["beat: no running process has reported"]

    # A beat that reported long ago (crashed without withdrawing) doesn't count,
    # nor does a stale old pod hold the rollout back.
    stale = timezone.now() - timedelta(minutes=10)
    live_watch.report("beat-crashed", ["beat"], NEW, ready=True, now=stale)
    live_watch.report("web-crashed", ["web"], OLD, ready=True, now=stale)
    assert live_watch.verdict(NEW).missing() == [
        "beat: no running process has reported"
    ]

    roll(NEW, roles=["beat"])
    assert live_watch.verdict(NEW).live
    assert len(live_watch.check_due()) == 1


@pytest.mark.django_db(transaction=True)
def test_one_process_serving_several_roles_reports_each(live, tmp_path):
    _, session = chat(tmp_path)
    # `ergonaut up`: one Celery worker consumes both queues.
    live_watch.report("up-worker", ["celery", "bot-tasks"], NEW, ready=True)
    roll(NEW, roles=["web", "bots", "beat"])
    live_watch.watch(session, NEW)
    assert live_watch.verdict(NEW).live
    assert RuntimeReport.objects.filter(instance="up-worker").count() == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_checkers_send_one_event(live, tmp_path):
    _, session = chat(tmp_path)
    live_watch.watch(session, NEW)
    roll(NEW)
    start = threading.Barrier(4)
    results = []

    def check():
        try:
            start.wait()
            results.append(live_watch.check_due())
        finally:
            close_old_connections()

    runners = [threading.Thread(target=check) for _ in range(4)]
    for runner in runners:
        runner.start()
    for runner in runners:
        runner.join()

    assert sum(len(r) for r in results) == 1
    assert len(events(session)) == 1
    assert len(SENT) == 1


@pytest.mark.django_db(transaction=True)
def test_a_crash_mid_send_rolls_back_and_the_next_checker_sends_once(
    live, tmp_path, monkeypatch
):
    _, session = chat(tmp_path)
    w, _ = live_watch.watch(session, NEW)
    roll(NEW)

    class Killed(BaseException):  # the process dies; nothing is committed
        pass

    real = messaging.send_event

    def die(*args, **kwargs):
        raise Killed

    monkeypatch.setattr(messaging, "send_event", die)
    with pytest.raises(Killed):
        live_watch.check_due()
    w.refresh_from_db()
    assert (w.status, w.thread_message_id) == ("pending", None)
    assert events(session) == []

    # After the restart: in-process caches are gone, the rows are not.
    monkeypatch.setattr(messaging, "send_event", real)
    live_watch._unknown.clear()
    assert live_watch.check_due() == [str(w.id)]
    assert live_watch.check_due() == []
    assert len(events(session)) == 1


@pytest.mark.django_db(transaction=True)
def test_processes_restarting_on_the_same_revision_stay_live(live, tmp_path):
    _, session = chat(tmp_path)
    roll(NEW, pod="one")
    # Every pod restarts: new instance ids, ready only once started.
    for role in ROLES:
        live_watch.withdraw(f"{role}-one")
    roll(NEW, pod="two", ready=False)
    live_watch.watch(session, NEW)
    assert live_watch.check_due() == []
    roll(NEW, pod="two")
    assert len(live_watch.check_due()) == 1


@pytest.mark.django_db(transaction=True)
def test_a_watch_that_expires_says_so_once_and_never_goes_live(live, tmp_path):
    _, session = chat(tmp_path)
    roll(OLD)
    w, _ = live_watch.watch(session, NEW, hours=2, note="check #401")
    assert live_watch.check_due(later(hours=1)) == []

    deadline = later(hours=2, minutes=1)
    roll(OLD, now=deadline)  # the old pods are still running at the deadline
    assert live_watch.check_due(deadline) == [str(w.id)]
    w.refresh_from_db()
    (event,) = events(session)
    assert w.status == "expired"
    assert event.text.startswith(
        f"[Deploy watch: {NEW[:12]} is NOT live after 2.0 hours."
    )
    assert "- web: web-new runs aaaaaaaaaaaa" in event.text
    assert event.text.endswith("Your note:\ncheck #401")

    roll(NEW, now=later(hours=3))
    assert live_watch.check_due(later(hours=3)) == []
    assert len(events(session)) == 1


@pytest.mark.django_db(transaction=True)
def test_live_at_the_deadline_wins_over_expiry(live, tmp_path):
    _, session = chat(tmp_path)
    w, _ = live_watch.watch(session, NEW, hours=1)
    roll(NEW, now=later(hours=2))
    assert live_watch.check_due(later(hours=2)) == [str(w.id)]
    w.refresh_from_db()
    assert w.status == "live"


@pytest.mark.django_db(transaction=True)
def test_watches_are_per_chat_and_idempotent(live, tmp_path):
    _, session = chat(tmp_path)
    w, created = live_watch.watch(session, NEW)
    again, created_again = live_watch.watch(session, NEW.upper())
    assert created and not created_again and again.id == w.id
    assert LiveWatch.objects.count() == 1

    assert live_watch.cancel(session, str(w.id))
    rearmed, created = live_watch.watch(session, NEW)
    assert created and rearmed.id == w.id and rearmed.status == "pending"

    roll(NEW)
    live_watch.check_due()
    with pytest.raises(ValueError, match="already told"):
        live_watch.watch(session, NEW)
    assert len(events(session)) == 1


@pytest.mark.django_db(transaction=True)
def test_another_chat_cannot_see_or_cancel_a_watch(live, tmp_path):
    bot, mine = chat(tmp_path)
    theirs = bot.sessions().create(
        user=User.objects.create(username="other"), bot_name=bot.name
    )
    w, _ = live_watch.watch(mine, NEW)

    def kit(session):
        skill = next(d for d in bot.skill_defs if d.name == "deploys")
        ctx = ToolContext(bot=bot, session=session, user=session.user)
        return skill.toolkits(ctx)[0]

    theirs_kit = kit(theirs)
    out = json.loads(
        theirs_kit.execute_tool("ergo_live_watch_cancel", {"watch_id": str(w.id)})
    )
    assert out["cancelled"] is False
    assert json.loads(theirs_kit.execute_tool("ergo_live_watches", {}))["watches"] == []
    assert not live_watch.cancel(theirs, "not-a-uuid")
    w.refresh_from_db()
    assert w.status == "pending"

    # The tool takes no chat id: a watch always belongs to the calling chat.
    schema = theirs_kit.tools["ergo_notify_when_live"].json_schema()
    assert set(schema["properties"]) == {"revision", "note", "hours"}
    made = json.loads(
        theirs_kit.execute_tool("ergo_notify_when_live", {"revision": NEW})
    )
    assert made["created"] and made["live_now"] is False
    assert LiveWatch.objects.get(id=made["watch"]["id"]).session_id == theirs.id

    mine_kit = kit(mine)
    listed = json.loads(mine_kit.execute_tool("ergo_live_watches", {}))["watches"]
    assert [x["id"] for x in listed] == [str(w.id)]
    out = json.loads(
        mine_kit.execute_tool("ergo_live_watch_cancel", {"watch_id": str(w.id)})
    )
    assert out["cancelled"] is True

    # Both chats hear about the same revision, each once.
    roll(NEW)
    live_watch.check_due()
    assert len(events(theirs)) == 1
    assert events(mine) == []


@pytest.mark.django_db(transaction=True)
def test_a_descendant_counts_when_ancestry_can_be_checked(live, tmp_path):
    calls = []

    def ancestry(target, revision):
        calls.append((target, revision))
        return {(NEW, NEWER): True, (NEW, OLD): False}.get((target, revision))

    live.DJANGO_ERGO = {**live.DJANGO_ERGO, "REVISION_ANCESTRY": ancestry}
    _, session = chat(tmp_path)
    live_watch.watch(session, NEW)
    roll(NEWER)
    live_watch.report("beat-old", ["beat"], OLD, ready=True)
    assert not live_watch.verdict(NEW).live
    live_watch.withdraw("beat-old")
    assert live_watch.verdict(NEW).live
    assert len(live_watch.check_due()) == 1
    assert set(
        RevisionAncestry.objects.values_list("target", "revision", "contains")
    ) == {(NEW, NEWER, True), (NEW, OLD, False)}
    asked = len(calls)
    assert live_watch.verdict(NEW).live
    assert len(calls) == asked  # answers come from the cache


@pytest.mark.django_db(transaction=True)
def test_an_unknown_ancestry_is_not_live_and_not_cached(live, tmp_path):
    def unknown(target, revision):
        return None

    live.DJANGO_ERGO = {**live.DJANGO_ERGO, "REVISION_ANCESTRY": unknown}
    roll(NEWER)
    assert not live_watch.verdict(NEW).live
    assert not RevisionAncestry.objects.exists()

    # Without an ancestry check, only an exact match is live.
    live.DJANGO_ERGO = {**live.DJANGO_ERGO, "REVISION_ANCESTRY": None}
    live_watch._unknown.clear()
    assert not live_watch.verdict(NEW).live
    assert live_watch.verdict(NEWER).live


@pytest.mark.django_db(transaction=True)
def test_revisions_are_resolved_or_refused(live, tmp_path):
    _, session = chat(tmp_path)
    with pytest.raises(ValueError, match="not a full 40-character commit SHA"):
        live_watch.watch(session, "bbbbbbb")
    with pytest.raises(ValueError, match="not a full 40-character"):
        live_watch.watch(session, "sha256:" + "d" * 64)
    with pytest.raises(ValueError, match="hours must be"):
        live_watch.watch(session, NEW, hours=100)

    def resolver(text):
        if text == "bbbbbbb":
            return NEW
        msg = f"can't resolve {text}"
        raise ValueError(msg)

    live.DJANGO_ERGO = {**live.DJANGO_ERGO, "REVISION_RESOLVER": resolver}
    w, _ = live_watch.watch(session, "bbbbbbb")
    assert (w.revision, w.requested) == (NEW, "bbbbbbb")
    with pytest.raises(ValueError, match="can't resolve nope"):
        live_watch.watch(session, "nope")
    roll(NEW)
    live_watch.check_due()
    assert "(you asked for bbbbbbb)" in events(session)[0].text


@pytest.mark.django_db(transaction=True)
def test_a_chat_waits_for_a_bounded_number_of_revisions(live, tmp_path):
    _, session = chat(tmp_path)
    for i in range(live_watch.MAX_PENDING_PER_CHAT):
        live_watch.watch(session, f"{i:040x}")
    with pytest.raises(ValueError, match="already waits for 5 revisions"):
        live_watch.watch(session, NEW)


@pytest.mark.django_db(transaction=True)
def test_without_live_roles_there_is_no_skill(settings, tmp_path):
    settings.DJANGO_ERGO = {"LIVE_ROLES": []}
    bot, session = chat(tmp_path)
    assert "deploys" not in {d.name for d in bot.skill_defs}
    with pytest.raises(ValueError, match="LIVE_ROLES is empty"):
        live_watch.watch(session, NEW)


@pytest.mark.django_db(transaction=True)
def test_old_reports_are_pruned(live):
    live_watch.report("gone", ["web"], OLD, ready=True, now=later(hours=-30))
    live_watch.report("here", ["web"], NEW, ready=True)
    assert live_watch.prune() == 1
    assert list(RuntimeReport.objects.values_list("instance", flat=True)) == ["here"]
