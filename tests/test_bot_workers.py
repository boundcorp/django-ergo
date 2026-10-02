from __future__ import annotations

import json

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from tests.test_bots import make_bot
from tests.test_bots import say

TOOLS = """
from django_ergo.bots import bot_task, bot_tool


@bot_task
def watch(ctx, target: str, polls: int = 2):
    seen = ctx.state.get("seen", 0) + 1
    ctx.state["seen"] = seen
    ctx.progress(f"checked {target} {seen} times")
    if seen < polls:
        return ctx.again(5, progress=f"waiting on {target}")
    return {"target": target, "checks": seen}


@bot_task
def explode():
    raise RuntimeError("the build broke")


@bot_tool
def pantry_count(item: str) -> int:
    return 4
"""

YAML = """
    name: kitchen
    tools: [tools/pantry.py]
"""

STEPS: list[tuple[str, float]] = []


def record_step(worker_id, delay):
    """A WORKER_RUNNER for tests: run steps by hand."""
    STEPS.append((worker_id, delay))


@pytest.fixture
def workers(settings):
    from django.conf import settings as django_settings

    STEPS.clear()
    settings.DJANGO_ERGO = {
        **getattr(django_settings, "DJANGO_ERGO", {}),
        "WORKER_RUNNER": "tests.test_bot_workers.record_step",
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
    }
    return STEPS


@pytest.mark.django_db(transaction=True)
def test_a_polling_worker_reports_back_to_its_thread(tmp_path, workers):
    from django_ergo.bots import workers as w
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, _ = make_bot(tmp_path, yaml_text=YAML, tools=TOOLS)
    user = get_user_model().objects.create(username="w")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)

    worker = ctx.workers.start(
        "watch", title="Watch the deploy", target="deploy", polls=3
    )
    assert (worker.function, worker.status) == ("task:watch", "queued")
    assert workers == [(str(worker.pk), 0)]

    assert w.run(str(worker.pk), bot.registry) == "running"
    worker.refresh_from_db()
    assert (worker.state, worker.progress, worker.polls) == (
        {"seen": 1},
        "waiting on deploy",
        1,
    )
    assert workers[-1] == (str(worker.pk), 5)
    assert w.run(str(worker.pk), bot.registry) == "running"
    assert w.run(str(worker.pk), bot.registry) == "completed"
    worker.refresh_from_db()
    assert worker.result == {"target": "deploy", "checks": 3}

    # The result comes back to the chat as a message, which starts a follow-up turn.
    message = ThreadMessage.objects.get(metadata__worker=str(worker.pk))
    assert message.recipient_session_id == session.pk
    assert '"checks": 3' in message.text
    from django_ergo.bots.messaging import turn_text

    assert turn_text(message).startswith("[A worker this chat started has finished.")

    # Finished workers don't run again.
    assert w.run(str(worker.pk), bot.registry) == "completed"
    assert Worker.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_failed_and_cancelled_workers(tmp_path, workers):
    from django_ergo.bots import workers as w
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ThreadMessage

    bot, _ = make_bot(tmp_path, yaml_text=YAML, tools=TOOLS)
    user = get_user_model().objects.create(username="w2")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)

    broken = ctx.workers.start("explode", title="Build")
    assert w.run(str(broken.pk), bot.registry) == "failed"
    broken.refresh_from_db()
    assert "the build broke" in broken.error
    assert (
        "failed: RuntimeError: the build broke"
        in ThreadMessage.objects.get(metadata__worker=str(broken.pk)).text
    )

    quiet = ctx.workers.start("watch", title="Watch", notify=False, target="x", polls=5)
    w.run(str(quiet.pk), bot.registry)
    assert w.cancel(quiet).startswith("Cancelling")
    assert w.run(str(quiet.pk), bot.registry) == "cancelled"
    assert not ThreadMessage.objects.filter(metadata__worker=str(quiet.pk)).exists()

    with pytest.raises(LookupError, match="no task 'nope'"):
        ctx.workers.start("nope")
    with pytest.raises(TypeError):
        ctx.workers.start("watch", target=object())


@pytest.mark.django_db(transaction=True)
def test_the_workers_skill_lists_starts_and_cancels(tmp_path, workers):
    from django_ergo.bots.tools import ToolContext

    bot, engine = make_bot(tmp_path, say("ok"), yaml_text=YAML, tools=TOOLS)
    user = get_user_model().objects.create(username="w3")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    skill = next(d for d in bot.skill_defs if d.name == "workers")
    assert skill.always
    kit = skill.toolkits(ctx)[0]

    started = json.loads(
        kit.execute_tool(
            "ergo_worker_start",
            {"task": "watch", "title": "Watch it", "args": {"target": "site"}},
        )
    )
    assert (started["title"], started["status"]) == ("Watch it", "queued")
    listed = json.loads(kit.execute_tool("ergo_worker_list", {"active_only": True}))
    assert [x["id"] for x in listed] == [started["id"]]
    assert kit.execute_tool(
        "ergo_worker_cancel", {"worker_id": started["id"]}
    ).startswith("Cancelling")
    with pytest.raises(ValueError, match="No worker"):
        kit.execute_tool(
            "ergo_worker_cancel", {"worker_id": "00000000-0000-0000-0000-000000000000"}
        )


@pytest.mark.django_db(transaction=True)
def test_orca_start_worker_watches_the_dispatch_and_reports_back(
    tmp_path, workers, monkeypatch
):
    import subprocess

    from django_ergo.bots import workers as w
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ThreadMessage
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path)
    calls = []
    state = {"status": "dispatched", "inbox": []}

    fixed = {
        ("terminal", "create"): {"terminal": {"handle": "term_mail"}},
        ("orchestration", "run-create"): {"run": {"id": "run_1"}},
        ("worktree", "show"): {
            "worktree": {"id": "repo::/home/dev/p/site", "path": "/home/dev/p/site"}
        },
        ("orchestration", "task-create"): {"task": {"id": "task_1"}},
        ("orchestration", "worker-start"): {"dispatchId": "ctx_1", "taskId": "task_1"},
    }

    def respond(args):
        command = tuple(args[1:3])
        if command == ("orchestration", "worker-show"):
            return {
                "dispatch": {"status": state["status"]},
                "worker": {"state": "running"},
                "observation": {"status": "alive"},
            }
        if command == ("orchestration", "inbox"):
            return {"messages": state["inbox"]}
        return fixed[command]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"ok": True, "result": respond(argv)}), stderr=""
        )

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    user = get_user_model().objects.create(username="orc")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    assert "orca_start_worker" in [t.name for t in plugin._tools()]
    assert next(
        t for t in plugin._tools() if t.name == "orca_start_worker"
    ).requires_approval

    started = plugin.start_worker(
        ctx,
        "Fix the footer.\nAcceptance: tests pass.",
        "path:/home/dev/p/site",
        title="Fix footer",
    )
    assert started["orca"] == {
        "run": "run_1",
        "task": "task_1",
        "dispatch": "ctx_1",
        "worktree": "id:repo::/home/dev/p/site",
    }
    worker_start = next(c for c in calls if c[1:3] == ["orchestration", "worker-start"])
    assert worker_start[worker_start.index("--from") + 1] == "term_mail"
    assert (
        worker_start[worker_start.index("--worktree") + 1]
        == "id:repo::/home/dev/p/site"
    )
    session.refresh_from_db()
    assert session.metadata["orca"]["devbox"] == {
        "mailbox": "term_mail",
        "run": "run_1",
    }

    # A second worker in the same chat reuses its mailbox and Run.
    plugin.start_worker(ctx, "Another task", "id:repo::/home/dev/p/site")
    assert sum(c[1:3] == ["terminal", "create"] for c in calls) == 1

    worker_id = started["id"]
    assert w.run(worker_id, bot.registry) == "running"
    state["inbox"] = [
        {
            "id": "msg_q",
            "run_id": "run_1",
            "type": "question",
            "subject": "Which color?",
            "body": "Blue or red?",
            "payload": json.dumps({"dispatchId": "ctx_1"}),
            "sequence": 1,
        },
        {
            "id": "msg_other",
            "run_id": "run_1",
            "type": "worker_done",
            "body": "someone else's",
            "payload": json.dumps({"dispatchId": "ctx_9"}),
            "sequence": 2,
        },
    ]
    assert w.run(worker_id, bot.registry) == "running"

    def sent(update):
        return [
            m
            for m in ThreadMessage.objects.filter(metadata__worker=worker_id)
            if bool(m.metadata.get("update")) is update
        ]

    [question] = sent(True)
    assert "Blue or red?" in question.text and "msg_q" in question.text
    from django_ergo.bots.messaging import turn_text

    assert turn_text(question).startswith("[News from the worker")

    state["status"] = "completed"
    state["inbox"].append(
        {
            "id": "msg_done",
            "run_id": "run_1",
            "type": "worker_done",
            "subject": "Footer fixed",
            "body": "Done; tests pass.",
            "payload": json.dumps({"dispatchId": "ctx_1"}),
            "sequence": 3,
        }
    )
    assert w.run(worker_id, bot.registry) == "completed"
    [done] = sent(False)
    assert "Done; tests pass." in done.text and "Footer fixed" in done.text
    assert len(sent(True)) == 1  # the question was passed on once
