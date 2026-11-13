from __future__ import annotations

import json

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots.agents import AgentCheck
from django_ergo.bots.agents import AgentManager
from django_ergo.bots.agents import AgentQuestion
from django_ergo.bots.plugins import BotPlugin
from tests.test_bot_workers import workers  # noqa: F401 — fixture
from tests.test_bots import make_bot

YAML = """
    name: shop
    plugins: [{name: "tests.test_bot_agents:FakeAgentsPlugin"}]
"""


class FakeAgents(AgentManager):
    """Agents in a dict: tests set ``checks`` to what each look returns."""

    agents = ("codex", "claude")
    requires_approval = False
    poll_seconds = 7
    where = "a test box"

    def __init__(self):
        self.started: list = []
        self.checks: list[AgentCheck] = []
        self.replies: list = []
        self.stopped: list = []

    def start(self, ctx, spec):
        self.started.append(spec)
        return {"job": f"job_{len(self.started)}"}

    def check(self, ctx, handle):
        return self.checks.pop(0) if self.checks else AgentCheck(progress="busy")

    def reply(self, ctx, handle, question_id, text):
        self.replies.append((handle["job"], question_id, text))
        return "Sent."

    def stop(self, handle):
        self.stopped.append(handle["job"])
        return "Stopped it"

    def log(self, worker, handle):
        return {"source": "test", "entries": [{"kind": "text", "text": handle["job"]}]}


class FakeAgentsPlugin(BotPlugin):
    name = "fake"

    def on_load(self):
        self.manager = FakeAgents()

    def agent_managers(self):
        return {"box": self.manager}


def start(kit, args: dict) -> dict:
    return json.loads(kit.execute_tool("ergo_agent_start", args))


def asked(worker_id: str, *question_ids: str) -> None:
    """As if the watcher had passed these questions on."""
    from django_ergo.conversation.models import Worker

    worker = Worker.objects.get(pk=worker_id)
    worker.state = {**worker.state, "seen": list(question_ids)}
    worker.save(update_fields=["state"])


def setup(tmp_path):
    from django_ergo.bots.agents import agent_toolkit
    from django_ergo.bots.tools import ToolContext

    bot, _ = make_bot(tmp_path, yaml_text=YAML, name="shop")
    user = get_user_model().objects.create(username="agents")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    return bot, session, bot.plugin("fake").manager, agent_toolkit(bot, ctx)


@pytest.mark.django_db(transaction=True)
def test_an_agent_asks_gets_an_answer_and_reports_back(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, session, manager, kit = setup(tmp_path)
    defs = {s.name: s for s in bot._skill_defs()}
    # Starting an agent brings the list of tiers it can pick from.
    assert "models" in defs["agents"].requires
    from django_ergo.bots.model_choice import models_toolkit
    from django_ergo.bots.tools import ToolContext

    ctx = ToolContext(bot=bot, session=session)
    assert models_toolkit(bot, ctx).has_tool("ergo_models_list")
    assert all(
        kit.has_tool(name)
        for name in ("ergo_agent_start", "ergo_agent_reply", "ergo_agent_stop")
    )

    started = start(
        kit,
        {
            "brief": "Fix the footer.\nTests pass.",
            "workspace": "site",
            "agent": "claude",
        },
    )
    worker = Worker.objects.get(pk=started["id"])
    assert (worker.function, worker.title) == ("agent:box", "claude: Fix the footer.")
    assert worker.args == {"handle": {"job": "job_1"}}
    assert worker.state["manager"] == "box"
    assert manager.started[0].workspace == "site"

    question = AgentQuestion(id="q1", subject="Color?", body="Blue or red?")
    manager.checks = [AgentCheck(progress="thinking", questions=[question])]
    assert w.run(started["id"], bot.registry) == "running"
    assert workers[-1] == (started["id"], 7)
    worker.refresh_from_db()
    assert worker.progress == "thinking"
    [told] = ThreadMessage.objects.filter(metadata__worker=started["id"])
    assert "Blue or red?" in told.text and "q1" in told.text

    answer = {"worker_id": started["id"], "question_id": "q1", "answer": "Blue"}
    assert (
        kit.execute_tool("ergo_agent_reply", answer) == "Answer to question q1 queued."
    )
    assert manager.replies == []
    assert "already queued" in kit.execute_tool("ergo_agent_reply", answer)
    with pytest.raises(ValueError, match="q2 isn't a question this agent asked"):
        kit.execute_tool("ergo_agent_reply", {**answer, "question_id": "q2"})

    # The watcher delivers the answer and the final report.
    manager.checks = [AgentCheck("done", report="Footer fixed", questions=[question])]
    assert w.run(started["id"], bot.registry) == "completed"
    assert manager.replies == [("job_1", "q1", "Blue")]
    messages = ThreadMessage.objects.filter(metadata__worker=started["id"])
    assert messages.count() == 3
    assert any("Footer fixed" in message.text for message in messages)
    assert any("Sent." in message.text for message in messages)

    log = w.log(bot, Worker.objects.get(pk=started["id"]))
    assert (log["live"], log["entries"][0]["text"]) == (True, "job_1")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("workers")
def test_cancel_during_terminal_agent_check_never_reports_success(
    tmp_path, monkeypatch
):
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Prepare an artifact", "workspace": "site"})

    def check(ctx, handle):
        w.cancel(ctx.worker)
        return AgentCheck(
            "done",
            progress="Needs a new run",
            report={"status": "needs_new_run", "outputs": ["artifact.txt"]},
        )

    monkeypatch.setattr(manager, "check", check)
    assert w.run(started["id"], bot.registry) == "cancelled"
    worker = Worker.objects.get(pk=started["id"])
    assert worker.result is None
    assert manager.stopped == ["job_1"]
    assert manager.replies == []
    assert not ThreadMessage.objects.filter(metadata__worker=started["id"]).exists()


@pytest.mark.django_db(transaction=True)
def test_slow_reply_does_not_block_tool_or_duplicate(tmp_path, workers):  # noqa: F811
    import threading

    from django.db import close_old_connections

    from django_ergo.bots import workers as w

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    entered = threading.Event()
    release = threading.Event()
    calls = []
    outcomes = []

    def slow(ctx, handle, question_id, text):
        calls.append(question_id)
        entered.set()
        if not release.wait(5):
            msg = "test did not release reply"
            raise RuntimeError(msg)
        return "Resumed"

    def run_watcher():
        try:
            outcomes.append(w.run(started["id"], bot.registry))
        finally:
            close_old_connections()

    manager.reply = slow
    asked(started["id"], "q")
    answer = {"worker_id": started["id"], "question_id": "q", "answer": "yes"}
    assert "queued" in kit.execute_tool("ergo_agent_reply", answer)
    assert not entered.is_set()
    thread = threading.Thread(target=run_watcher)
    thread.start()
    try:
        assert entered.wait(5)
        assert "already queued" in kit.execute_tool("ergo_agent_reply", answer)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert outcomes == ["running"]
    assert calls == ["q"]


@pytest.mark.django_db(transaction=True)
def test_reply_failure_is_reported_and_not_retried(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    calls = []

    def fail(ctx, handle, question_id, text):
        calls.append(question_id)
        msg = "resume failed"
        raise RuntimeError(msg)

    manager.reply = fail
    asked(started["id"], "q")
    answer = {"worker_id": started["id"], "question_id": "q", "answer": "yes"}
    assert "queued" in kit.execute_tool("ergo_agent_reply", answer)
    manager.checks = [AgentCheck("done", report="Final report")]
    assert w.run(started["id"], bot.registry) == "completed"
    assert calls == ["q"]
    with pytest.raises(ValueError, match="no longer running"):
        kit.execute_tool("ergo_agent_reply", answer)
    texts = list(
        ThreadMessage.objects.filter(metadata__worker=started["id"]).values_list(
            "text", flat=True
        )
    )
    assert any("resume failed" in text for text in texts)
    assert any("Final report" in text for text in texts)


@pytest.mark.django_db(transaction=True)
def test_interrupted_reply_is_reported_without_redelivery(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    worker = Worker.objects.get(pk=started["id"])
    worker.state["seen"] = ["q"]
    worker.args["_agent_replies"] = {"q": {"answer": "yes", "status": "processing"}}
    worker.save(update_fields=["args", "state"])
    question = AgentQuestion(id="next", body="Which option next?")
    manager.checks = [
        AgentCheck(progress="first observation", questions=[question]),
        AgentCheck(progress="second observation", questions=[question]),
    ]
    assert w.run(started["id"], bot.registry) == "running"
    assert w.run(started["id"], bot.registry) == "running"
    assert manager.replies == []
    assert manager.checks == []  # Observation/reconciliation still ran both times.
    worker.refresh_from_db()
    assert worker.progress == "second observation"
    assert worker.state["seen"] == ["q"]
    with pytest.raises(ValueError, match="isn't a question this agent asked"):
        kit.execute_tool(
            "ergo_agent_reply",
            {"worker_id": started["id"], "question_id": "next", "answer": "yes"},
        )
    texts = list(
        ThreadMessage.objects.filter(metadata__worker=started["id"]).values_list(
            "text", flat=True
        )
    )
    assert len(texts) == 1
    assert "unknown" in texts[0] and "interrupted" in texts[0]
    # Reconciliation can resolve the old delivery; only then expose the next one.
    worker.args["_agent_replies"]["q"].update(status="sent", result="Reconciled")
    worker.save(update_fields=["args"])
    manager.checks = [AgentCheck(questions=[question])]
    assert w.run(started["id"], bot.registry) == "running"
    worker.refresh_from_db()
    assert worker.state["seen"] == ["next", "q"]
    messages = ThreadMessage.objects.filter(metadata__worker=started["id"])
    assert messages.count() == 3
    assert "Reconciled" in messages[1].text
    assert "Which option next?" in messages[2].text


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("workers")
@pytest.mark.parametrize("status", ["done", "failed"])
def test_pending_reply_still_observes_terminal_status(tmp_path, status):
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    worker = Worker.objects.get(pk=started["id"])
    worker.state["seen"] = ["q"]
    worker.args["_agent_replies"] = {"q": {"answer": "yes", "status": "processing"}}
    worker.save(update_fields=["args", "state"])
    manager.checks = [
        AgentCheck(
            status,
            report="Final report",
            error="Final failure",
            questions=[AgentQuestion(id="next", body="Unsafe new question")],
        )
    ]
    assert w.run(started["id"], bot.registry) == (
        "completed" if status == "done" else "failed"
    )
    assert manager.checks == []
    assert manager.replies == []
    worker.refresh_from_db()
    assert worker.state["seen"] == ["q"]
    assert not ThreadMessage.objects.filter(
        metadata__worker=started["id"], text__contains="Unsafe new question"
    ).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("workers")
def test_reply_queue_rejects_observed_terminal_mail_fence(tmp_path):
    from django_ergo.conversation.models import Worker

    _, _, _, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    asked(started["id"], "q")
    worker = Worker.objects.get(pk=started["id"])
    worker.args["_agent_mail_closed"] = "Remote completion observed"
    worker.save(update_fields=["args"])
    assert worker.active
    with pytest.raises(ValueError, match="no longer running"):
        kit.execute_tool(
            "ergo_agent_reply",
            {"worker_id": started["id"], "question_id": "q", "answer": "yes"},
        )
    worker.refresh_from_db()
    assert "_agent_replies" not in worker.args


@pytest.mark.django_db(transaction=True)
def test_answer_queued_during_final_check_is_closed_without_delivery(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    asked(started["id"], "q")

    def check(ctx, handle):
        kit.execute_tool(
            "ergo_agent_reply",
            {"worker_id": started["id"], "question_id": "q", "answer": "yes"},
        )
        return AgentCheck("done", report="Final report")

    manager.check = check
    assert w.run(started["id"], bot.registry) == "completed"
    assert manager.replies == []
    worker = Worker.objects.get(pk=started["id"])
    assert worker.args["_agent_replies"]["q"]["status"] == "unsent"
    with pytest.raises(ValueError, match="no longer running"):
        kit.execute_tool(
            "ergo_agent_reply",
            {"worker_id": started["id"], "question_id": "q", "answer": "yes"},
        )
    assert any(
        "Final report" in text
        for text in ThreadMessage.objects.filter(
            metadata__worker=started["id"]
        ).values_list("text", flat=True)
    )


@pytest.mark.django_db(transaction=True)
def test_a_failed_or_stopped_agent(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.bots.workers import worker_toolkit
    from django_ergo.conversation.models import Worker

    bot, session, manager, kit = setup(tmp_path)
    with pytest.raises(ValueError, match="box runs codex, claude, not 'omp'"):
        kit.execute_tool(
            "ergo_agent_start", {"brief": "x", "workspace": "site", "agent": "omp"}
        )

    failing = start(kit, {"brief": "Break", "workspace": "site"})
    assert manager.started[-1].agent == "codex"  # the manager's default
    manager.checks = [AgentCheck("failed", error="tests are red")]
    assert w.run(failing["id"], bot.registry) == "failed"
    assert "tests are red" in Worker.objects.get(pk=failing["id"]).error

    stopped = start(kit, {"brief": "Stop me", "workspace": "site"})
    said = kit.execute_tool("ergo_agent_stop", {"worker_id": stopped["id"]})
    assert said.startswith("Cancelled") and "Stopped it" in said
    assert manager.stopped == ["job_2"]
    assert Worker.objects.get(pk=stopped["id"]).status == "cancelled"

    # Cancelling it as a plain worker stops the agent too.
    other = start(kit, {"brief": "And me", "workspace": "site"})
    from django_ergo.bots.tools import ToolContext

    ctx = ToolContext(bot=bot, session=session, user=session.user)
    worker_toolkit(bot, ctx).execute_tool(
        "ergo_worker_cancel", {"worker_id": other["id"]}
    )
    assert manager.stopped == ["job_2", "job_3"]

    with pytest.raises(ValueError, match="No agent worker"):
        kit.execute_tool(
            "ergo_agent_reply",
            {
                "worker_id": "00000000-0000-0000-0000-000000000000",
                "question_id": "q",
                "answer": "a",
            },
        )


@pytest.mark.django_db
def test_a_bot_without_agent_managers_has_no_agents_skill(tmp_path):
    from tests.test_bot_workers import YAML as PLAIN

    bot, _ = make_bot(tmp_path, yaml_text=PLAIN)
    assert "agents" not in {s.name for s in bot._skill_defs()}


@pytest.mark.django_db(transaction=True)
def test_a_reply_to_a_question_never_asked_names_the_open_one(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w

    bot, _, manager, kit = setup(tmp_path)
    started = start(kit, {"brief": "Fix", "workspace": "site"})
    reply = {"worker_id": started["id"], "question_id": "job:1", "answer": "Go"}
    with pytest.raises(ValueError, match="no unanswered question"):
        kit.execute_tool("ergo_agent_reply", reply)

    question = AgentQuestion(id="job:2", body="Which option?")
    manager.checks = [AgentCheck(progress="waiting", questions=[question])]
    assert w.run(started["id"], bot.registry) == "running"
    with pytest.raises(ValueError, match="Its open question is job:2"):
        kit.execute_tool("ergo_agent_reply", reply)
    assert "queued" in kit.execute_tool(
        "ergo_agent_reply", {**reply, "question_id": "job:2"}
    )
