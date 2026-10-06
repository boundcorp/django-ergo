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


@bot_task(requires_approval=True)
def launch(target: str):
    return target


@bot_task
def open_pr():
    return {"pr": "https://github.com/boundcorp/django-ergo/pull/12", "status": "done"}


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
def test_a_worker_s_pull_requests_are_recorded_in_its_chat(tmp_path, workers):
    from django_ergo.bots import workers as w
    from django_ergo.bots.tools import ToolContext

    bot, _ = make_bot(tmp_path, yaml_text=YAML, tools=TOOLS)
    user = get_user_model().objects.create(username="w")
    session = async_to_sync(bot.main_session)(user)
    worker = ToolContext(bot=bot, session=session, user=user).workers.start(
        "open_pr", title="Open the PR"
    )
    assert w.run(str(worker.pk), bot.registry) == "completed"
    [pr] = session.attachments.filter(metadata__link="github_pr")
    assert (pr.url, pr.metadata["number"]) == (
        "https://github.com/boundcorp/django-ergo/pull/12",
        12,
    )


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
    from django_ergo.conversation.adapters import ClaudeToolAdapter

    bot, engine = make_bot(tmp_path, say("ok"), yaml_text=YAML, tools=TOOLS)
    user = get_user_model().objects.create(username="w3")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    skill = next(d for d in bot.skill_defs if d.name == "workers")
    assert skill.always
    kit = skill.toolkits(ctx)[0]

    schema = next(
        s
        for s in kit.get_tools_schema(ClaudeToolAdapter())
        if s["name"] == "ergo_worker_start"
    )
    assert "launch" not in schema["input_schema"]["properties"]["task"]["enum"]
    with pytest.raises(ValueError, match="can't be started here"):
        kit.execute_tool(
            "ergo_worker_start",
            {"task": "launch", "title": "x", "args": {"target": "y"}},
        )
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
def test_orca_start_worker_watches_the_dispatch_and_reports_back(  # noqa: PLR0915
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
        ("terminal", "send"): {"send": {"accepted": True}},
        ("terminal", "show"): {"terminal": {"connected": True, "paneRuntimeId": 7}},
        ("orchestration", "reply"): {"sent": True},
    }

    def respond(args):
        command = tuple(args[1:3])
        if command == ("orchestration", "worker-show"):
            return {
                "dispatch": {"status": state["status"]},
                "worker": {"state": "running", "agent_terminal_handle": "term_agent"},
                "observation": {"status": "alive"},
            }
        if command == ("orchestration", "inbox"):
            return {"messages": state["inbox"]}
        if command == ("orchestration", "worker-read"):
            return state.get("output") or {"terminal": {"tail": []}}
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

    # omp takes its model from the worktree, not from Orca's --model.
    pinned = []
    monkeypatch.setattr(
        type(plugin),
        "push",
        lambda self, root, folder, name, data: pinned.append(
            (root, folder, name, data)
        ),
    )
    plugin.start_worker(
        ctx,
        "Omp task",
        "id:repo::/home/dev/p/site",
        agent="omp",
        model="anthropic/claude-sonnet-5-5",
        effort="high",
    )
    omp_start = [c for c in calls if c[1:3] == ["orchestration", "worker-start"]][-1]
    assert "--model" not in omp_start and "--effort" not in omp_start
    assert pinned == [
        (
            "/home/dev/p/site",
            ".omp",
            "config.yml",
            b'modelRoles:\n  default: "anthropic/claude-sonnet-5-5:high"\n',
        ),
        ("/home/dev/p/site", ".omp", ".gitignore", b"*\n"),
    ]
    with pytest.raises(ValueError, match="omp takes effort only"):
        plugin.start_worker(
            ctx, "x", "id:repo::/home/dev/p/site", agent="omp", effort="high"
        )

    worker_id = started["id"]
    assert w.run(worker_id, bot.registry) == "running"
    # First check: no heartbeat yet, so Enter goes to the agent's terminal, once.
    sends = [c for c in calls if c[1:3] == ["terminal", "send"]]
    assert (
        len(sends) == 1 and sends[0][sends[0].index("--terminal") + 1] == "term_agent"
    )
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
    assert "ergo_agent_reply" in question.text
    from django_ergo.bots.agents import agent_toolkit

    agent_toolkit(bot, ctx).execute_tool(
        "ergo_agent_reply",
        {"worker_id": worker_id, "question_id": "msg_q", "answer": "Blue"},
    )
    assert calls[-1][1:7] == [
        "orchestration",
        "reply",
        "--id",
        "msg_q",
        "--body",
        "Blue",
    ]

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
    assert (
        len([c for c in calls if c[1:3] == ["terminal", "send"]]) == 1
    )  # Enter was pressed only once


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("shown", "expected_worktree"),
    [
        (
            {"worktree": {"id": "repo::/home/dev/p/site"}},
            "id:repo::/home/dev/p/site",
        ),
        (ValueError("worktree show unavailable"), "path:/home/dev/p/site"),
    ],
    ids=["missing-path", "show-error"],
)
def test_orca_start_worker_skips_usage_without_a_worktree_path(
    tmp_path, workers, monkeypatch, shown, expected_worktree
):
    from django_ergo.bots.tools import ToolContext
    from django_ergo.bots.workers import WorkerContext
    from django_ergo.conversation.models import Worker
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path)
    user = get_user_model().objects.create(username="no-usage-path")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    dispatched = []

    def cli_json(args):
        if isinstance(shown, Exception):
            raise shown
        return shown

    def dispatch(*args):
        dispatched.append(args[2])
        return "task_1", "run_1", {"dispatchId": "ctx_1"}

    def reject_scan(*args, **kwargs):
        msg = "usage scan must not run without a worktree path"
        raise AssertionError(msg)

    monkeypatch.setattr(plugin, "cli_json", cli_json)
    monkeypatch.setattr(plugin, "dispatch", dispatch)
    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", reject_scan)

    started = plugin.start_worker(ctx, "Count tokens", "path:/home/dev/p/site")

    assert dispatched == [expected_worktree]
    worker = Worker.objects.get(pk=started["id"])
    assert worker.state == {
        "seen": [],
        "agent": "codex",
        "model": "",
        "effort": "",
        "manager": "orca",
    }
    plugin.scan_usage(WorkerContext(bot, worker), settled=True)


@pytest.mark.django_db(transaction=True)
def test_orca_start_worker_uses_path_encoded_in_id_selector(
    tmp_path, workers, monkeypatch
):
    from unittest.mock import Mock

    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import Worker
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path)
    user = get_user_model().objects.create(username="id-worktree")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    selector = (
        "id:00000000-0000-0000-0000-000000000000::/home/dev/orca/workspaces/repo/task"
    )
    dispatched = []
    cli_json = Mock()

    def dispatch(*args):
        dispatched.append(args[2])
        return "task_1", "run_1", {"dispatchId": "ctx_1"}

    monkeypatch.setattr(plugin, "cli_json", cli_json)
    monkeypatch.setattr(plugin, "dispatch", dispatch)

    started = plugin.start_worker(ctx, "Count tokens", selector)
    cli_json.assert_not_called()

    assert dispatched == [selector]
    worker = Worker.objects.get(pk=started["id"])
    assert worker.function == "agent:orca"
    assert worker.args["handle"]["path"] == "/home/dev/orca/workspaces/repo/task"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "selector",
    [
        "id:00000000-0000-0000-0000-000000000000",
        "id:00000000-0000-0000-0000-000000000000::",
    ],
)
def test_orca_start_worker_skips_usage_for_id_selector_without_path(
    tmp_path, workers, monkeypatch, selector
):
    from unittest.mock import Mock

    from django_ergo.bots.tools import ToolContext
    from django_ergo.bots.workers import WorkerContext
    from django_ergo.conversation.models import Worker
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path)
    user = get_user_model().objects.create(username="id-without-path")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    dispatched = []
    cli_json = Mock()

    def dispatch(*args):
        dispatched.append(args[2])
        return "task_1", "run_1", {"dispatchId": "ctx_1"}

    def reject_scan(*args, **kwargs):
        msg = "usage scan must not run without a worktree path"
        raise AssertionError(msg)

    monkeypatch.setattr(plugin, "cli_json", cli_json)
    monkeypatch.setattr(plugin, "dispatch", dispatch)
    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", reject_scan)

    started = plugin.start_worker(ctx, "Count tokens", selector)
    cli_json.assert_not_called()

    assert dispatched == [selector]
    worker = Worker.objects.get(pk=started["id"])
    assert worker.state == {
        "seen": [],
        "agent": "codex",
        "model": "",
        "effort": "",
        "manager": "orca",
    }
    plugin.scan_usage(WorkerContext(bot, worker), settled=True)


@pytest.mark.django_db(transaction=True)
def test_orca_start_worker_replaces_a_mailbox_whose_terminal_is_gone(
    tmp_path, workers, monkeypatch
):
    import subprocess

    from django_ergo.bots.tools import ToolContext
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path)
    calls = []
    fixed = {
        ("terminal", "create"): {"terminal": {"handle": "term_new"}},
        ("orchestration", "run-create"): {"run": {"id": "run_new"}},
        ("worktree", "show"): {
            "worktree": {"id": "repo::/home/dev/p/site", "path": "/home/dev/p/site"}
        },
        ("orchestration", "task-create"): {"task": {"id": "task_1"}},
        ("orchestration", "worker-start"): {"dispatchId": "ctx_1", "taskId": "task_1"},
    }

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["orchestration", "worker-start"] and "term_gone" in argv:
            body = {"ok": False, "error": {"message": "selector_not_found"}}
            return subprocess.CompletedProcess(
                argv, 1, stdout=json.dumps(body), stderr=""
            )
        body = {"ok": True, "result": fixed[tuple(argv[1:3])]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    user = get_user_model().objects.create(username="stale")
    session = async_to_sync(bot.main_session)(user)
    # The mailbox lived in a worktree that has since been removed.
    session.metadata = {
        **session.metadata,
        "orca": {"devbox": {"mailbox": "term_gone", "run": "run_old"}},
    }
    session.save(update_fields=["metadata"])
    ctx = ToolContext(bot=bot, session=session, user=user)

    started = plugin.start_worker(ctx, "Fix it", "path:/home/dev/p/site")
    assert started["orca"]["run"] == "run_new"
    starts = [c for c in calls if c[1:3] == ["orchestration", "worker-start"]]
    assert [c[c.index("--from") + 1] for c in starts] == ["term_gone", "term_new"]
    session.refresh_from_db()
    assert session.metadata["orca"]["devbox"] == {
        "mailbox": "term_new",
        "run": "run_new",
    }

    # Any other failure is reported as it is, without a retry.
    fixed[("orchestration", "worker-start")] = {}
    with pytest.raises(ValueError, match="no dispatch id"):
        plugin.start_worker(ctx, "Again", "path:/home/dev/p/site")


@pytest.mark.django_db(transaction=True)
def test_orca_watch_fails_a_worker_whose_agent_terminal_is_gone(
    tmp_path, workers, monkeypatch
):
    import subprocess
    from datetime import timedelta

    from django.utils import timezone

    from django_ergo.bots import workers as w
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import Worker
    from django_ergo.plugins import orca
    from tests.test_bot_plugins import orca_bot

    bot, _, plugin = orca_bot(tmp_path)
    terminal = {"show": {"terminal": {"connected": True, "paneRuntimeId": 7}}}
    fixed = {
        ("terminal", "create"): {"terminal": {"handle": "term_mail"}},
        ("orchestration", "run-create"): {"run": {"id": "run_1"}},
        ("worktree", "show"): {
            "worktree": {"id": "repo::/home/dev/p/site", "path": "/home/dev/p/site"}
        },
        ("orchestration", "task-create"): {"task": {"id": "task_1"}},
        ("orchestration", "worker-start"): {"dispatchId": "ctx_1", "taskId": "task_1"},
        ("orchestration", "inbox"): {"messages": []},
        ("terminal", "send"): {"send": {"accepted": True}},
        ("orchestration", "worker-show"): {
            "dispatch": {"status": "dispatched", "last_heartbeat_at": "x"},
            "worker": {"state": "ready", "agent_terminal_handle": "term_agent"},
        },
    }

    def fake_run(argv, **kwargs):
        if argv[1:3] == ["terminal", "show"]:
            body = terminal["show"]
            code = 0 if body.get("ok", True) else 1
            payload = body if "ok" in body else {"ok": True, "result": body}
            return subprocess.CompletedProcess(
                argv, code, stdout=json.dumps(payload), stderr=""
            )
        body = {"ok": True, "result": fixed[tuple(argv[1:3])]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    user = get_user_model().objects.create(username="gone")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    worker_id = plugin.start_worker(ctx, "Count files", "path:/home/dev/p/site")["id"]

    assert w.run(worker_id, bot.registry) == "running"  # terminal live

    # The agent's terminal exits (Orca still says "dispatched")...
    terminal["show"] = {
        "terminal": {
            "connected": False,
            "paneRuntimeId": -1,
            "preview": "zsh: warning: 1 jobs SIGHUPed",
        }
    }
    assert w.run(worker_id, bot.registry) == "running"  # ...first seen: wait
    row = Worker.objects.get(id=worker_id)
    assert "terminal_gone_since" in row.state
    assert "agent terminal exited" in row.progress

    # ...and if it comes back before the grace period ends, nothing happens.
    terminal["show"] = {"terminal": {"connected": True, "paneRuntimeId": 7}}
    assert w.run(worker_id, bot.registry) == "running"
    assert "terminal_gone_since" not in Worker.objects.get(id=worker_id).state

    # Gone for good: once the grace period passes, the worker fails with the reason.
    terminal["show"] = {"ok": False, "error": {"message": "terminal_handle_stale"}}
    assert w.run(worker_id, bot.registry) == "running"
    row = Worker.objects.get(id=worker_id)
    past = timezone.now() - timedelta(seconds=orca.TERMINAL_GONE_GRACE_SECONDS + 1)
    row.state = {**row.state, "terminal_gone_since": past.isoformat()}
    row.save(update_fields=["state"])
    assert w.run(worker_id, bot.registry) == "failed"
    row.refresh_from_db()
    assert "agent terminal gone without reporting done" in row.error


def test_orca_worker_output_reads_transcripts_and_screens():
    from django_ergo.plugins.orca import output_entries

    source, entries = output_entries(
        {
            "source": "transcript",
            "transcript": {
                "messages": [
                    {
                        "role": "assistant",
                        "timestamp": 1_790_000_000_000,
                        "blocks": [
                            {"type": "text", "text": "Running the tests."},
                            {
                                "type": "tool-call",
                                "name": "Bash",
                                "input": {"command": "pytest -q", "timeout": 60},
                            },
                        ],
                    },
                    {
                        "role": "tool",
                        "timestamp": None,
                        "blocks": [
                            {
                                "type": "tool-result",
                                "output": "2 failed",
                                "isError": True,
                            }
                        ],
                    },
                ]
            },
        }
    )
    assert source == "transcript"
    assert entries == [
        {"kind": "assistant", "text": "Running the tests.", "at": 1_790_000_000.0},
        {"kind": "tool", "text": "Bash pytest -q", "at": 1_790_000_000.0},
        {"kind": "error", "text": "2 failed", "at": None},
    ]
    # Older hosts and agents with no transcript give the screen, with its colors.
    source, entries = output_entries(
        {"terminal": {"tail": ["\x1b[1;32m✓ built\x1b[0m", "", "  waiting  "]}}
    )
    assert source == "terminal"
    assert [e["text"] for e in entries] == ["✓ built", "waiting"]


@pytest.mark.django_db(transaction=True)
def test_orca_watcher_keeps_the_agents_activity_and_reads_its_log(
    tmp_path, workers, monkeypatch
):
    import subprocess

    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import Worker
    from tests.test_bot_plugins import orca_bot

    bot, _, _ = orca_bot(tmp_path, config="environment: devbox, stall_minutes: 5")
    clock = {"now": 1_790_000_000.0}
    monkeypatch.setattr("django_ergo.plugins.orca.time.time", lambda: clock["now"])
    screen = {"tail": ["Reading src/app.py"]}
    reads = []

    def fake_run(argv, **kwargs):
        command = tuple(argv[1:3])
        if command == ("orchestration", "worker-show"):
            result = {
                "dispatch": {"status": "dispatched", "last_heartbeat_at": None},
                "worker": {"state": "running"},
                "observation": {"agentWait": None},
                "projection": {"liveness": {"verdict": "live"}},
            }
        elif command == ("orchestration", "inbox"):
            result = {"messages": []}
        elif command == ("orchestration", "worker-read"):
            reads.append(argv)
            result = {"source": "terminal", "terminal": dict(screen)}
        else:
            result = {}
        body = {"ok": True, "result": result}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", fake_run)
    user = get_user_model().objects.create(username="watcher")
    session = async_to_sync(bot.main_session)(user)
    worker = Worker.objects.create(
        session=session,
        bot_name=bot.name,
        title="codex: Fix footer",
        function="orca:watch",
        args={"dispatch": "ctx_1", "run": "run_1"},
        state={"task": "task_1", "seen": [], "nudged": True},
    )

    assert w.run(str(worker.pk), bot.registry) == "running"
    worker.refresh_from_db()
    shown = w.activity(worker)
    assert shown["entries"] == [
        {"kind": "terminal", "text": "Reading src/app.py", "at": None}
    ]
    assert (shown["at"], shown["stall_after"], shown["liveness"]) == (
        clock["now"],
        300,
        "live",
    )
    started = clock["now"]

    # Ten minutes on with the same screen: the last activity stays where it was.
    clock["now"] += 600
    assert w.run(str(worker.pk), bot.registry) == "running"
    worker.refresh_from_db()
    assert w.activity(worker)["at"] == started
    assert w.activity(worker)["checked_at"] == clock["now"]

    # New output moves it on.
    screen["tail"] = ["Reading src/app.py", "Editing footer.html"]
    clock["now"] += 120
    w.run(str(worker.pk), bot.registry)
    worker.refresh_from_db()
    assert w.activity(worker)["at"] == clock["now"]
    assert [e["text"] for e in w.activity(worker)["entries"]][
        -1
    ] == "Editing footer.html"

    # The full log is read now, with a bigger limit.
    log = w.log(bot, worker)
    assert log["live"] and log["source"] == "terminal" and len(log["entries"]) == 2
    assert reads[-1][reads[-1].index("--limit") + 1] == "400"

    # When Orca can't be reached, the log is what the last check kept.
    def broken(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no orca")

    monkeypatch.setattr("django_ergo.plugins.orca.subprocess.run", broken)
    log = w.log(bot, worker)
    assert not log["live"] and "no orca" in log["error"]
    assert [e["text"] for e in log["entries"]][-1] == "Editing footer.html"
