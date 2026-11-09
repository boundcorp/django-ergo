from __future__ import annotations

from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model

from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.definition import BotDefinitionError
from django_ergo.bots.schedules import Cron
from django_ergo.bots.schedules import ScheduleError
from tests.test_bots import make_bot

User = get_user_model()
LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize(
    ("expression", "moment", "expected"),
    [
        ("0 17 * * sun", datetime(2026, 10, 4, 17, 0, tzinfo=LA), True),  # a Sunday
        ("0 17 * * sun", datetime(2026, 10, 5, 17, 0, tzinfo=LA), False),
        ("0 17 * * 7", datetime(2026, 10, 4, 17, 0, tzinfo=LA), True),
        (
            "*/15 9-17 * * mon-fri",
            datetime(2026, 10, 2, 9, 45, tzinfo=LA),
            True,
        ),  # a Friday
        ("*/15 9-17 * * mon-fri", datetime(2026, 10, 2, 9, 50, tzinfo=LA), False),
        ("30 8 1,15 * *", datetime(2026, 10, 15, 8, 30, tzinfo=LA), True),
        ("0 0 1 jan *", datetime(2027, 1, 1, 0, 0, tzinfo=LA), True),
        (
            "0 12 13 * fri",
            datetime(2026, 10, 13, 12, 0, tzinfo=LA),
            True,
        ),  # day OR weekday
        ("0 12 13 * fri", datetime(2026, 10, 2, 12, 0, tzinfo=LA), True),
    ],
)
def test_cron_matches(expression, moment, expected):
    assert Cron.parse(expression).matches(moment) is expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "* * * *",
        "61 * * * *",
        "* 24 * * *",
        "*/0 * * * *",
        "5-2 * * * *",
        "0 0 * * xyz",
    ],
)
def test_bad_cron_is_refused(bad):
    with pytest.raises(ScheduleError):
        Cron.parse(bad)


def test_next_run():
    cron = Cron.parse("0 17 * * sun")
    assert cron.next_after(datetime(2026, 10, 2, 9, 0, tzinfo=LA)) == datetime(
        2026, 10, 4, 17, 0, tzinfo=LA
    )
    assert (
        Cron.parse("0 0 29 feb *").next_after(datetime(2026, 10, 2, tzinfo=LA)) is None
    )  # past 400 days
    assert Cron.parse("0 0 29 feb *").next_after(
        datetime(2027, 10, 2, tzinfo=LA)
    ) == datetime(2028, 2, 29, 0, 0, tzinfo=LA)
    assert (
        Cron.parse("*/5 * * * *")
        .next_after(datetime(2026, 10, 2, 9, 3, tzinfo=LA))
        .minute
        == 5
    )


def test_schedules_in_bot_yaml_are_checked():
    good = BotDefinition.from_dict(
        {
            "name": "k",
            "schedules": [
                {"name": "plan", "cron": "0 17 * * sun", "message": "Plan", "to": "new"}
            ],
        }
    )
    assert [
        (s.name, s.actions[0].to, s.actions[0].thread_title) for s in good.schedules
    ] == [("plan", "thread", "plan %b %d")]
    for bad, error in [
        ([{"name": "plan", "cron": "0 17 * *", "message": "x"}], "5 fields"),
        ([{"name": "plan", "message": "x"}], "needs name, cron and message"),
        (
            [{"name": "plan", "cron": "* * * * *", "message": "x", "to": "everyone"}],
            "isn.t in chats",
        ),
        ([{"name": "p", "cron": "* * * * *", "message": "x"}] * 2, "two named"),
    ]:
        with pytest.raises(BotDefinitionError, match=error):
            BotDefinition.from_dict({"name": "k", "schedules": bad})


SCHEDULED = """
    name: kitchen
    timezone: America/Los_Angeles
    tools: [tools/pantry.py]
    schedules:
      - {name: weekly-plan, cron: "0 17 * * sun", message: "Plan next week's dinners."}
      - {name: shopping, cron: "0 9 * * sat", message: "What do we need?", to: new, users: [lee]}
      - {name: paused, cron: "* * * * *", message: "Never", enabled: false}
"""


@pytest.mark.django_db(transaction=True)
def test_run_due_sends_each_schedule_once(tmp_path, settings):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    bot, _ = make_bot(tmp_path, yaml_text=SCHEDULED)
    lee = User.objects.create(username="lee")
    User.objects.create(username="guest")  # no chat with the bot: not scheduled
    ConversationSession.objects.create(
        user=lee, bot_name="kitchen", metadata={"bot_role": "main"}
    )

    sunday_5pm = datetime(2026, 10, 4, 17, 0, tzinfo=LA).astimezone(UTC)
    assert schedules.run_due([bot], sunday_5pm) == ["kitchen/weekly-plan/lee"]
    assert schedules.run_due([bot], sunday_5pm) == []  # once per minute
    sent = ThreadMessage.objects.get()
    assert sent.sender_session_id is None
    assert sent.metadata == {
        "schedule": "weekly-plan",
        "message_author": {
            "kind": "system",
            "ref": "schedule:weekly-plan",
            "display_name": "Schedule: weekly-plan",
        },
        "message_provenance": {"kind": "event"},
    }
    assert sent.recipient_session.metadata["bot_role"] == "main"

    saturday_9am = datetime(2026, 10, 3, 9, 0, tzinfo=LA).astimezone(UTC)
    assert schedules.run_due([bot], saturday_9am) == ["kitchen/shopping/lee"]
    thread = ThreadMessage.objects.get(metadata__schedule="shopping").recipient_session
    assert thread.metadata["bot_role"] == "thread"
    assert thread.metadata["title"] == "shopping Oct 03"

    from django_ergo.bots.messaging import turn_text

    assert "automated notification, not a message from the user" in turn_text(sent)
    assert turn_text(sent).endswith("\n\nPlan next week's dinners.")


def test_schedule_targets_and_thread_titles():
    from django_ergo.bots.schedules import Schedule

    definition = BotDefinition.from_dict(
        {
            "name": "k",
            "chats": {"reports": {"description": "Weekly analytics"}},
            "schedules": [
                {"name": "a", "cron": "0 9 * * mon", "message": "x", "to": "reports"},
                {
                    "name": "b",
                    "cron": "0 9 * * *",
                    "message": "x",
                    "to": {"thread": "Reports {n}", "in": "reports"},
                },
                {
                    "name": "c",
                    "cron": "0 9 * * *",
                    "message": "x",
                    "to": {"thread": "Digest {date:%b %d} (#{n})"},
                },
                {"name": "d", "cron": "0 9 * * *", "message": "x", "to": "root"},
            ],
        }
    )
    a, b, c, d = (s.actions[0] for s in definition.schedules)
    assert (a.to, b.to, b.thread_in, c.thread_in, d.to) == (
        "reports",
        "thread",
        "reports",
        "main",
        "main",
    )
    moment = datetime(2026, 10, 5, 9, 0, tzinfo=LA)
    assert b.title_for(moment, 3) == "Reports 3"
    assert c.title_for(moment, 12) == "Digest Oct 05 (#12)"
    assert (
        Schedule.from_config(
            {
                "name": "e",
                "cron": "* * * * *",
                "message": "x",
                "to": {"thread": "Day %d"},
            }
        )
        .actions[0]
        .title_for(moment, 1)
        == "Day 05"
    )
    with pytest.raises(BotDefinitionError, match="isn't in chats"):
        BotDefinition.from_dict(
            {
                "name": "k",
                "schedules": [
                    {"name": "a", "cron": "* * * * *", "message": "x", "to": "nope"}
                ],
            }
        )


@pytest.mark.parametrize("target", ["reports", {"thread": "Digest", "in": "reports"}])
def test_schedule_rejects_retired_chat_target(target):
    config = {
        "name": "k",
        "chats": {"reports": {"retired": True}},
        "schedules": [{"name": "a", "cron": "* * * * *", "message": "x", "to": target}],
    }
    with pytest.raises(BotDefinitionError, match="targets retired chat 'reports'"):
        BotDefinition.from_dict(config)
    config["chats"]["reports"]["retired"] = False
    assert BotDefinition.from_dict(config).schedules


STATS_TOOLS = """
def pull_stats(days: int = 1) -> dict:
    return {"visits": 10 * days}


def with_context(ctx, label: str) -> str:
    return f"{label} for {ctx.user.username} on {ctx.bot.name}"


def broken() -> None:
    raise RuntimeError("the API is down")


def alerts(found: bool = False) -> list:
    return ["sync is stale"] if found else []
"""


def test_actions_are_checked():
    for bad, error in [
        ({"actions": []}, "non-empty list"),
        ({"actions": [{"wat": 1}]}, "or {run"),
        ({"actions": [{"run": "/etc/passwd.py:x"}]}, "in the bot folder"),
        ({"actions": [{"run": "tools/a.py"}]}, "in the bot folder"),
        ({"actions": [{"run": "tools/a.py:f", "args": [1]}]}, "args must be a mapping"),
        ({"actions": [{"prompt": "hi", "to": "nope"}]}, "isn't in chats"),
    ]:
        with pytest.raises(BotDefinitionError, match=error):
            BotDefinition.from_dict(
                {"name": "k", "schedules": [{"name": "s", "cron": "* * * * *", **bad}]}
            )


@pytest.mark.django_db(transaction=True)
def test_run_steps_feed_the_next_prompt_and_failures_stop_the_rest(tmp_path, settings):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import BotJob
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    yaml_text = """
        name: stats
        timezone: America/Los_Angeles
        schedules:
          - name: weekly
            cron: "0 8 * * mon"
            users: [lee]
            actions:
              - {run: "jobs/stats.py:pull_stats", args: {days: 7}}
              - {run: "jobs/stats.py:with_context", args: {label: Report}}
              - {prompt: "Summarize: {result}", to: {thread: "Stats {date:%b %d}"}}
          - name: flaky
            cron: "0 9 * * mon"
            users: [lee]
            actions:
              - {run: "jobs/stats.py:broken"}
              - {prompt: "never sent"}
    """
    bot, _ = make_bot(tmp_path, yaml_text=yaml_text, name="stats")
    (bot.definition.root_dir / "jobs").mkdir()
    (bot.definition.root_dir / "jobs" / "stats.py").write_text(STATS_TOOLS)
    User.objects.create(username="lee")

    monday_8am = datetime(2026, 10, 5, 8, 0, tzinfo=LA).astimezone(UTC)
    assert schedules.run_due([bot], monday_8am) == ["stats/weekly/lee"]
    first, second = BotJob.objects.order_by("created_at")
    assert (first.status, first.result, first.target) == (
        "completed",
        {"visits": 70},
        "jobs/stats.py:pull_stats",
    )
    assert second.result == "Report for lee on stats"
    sent = ThreadMessage.objects.get()
    assert sent.text == "Summarize: Report for lee on stats"
    assert sent.recipient_session.metadata["title"] == "Stats Oct 05"

    monday_9am = datetime(2026, 10, 5, 9, 0, tzinfo=LA).astimezone(UTC)
    assert schedules.run_due([bot], monday_9am) == ["stats/flaky/lee"]
    failed = BotJob.objects.get(target="jobs/stats.py:broken")
    assert failed.status == "failed"
    assert "RuntimeError" in failed.error
    assert "the API is down" not in failed.error
    assert "RuntimeError" in failed.traceback
    assert ThreadMessage.objects.count() == 1  # the prompt after it never went out
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import SessionMessage

    run = ScheduleRun.objects.get(schedule="flaky")
    assert run.status == "failed"
    assert run.steps[0]["status"] == "failed"
    notice = SessionMessage.objects.get()
    assert "Schedule flaky failed" in notice.content_blocks.get().text
    assert notice.session.metadata["bot_role"] == "main"


@pytest.mark.django_db(transaction=True)
def test_stop_if_empty_skips_the_prompt_when_nothing_is_found(tmp_path, settings):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import BotJob
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    yaml_text = """
        name: stats
        timezone: America/Los_Angeles
        schedules:
          - name: quiet
            cron: "0 8 * * *"
            users: [lee]
            actions:
              - {run: "jobs/stats.py:alerts", stop_if_empty: true}
              - {prompt: "Alerts: {result}"}
          - name: loud
            cron: "0 9 * * *"
            users: [lee]
            actions:
              - {run: "jobs/stats.py:alerts", args: {found: true}, stop_if_empty: true}
              - {prompt: "Alerts: {result}"}
    """
    bot, _ = make_bot(tmp_path, yaml_text=yaml_text, name="stats")
    (bot.definition.root_dir / "jobs").mkdir()
    (bot.definition.root_dir / "jobs" / "stats.py").write_text(STATS_TOOLS)
    User.objects.create(username="lee")

    schedules.run_due([bot], datetime(2026, 10, 5, 8, 0, tzinfo=LA).astimezone(UTC))
    assert BotJob.objects.get().status == "completed"  # empty, but not a failure
    assert not ThreadMessage.objects.exists()

    schedules.run_due([bot], datetime(2026, 10, 5, 9, 0, tzinfo=LA).astimezone(UTC))
    assert "sync is stale" in ThreadMessage.objects.get().text


@pytest.mark.django_db(transaction=True)
def test_code_only_schedules_run_once_as_the_first_admin(tmp_path):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import BotJob

    yaml_text = """
        name: stats
        timezone: America/Los_Angeles
        schedules:
          - {name: pull, cron: "0 */12 * * *", actions: [{run: "jobs/stats.py:pull_stats"}]}
    """
    bot, _ = make_bot(tmp_path, yaml_text=yaml_text, name="stats")
    (bot.definition.root_dir / "jobs").mkdir()
    (bot.definition.root_dir / "jobs" / "stats.py").write_text(STATS_TOOLS)
    User.objects.create(username="guest")
    User.objects.create(username="lee", is_superuser=True)
    User.objects.create(username="other-admin", is_superuser=True)

    noon = datetime(2026, 10, 5, 12, 0, tzinfo=LA).astimezone(UTC)
    # Nobody has a chat with the bot, and it still runs: once, as the first admin.
    assert schedules.run_due([bot], noon) == ["stats/pull/lee"]
    assert BotJob.objects.get().status == "completed"


WORKFLOW_CODE = """
def inspect_context(context, *, label):
    assert context["bot"].name == "workflow"
    assert context["user"].username == "lee"
    assert context["run"].schedule == "check"
    assert context["models"]["notify"] is True
    return {"ordered": context["result"] == context["models"], "literal": "{models}", "label": label}


async def async_step(context):
    return {"ready": True}


def broken(context):
    raise RuntimeError("credential=sk-DO-NOT-STORE-123456789")


def private_result(context):
    return {"api_key": "do-not-store", "message": "secret-from-env",
            "capabilities": {"maxTokens": 8192, "max_tokens": 4096, "access_token": "hide-me"}}
"""


def workflow_bot(tmp_path, workflow, *, timezone=None):
    import yaml

    schedule = {
        "name": "check",
        "cron": "0 9 * * sun",
        "users": ["lee"],
        "workflow": workflow,
    }
    if timezone:
        schedule["timezone"] = timezone
    bot, _ = make_bot(
        tmp_path,
        name="workflow",
        yaml_text=yaml.safe_dump(
            {
                "name": "workflow",
                "timezone": "America/Los_Angeles",
                "chats": {"reports": {"description": "Reports"}},
                "schedules": [schedule],
            }
        ),
    )
    (bot.definition.root_dir / "workflow.py").write_text(WORKFLOW_CODE)
    return bot


@pytest.mark.parametrize(
    "expression",
    [
        "models.notify",
        "models['notify'] and models.count >= 2",
        "not models.missing",
        "'new' in models.names",
        "models.names[0] == 'new'",
        "models.count == 2 or models.unknown",
    ],
)
def test_workflow_conditions_are_json_only(expression):
    from django_ergo.bots.schedule_workflow import condition

    assert condition(
        expression,
        {
            "models": {"notify": True, "count": 2, "missing": False, "names": ["new"]},
        },
    )


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "models.get('notify')",
        "models.__class__",
        "models['notify'].__class__",
        "models.count + 1",
        "[x for x in models]",
        "(lambda: True)()",
        "True or models.clear()",
        "models is models",
    ],
)
def test_workflow_refuses_executable_conditions(expression):
    from django_ergo.bots.schedules import Schedule

    with pytest.raises(ScheduleError):
        Schedule.from_config(
            {
                "name": "s",
                "cron": "* * * * *",
                "workflow": [{"if": expression}],
            }
        )


@pytest.mark.parametrize(
    "workflow",
    [
        [],
        [{"python": "os.system"}],
        [{"python": "../file.py:run"}],
        [{"python": "file.py:run", "save_as": "bot"}],
        [{"python": "file.py:run", "save_as": "__secret"}],
        [{"python": "file.py:run", "save_as": "x"}] * 2,
        [{"python": "file.py:run", "prompt": "ambiguous"}],
        [{"prompt": "text", "llm": "false"}],
    ],
)
def test_workflow_configuration_is_checked(workflow):
    from django_ergo.bots.schedules import Schedule

    with pytest.raises(ScheduleError):
        Schedule.from_config({"name": "s", "cron": "* * * * *", "workflow": workflow})


@pytest.mark.django_db(transaction=True)
def test_workflow_orders_context_templates_and_audits_once(
    tmp_path, settings, monkeypatch
):
    from django_ergo.bots import model_catalog
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    seen = []

    def check(context):
        seen.append((context["bot"].name, context["user"].username, context["run"].pk))
        return {"notify": True, "new": ["model-new"]}

    monkeypatch.setattr(model_catalog, "check_available", check)
    bot = workflow_bot(
        tmp_path,
        [
            {"python": "ergo.models.check_available", "save_as": "models"},
            {"if": "models.notify"},
            {
                "python": "workflow.py:inspect_context",
                "args": {"label": "catalog-review"},
                "save_as": "summary",
            },
            {
                "prompt": "Models: {models}\nSummary: {summary}",
                "to": {"thread": "New models {date:%Y-%m-%d}"},
            },
        ],
        timezone="UTC",
    )
    User.objects.create(username="lee")
    now = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
    assert schedules.run_due([bot], now) == ["workflow/check/lee"]
    run = ScheduleRun.objects.get()
    assert seen == [("workflow", "lee", run.pk)]
    assert run.status == "completed"
    assert run.started_at <= run.completed_at
    assert [step["kind"] for step in run.steps] == ["python", "if", "python", "prompt"]
    assert all(step["status"] == "completed" for step in run.steps)
    assert all(step["started_at"] <= step["completed_at"] for step in run.steps)
    assert run.steps[0]["result"]["new"] == ["model-new"]
    assert run.steps[2]["result"]["ordered"] is True
    assert run.steps[2]["result"]["label"] == "catalog-review"
    sent = ThreadMessage.objects.get()
    assert '"model-new"' in sent.text
    assert '"literal": "{models}"' in sent.text  # no recursive interpolation
    assert sent.recipient_session.metadata["title"] == "New models 2026-10-04"
    schedules.run_actions(run.pk, bot.registry)
    assert ThreadMessage.objects.count() == 1
    assert len(seen) == 1


@pytest.mark.django_db(transaction=True)
def test_false_workflow_is_quiet_without_sessions_or_dispatch(
    tmp_path, settings, monkeypatch
):
    from django_ergo.bots import model_catalog
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import SessionMessage
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    monkeypatch.setattr(
        model_catalog, "check_available", lambda context: {"notify": False}
    )
    bot = workflow_bot(
        tmp_path,
        [
            {"python": "ergo.models.check_available", "save_as": "models"},
            {"if": "models.notify"},
            {"python": "workflow.py:broken"},
            {"prompt": "Never", "to": {"thread": "Never"}},
        ],
        timezone="UTC",
    )
    User.objects.create(username="lee")
    schedules.run_due([bot], datetime(2026, 10, 4, 9, 0, tzinfo=UTC))
    run = ScheduleRun.objects.get()
    assert run.status == "noop"
    assert len(run.steps) == 2
    assert run.steps[1]["result"] is False
    assert not ConversationSession.objects.exists()
    assert not SessionMessage.objects.exists()
    assert not ThreadMessage.objects.exists()


@pytest.mark.parametrize(
    ("target", "role"),
    [
        ("main", "main"),
        ("reports", "chat"),
        ({"thread": "Summary"}, "thread"),
    ],
)
@pytest.mark.django_db(transaction=True)
def test_workflow_message_only_targets_without_llm(tmp_path, settings, target, role):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import SessionMessage
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    bot = workflow_bot(
        tmp_path,
        [
            {"python": "workflow.py:async_step", "save_as": "check"},
            {"prompt": "Status {check}", "to": target, "llm": False},
        ],
        timezone="UTC",
    )
    User.objects.create(username="lee")
    schedules.run_due([bot], datetime(2026, 10, 4, 9, 0, tzinfo=UTC))
    message = SessionMessage.objects.get()
    assert message.session.metadata["bot_role"] == role
    assert message.role == "assistant"
    assert message.author["kind"] == "system"
    assert '"ready": true' in message.content_blocks.get().text
    assert not ThreadMessage.objects.exists()
    assert ScheduleRun.objects.get().status == "completed"


@pytest.mark.django_db(transaction=True)
def test_workflow_failure_is_audited_and_notifies_main_safely(
    tmp_path, settings, caplog
):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import SessionMessage
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    bot = workflow_bot(
        tmp_path,
        [
            {"python": "workflow.py:broken"},
            {"prompt": "Never"},
        ],
        timezone="UTC",
    )
    User.objects.create(username="lee")
    schedules.run_due([bot], datetime(2026, 10, 4, 9, 0, tzinfo=UTC))
    run = ScheduleRun.objects.get()
    assert run.status == "failed"
    assert run.completed_at is not None
    assert len(run.steps) == 1
    assert run.steps[0]["status"] == "failed"
    assert "RuntimeError" in run.error
    message = SessionMessage.objects.get()
    assert message.session.metadata["bot_role"] == "main"
    assert "Schedule check failed" in message.content_blocks.get().text
    assert "sk-DO-NOT" not in (
        str(run.steps) + run.error + message.content_blocks.get().text + caplog.text
    )
    assert not ThreadMessage.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_workflow_results_redact_credentials_before_audit_and_prompt(
    tmp_path, settings, monkeypatch
):
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import SessionMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}
    monkeypatch.setenv("TEST_API_TOKEN", "secret-from-env")
    bot = workflow_bot(
        tmp_path,
        [
            {"python": "workflow.py:private_result", "save_as": "data"},
            {"prompt": "{data}", "llm": False},
        ],
        timezone="UTC",
    )
    User.objects.create(username="lee")
    schedules.run_due([bot], datetime(2026, 10, 4, 9, 0, tzinfo=UTC))
    result = ScheduleRun.objects.get().steps[0]["result"]
    assert result == {
        "api_key": "[hidden]",
        "message": "[hidden]",
        "capabilities": {
            "maxTokens": 8192,
            "max_tokens": 4096,
            "access_token": "[hidden]",
        },
    }
    assert "do-not-store" not in SessionMessage.objects.get().content_blocks.get().text


def test_schedule_timezone_is_validated():
    from django_ergo.bots.schedules import Schedule

    with pytest.raises(ScheduleError, match="timezone"):
        Schedule.from_config(
            {
                "name": "s",
                "cron": "* * * * *",
                "message": "hi",
                "timezone": "not/a/timezone",
            }
        )


def test_workflow_missing_json_lookup_is_an_error():
    from django_ergo.bots.schedule_workflow import WorkflowError
    from django_ergo.bots.schedule_workflow import condition

    with pytest.raises(WorkflowError, match="missing or incompatible"):
        condition("models.notify", {"models": {}})


@pytest.mark.parametrize(
    "value",
    [
        object(),
        {"not-json": object()},
        {1: "non-string-key"},
        float("nan"),
        float("inf"),
        "x" * 1_000_001,
    ],
)
def test_workflow_results_require_bounded_finite_json(value):
    from django_ergo.bots.schedule_workflow import WorkflowError
    from django_ergo.bots.schedule_workflow import safe_json

    with pytest.raises(WorkflowError):
        safe_json(value)


@pytest.mark.django_db(transaction=True)
def test_catalog_fetch_failure_reaches_main_with_safe_source_details(
    tmp_path, settings, monkeypatch
):
    from django_ergo.bots import model_catalog
    from django_ergo.bots import schedules
    from django_ergo.conversation.models import ScheduleRun
    from django_ergo.conversation.models import SessionMessage
    from django_ergo.conversation.models import ThreadMessage

    settings.DJANGO_ERGO = {"THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message"}

    def unavailable(context):
        provider = "openai"
        raise model_catalog.ModelCatalogError(provider, "official_list")

    monkeypatch.setattr(model_catalog, "check_available", unavailable)
    bot = workflow_bot(
        tmp_path,
        [
            {"python": "ergo.models.check_available", "save_as": "models"},
            {"if": "models.notify"},
            {"prompt": "Never", "to": {"thread": "Never"}},
        ],
        timezone="UTC",
    )
    User.objects.create(username="lee")
    schedules.run_due([bot], datetime(2026, 10, 4, 9, 0, tzinfo=UTC))
    run = ScheduleRun.objects.get()
    assert run.status == "failed"
    assert len(run.steps) == 1
    assert "openai" in run.error and "official_list" in run.error
    notice = SessionMessage.objects.get()
    assert notice.session.metadata["bot_role"] == "main"
    assert run.error in notice.content_blocks.get().text
    assert not ThreadMessage.objects.exists()
