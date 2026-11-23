"""Schedule identity, run-thread links and optional continuity context."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots import schedules
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.definition import BotDefinitionError
from django_ergo.bots.schedules import MAX_CONTINUITY_RUNS
from django_ergo.conversation.models import ConversationCompaction
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import ScheduleRun
from django_ergo.conversation.models import ThreadMessage
from tests.test_bots import make_bot

User = get_user_model()
LA = ZoneInfo("America/Los_Angeles")
RUNNER = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}


def yaml_for(extra: str = "", name: str = "weekly report", key: str = "") -> str:
    ident = f"id: {key}, " if key else ""
    return f"""
    name: kitchen
    timezone: America/Los_Angeles
    schedules:
      - {{{ident}name: "{name}", cron: "0 8 * * mon", users: [lee], {extra}
         message: "Review the week.", to: {{thread: "Report {{date:%b %d}}"}}}}
    """


def monday(week: int) -> datetime:
    return (datetime(2026, 10, 5, 8, 0, tzinfo=LA) + timedelta(weeks=week)).astimezone(
        UTC
    )


def run_week(bot, week: int) -> ThreadMessage:
    assert schedules.run_due([bot], monday(week))
    return ThreadMessage.objects.order_by("-created_at").first()


@pytest.fixture
def lee(db):
    return User.objects.create(username="lee")


@pytest.fixture
def setup(tmp_path, settings, lee):
    settings.DJANGO_ERGO = RUNNER
    made = []

    def make(extra="continuity: true,", **kwargs):
        made.append(1)
        folder = tmp_path / f"v{len(made)}"
        folder.mkdir()
        bot, _ = make_bot(folder, yaml_text=yaml_for(extra, **kwargs))
        return bot

    return make


def test_defaults_and_validation():
    definition = BotDefinition.from_dict(
        {
            "name": "k",
            "schedules": [{"name": "plain", "cron": "* * * * *", "message": "x"}],
        }
    )
    [schedule] = definition.schedules
    assert (schedule.key, schedule.continuity, schedule.continuity_runs) == (
        "plain",
        False,
        5,
    )
    base = {"name": "a", "cron": "* * * * *", "message": "x"}
    for bad in (
        {"continuity": "yes"},
        {"continuity_runs": 0},
        {"continuity_runs": MAX_CONTINUITY_RUNS + 1},
        {"continuity_runs": True},
        {"id": "has space"},
    ):
        with pytest.raises(BotDefinitionError):
            BotDefinition.from_dict({"name": "k", "schedules": [{**base, **bad}]})


@pytest.mark.django_db(transaction=True)
def test_run_threads_are_linked_and_survive_rename(setup):
    bot = setup(key="weekly-report")
    run_week(bot, 0)
    run = ScheduleRun.objects.get()
    assert run.schedule_key == "weekly-report"
    thread = ConversationSession.objects.get(metadata__bot_role="thread")
    assert thread.metadata["schedule_id"] == "weekly-report"
    assert thread.metadata["schedule_run"] == run.pk
    assert thread.metadata["schedule"] == "weekly report"

    renamed = setup(name="Monday review", key="weekly-report")
    text = run_week(renamed, 1).text
    assert "weekly report" not in text.split("Earlier runs")[0]
    assert str(thread.pk) in text  # the pre-rename thread is still found
    newest = ConversationSession.objects.order_by("-created_at").first()
    assert newest.metadata["schedule"] == "Monday review"
    assert newest.metadata["schedule_id"] == "weekly-report"


@pytest.mark.django_db(transaction=True)
def test_continuity_lists_earlier_runs_newest_first_and_excludes_current(setup):
    bot = setup()
    first = run_week(bot, 0)
    assert "Earlier runs" not in first.text  # nothing before it: no block
    second = run_week(bot, 1)
    third = run_week(bot, 2)
    assert second.text.startswith("Review the week.")
    assert str(first.recipient_session_id) in second.text
    assert str(second.recipient_session_id) not in second.text  # current run left out
    body = third.text
    assert body.index(str(second.recipient_session_id)) < body.index(
        str(first.recipient_session_id)
    )
    assert str(third.recipient_session_id) not in body
    assert "Report Oct 05" in body
    assert f"/s/{first.recipient_session_id}" in body


@pytest.mark.django_db(transaction=True)
def test_continuity_is_bounded_and_off_by_default(setup):
    bot = setup("continuity: true, continuity_runs: 2,")
    sent = [run_week(bot, week) for week in range(4)]
    last = sent[-1].text
    assert last.count("link /s/") == 2
    assert str(sent[0].recipient_session_id) not in last
    assert str(sent[2].recipient_session_id) in last

    off = setup("", name="plain off", key="other")
    off_first = run_week(off, 6)
    assert "Earlier runs" not in off_first.text
    explicit_off = setup("continuity: false,", name="off", key="third")
    assert "Earlier runs" not in run_week(explicit_off, 7).text


@pytest.mark.django_db(transaction=True)
def test_summaries_resolved_runs_and_missing_data(setup, lee):
    bot = setup()
    first = run_week(bot, 0)
    thread = first.recipient_session
    ConversationCompaction.objects.create(
        session=thread,
        mode="rolling",
        from_sequence=1,
        upto_sequence=1,
        summary="old summary",
    )
    ConversationCompaction.objects.create(
        session=thread,
        mode="rolling",
        from_sequence=1,
        upto_sequence=4,
        summary="Found   plenty. " + "x" * 2000,
    )
    thread.status = "completed"  # resolved
    thread.save(update_fields=["status"])
    text = run_week(bot, 1).text
    assert "resolved" in text
    assert "summary: Found plenty. x" in text
    assert "old summary" not in text
    assert "x" * 500 not in text  # clipped
    assert text.count("link /s/") == 1  # a thread with no history is still listed


@pytest.mark.django_db(transaction=True)
def test_continuity_text_is_capped_and_deleted_threads_are_skipped(setup):
    bot = setup("continuity: true, continuity_runs: 20,")
    sent = [run_week(bot, week) for week in range(3)]
    sent[0].recipient_session.delete()  # a deleted thread just drops out
    text = run_week(bot, 3).text
    assert text.count("link /s/") == 2
    run = ScheduleRun.objects.order_by("-pk").first()
    schedule = bot.definition.schedules[0]
    for session in ConversationSession.objects.filter(metadata__bot_role="thread"):
        ConversationCompaction.objects.create(
            session=session,
            mode="rolling",
            from_sequence=1,
            upto_sequence=1,
            summary="s" * 5000,
        )
    block = schedules.continuity_text(bot, run, schedule)
    assert len(block) <= schedules.CONTINUITY_MAX_CHARS + 200


@pytest.mark.django_db(transaction=True)
def test_legacy_threads_without_schedule_id_are_found(setup, lee):
    bot = setup(name="legacy")
    old = ConversationSession.objects.create(
        user=lee,
        bot_name="kitchen",
        metadata={"bot_role": "thread", "title": "Legacy Oct 01", "schedule": "legacy"},
    )
    assert str(old.pk) in run_week(bot, 0).text


@pytest.mark.django_db(transaction=True)
def test_continuity_never_crosses_people_or_bots(setup, lee, tmp_path):
    bot = setup()
    other = User.objects.create(username="guest")
    stranger = ConversationSession.objects.create(
        user=other,
        bot_name="kitchen",
        metadata={"bot_role": "thread", "schedule_id": "weekly report"},
    )
    elsewhere = ConversationSession.objects.create(
        user=lee,
        bot_name="pantry",
        metadata={"bot_role": "thread", "schedule_id": "weekly report"},
    )
    text = run_week(bot, 0).text
    assert "Earlier runs" not in text
    for session in (stranger, elsewhere):
        assert str(session.pk) not in text

    # The history tools read the person's own run threads, and only those.
    mine = ConversationSession.objects.get(
        metadata__bot_role="thread", user=lee, bot_name="kitchen"
    )
    anchor = ConversationSession.objects.create(
        user=lee, bot_name="kitchen", metadata={"bot_role": "main"}
    )
    ids = {s.source_id for s in bot.history_sources(anchor)}
    assert f"session:{mine.pk}" in ids
    assert f"session:{stranger.pk}" not in ids
    assert f"session:{elsewhere.pk}" not in ids
    assert bot.linked_source(anchor, str(mine.pk)) is not None
    assert bot.linked_source(anchor, str(stranger.pk)) is None


@pytest.mark.django_db(transaction=True)
def test_removed_schedule_fails_the_run_but_threads_stay_linked(setup, lee):
    bot = setup()
    run_week(bot, 0)
    gone = ScheduleRun.objects.create(
        bot_name="kitchen",
        schedule="removed",
        schedule_key="removed",
        user=lee,
        minute=monday(5),
    )
    schedules.run_actions(gone.pk, bot.registry)
    gone.refresh_from_db()
    assert gone.status == "failed"
    assert "ScheduleError" in gone.error
    thread = ConversationSession.objects.get(metadata__schedule_id="weekly report")
    assert thread.metadata["schedule_run"] == ScheduleRun.objects.first().pk
    # Running the same claimed run again does nothing (no duplicate thread).
    schedules.run_actions(gone.pk, bot.registry)
    assert ConversationSession.objects.filter(metadata__bot_role="thread").count() == 1


@pytest.mark.django_db(transaction=True)
def test_run_found_by_key_after_rename_between_claim_and_run(setup, lee):
    bot = setup(name="renamed", key="stable")
    run = ScheduleRun.objects.create(
        bot_name="kitchen",
        schedule="old name",
        schedule_key="stable",
        user=lee,
        minute=monday(0),
    )
    assert schedules.find_schedule(bot, run).name == "renamed"
    legacy = ScheduleRun.objects.create(
        bot_name="kitchen",
        schedule="renamed",
        schedule_key="stable",
        user=lee,
        minute=monday(1),
    )
    assert schedules.find_schedule(bot, legacy).key == "stable"


def two(a: dict, b: dict):
    base = {"cron": "* * * * *", "message": "x"}
    return BotDefinition.from_dict(
        {"name": "k", "schedules": [{**base, **a}, {**base, **b}]}
    )


def test_duplicate_keys_and_aliases_are_rejected():
    two({"name": "a", "id": "x"}, {"name": "b", "id": "y"})  # fine
    for a, b in (
        ({"name": "a", "id": "x"}, {"name": "b", "id": "x"}),
        ({"name": "a", "id": "b"}, {"name": "b"}),  # b's key is "b"
        (
            {"name": "a", "id": "x", "aliases": ["old"]},
            {"name": "b", "id": "y", "aliases": ["old"]},
        ),
        ({"name": "a", "id": "x"}, {"name": "b", "id": "y", "aliases": ["x"]}),
    ):
        with pytest.raises(BotDefinitionError, match="used by both"):
            two(a, b)
    for bad in ("old", [""], [3], ["x" * 101]):
        with pytest.raises(BotDefinitionError):
            two({"name": "a", "aliases": bad}, {"name": "b"})


@pytest.mark.django_db(transaction=True)
def test_rename_in_same_minute_claims_once(setup, lee):
    first = setup(name="before", key="stable")
    renamed = setup(name="after", key="stable")
    assert schedules.run_due([first], monday(0))
    assert schedules.run_due([renamed], monday(0)) == []
    assert ScheduleRun.objects.count() == 1
    assert ScheduleRun.objects.get().schedule_key == "stable"


@pytest.mark.django_db(transaction=True)
def test_adopting_an_id_keeps_history_and_dedup(setup, lee):
    old = setup(name="weekly report")  # implicit key: the name
    run_week(old, 0)
    adopted = setup(
        name="weekly report",
        key="weekly-report",
        extra="continuity: true, aliases: ['weekly report'],",
    )
    # Same minute as the legacy-keyed run: not claimed twice.
    assert schedules.run_due([adopted], monday(0)) == []
    text = run_week(adopted, 1).text
    assert "Earlier runs" in text
    assert text.count("link /s/") == 1
    # Without the alias the old history is not found (and nothing is guessed).
    plain = setup(name="weekly report", key="weekly-report")
    # (only the adopted run's thread, not the week-0 one under the old name)
    assert run_week(plain, 2).text.count("link /s/") == 1


@pytest.mark.django_db(transaction=True)
def test_missing_key_does_not_fall_back_to_a_replacement_by_name(setup, lee):
    bot = setup(name="old", key="new-id")
    run = ScheduleRun.objects.create(
        bot_name="kitchen",
        schedule="new-id-x",
        schedule_key="gone",
        user=lee,
        minute=monday(0),
    )
    assert schedules.find_schedule(bot, run) is None
    named = ScheduleRun.objects.create(
        bot_name="kitchen",
        schedule="old",
        schedule_key="old",
        user=lee,
        minute=monday(1),
    )
    assert schedules.find_schedule(bot, named) is None  # name matches, key doesn't
    schedules.run_actions(run.pk, bot.registry)
    run.refresh_from_db()
    assert run.status == "failed"
    assert not ConversationSession.objects.filter(metadata__bot_role="thread").exists()


@pytest.mark.django_db(transaction=True)
def test_backfill_migration_keys_legacy_rows(lee):
    import importlib

    from django.apps import apps

    mod = importlib.import_module("django_ergo.migrations.0054_schedule_run_key_unique")
    legacy = ScheduleRun.objects.create(
        bot_name="kitchen", schedule="old name", user=lee, minute=monday(0)
    )
    keyed = ScheduleRun.objects.create(
        bot_name="kitchen", schedule="n", schedule_key="k", user=lee, minute=monday(0)
    )
    mod.backfill_keys(apps, None)
    legacy.refresh_from_db()
    keyed.refresh_from_db()
    assert (legacy.schedule_key, keyed.schedule_key) == ("old name", "k")
