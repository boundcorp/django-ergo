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
    from django_ergo.bots.messaging import turn_text
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    bot, session, manager, kit = setup(tmp_path)
    assert "agents" in {s.name for s in bot._skill_defs()}
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
    assert turn_text(told).startswith("[News from the worker")

    assert (
        kit.execute_tool(
            "ergo_agent_reply",
            {"worker_id": started["id"], "question_id": "q1", "answer": "Blue"},
        )
        == "Sent."
    )
    assert manager.replies == [("job_1", "q1", "Blue")]

    # The same question isn't passed on twice; the report finishes the worker.
    manager.checks = [AgentCheck("done", report="Footer fixed", questions=[question])]
    assert w.run(started["id"], bot.registry) == "completed"
    messages = ThreadMessage.objects.filter(metadata__worker=started["id"])
    assert messages.count() == 2
    assert "Footer fixed" in messages.exclude(pk=told.pk).get().text

    log = w.log(bot, Worker.objects.get(pk=started["id"]))
    assert (log["live"], log["entries"][0]["text"]) == (True, "job_1")


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
