from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots import agents
from django_ergo.bots import workers as runner
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import ToolContext
from django_ergo.conversation.models import Worker
from django_ergo.plugins import orca_instructions as protocol
from tests.test_bot_plugins import orca_bot
from tests.test_bot_workers import workers  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def instruction_worker(tmp_path, monkeypatch, workers):  # noqa: F811
    bot, _, plugin = orca_bot(tmp_path, config="approve_changes: false")
    user = get_user_model().objects.create(username="instruction-owner")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    handle = {
        "run": "run_1",
        "task": "task_1",
        "dispatch": "ctx_1",
        "mailbox": "term_owner",
    }
    monkeypatch.setattr(plugin.manager, "start", lambda ctx, spec: dict(handle))
    worker = agents.start(ctx, agents.AgentSpec("Fix the footer", "site"), "orca")
    state = {"messages": [], "sends": [], "timeout": False}
    shown = {
        "dispatch": {
            "status": "dispatched",
            "assignee_handle": "term_worker",
            "assignee_pane_key": "pane_1",
        },
        "worker": {"state": "running"},
    }

    def cli(args):
        command = tuple(args[:2])
        if command == ("orchestration", "send"):
            state["sends"].append(args)
            if state["timeout"]:
                raise subprocess.TimeoutExpired(args, 1)
            return {"message": {"id": "msg_out", "read": 0, "delivered_at": None}}
        if command == ("orchestration", "worker-show"):
            return shown
        if command == ("orchestration", "inbox"):
            return {"messages": state["messages"]}
        msg = f"Unexpected command: {args}"
        raise AssertionError(msg)

    monkeypatch.setattr(plugin, "cli_json", cli)
    for name in (
        "record_activity",
        "scan_usage",
        "fail_if_terminal_gone",
        "submit_brief_once",
    ):
        monkeypatch.setattr(plugin, name, lambda *args, **kwargs: None)
    kit = FunctionToolkit(plugin._tools(), ctx)
    return SimpleNamespace(
        bot=bot,
        plugin=plugin,
        worker=worker,
        handle=handle,
        ctx=ctx,
        kit=kit,
        state=state,
        shown=shown,
    )


def queue(
    env,
    instruction_id="i1",
    text="Stop before any external side effect",
    supersedes="",
    priority="urgent",
):
    return json.loads(
        env.kit.execute_tool(
            "ergo_orca_instruct",
            {
                "worker_id": str(env.worker.pk),
                "instruction_id": instruction_id,
                "text": text,
                "supersedes": supersedes,
                "priority": priority,
            },
        )
    )


def status(env):
    return json.loads(
        env.kit.execute_tool(
            "ergo_orca_instruction_status", {"worker_id": str(env.worker.pk)}
        )
    )


def ack(
    env,
    instruction_id="i1",
    version=1,
    action="Stop edits; inspect only the authorized cleanup scope",
    **overrides,
):
    return {
        "id": f"ack_{instruction_id}",
        "run_id": "run_1",
        "from_handle": "term_worker",
        "sender_pane_key": "pane_1",
        "type": "status",
        "sequence": version,
        "payload": json.dumps({"taskId": "task_1", "dispatchId": "ctx_1"}),
        "body": json.dumps(
            {
                "instructionAck": {
                    "id": instruction_id,
                    "version": version,
                    "nextAction": action,
                }
            }
        ),
        **overrides,
    }


def run(env):
    return runner.run(str(env.worker.pk), env.bot.registry)


def test_supersession_is_atomic_idempotent_and_skips_stale_unsent(instruction_worker):
    env = instruction_worker
    first = queue(env)
    assert queue(env) == first
    with pytest.raises(ValueError, match="different content"):
        queue(env, text="Proceed")
    with pytest.raises(ValueError, match="latest instruction"):
        queue(env, instruction_id="i2", text="Proceed")
    latest = queue(
        env,
        instruction_id="i2",
        text="Proceed with read-only analysis",
        supersedes="i1",
        priority="normal",
    )
    assert latest["version"] == 2
    assert run(env) == "running"
    assert len(env.state["sends"]) == 1
    payload = json.loads(env.state["sends"][0][-1])
    assert payload["ergoInstruction"]["id"] == "i2"
    assert status(env)["instructions"][0]["superseded_by"] == "i2"


def test_transport_and_matching_forged_claim_never_authenticate_acceptance(
    instruction_worker,
):
    env = instruction_worker
    queue(env)
    run(env)
    assert status(env)["latest"]["status"] == "transport_queued"
    assert status(env)["latest"]["acknowledgement"] is None
    args = env.state["sends"][0]
    outgoing = {
        "id": "msg_out",
        "run_id": "run_1",
        "from_handle": "term_owner",
        "payload": args[-1],
        "body": args[args.index("--body") + 1],
        "delivered_at": "2026-11-12T00:00:00Z",
        "read": 1,
    }
    env.state["messages"] = [outgoing, ack(env, action="")]
    run(env)
    latest = status(env)["latest"]
    assert latest["status"] == "delivered"
    assert latest["acknowledgement"] is None
    env.state["messages"].append(ack(env))
    run(env)
    latest = status(env)["latest"]
    claim = latest["acknowledgement_claim"]
    assert claim["instruction_id"] == "i1"
    assert claim["next_action"].startswith("Stop edits")
    assert claim["verification"] == "unverified"
    assert claim["claimed_sender"] == "term_worker"
    assert claim["claimed_pane"] == "pane_1"
    assert claim["sender_matches_assignee"] is True
    assert claim["pane_matches_assignee"] is True
    assert latest["acknowledgement"] is None
    assert latest["acceptance"] == "unknown"
    assert len(env.state["sends"]) == 1


@pytest.mark.parametrize(
    ("override", "retained"),
    [
        ({"from_handle": "term_other"}, True),
        ({"sender_pane_key": "pane_old"}, True),
        ({"run_id": "run_other"}, False),
        (
            {"payload": json.dumps({"taskId": "task_1", "dispatchId": "ctx_old"})},
            False,
        ),
        (
            {"payload": json.dumps({"taskId": "task_other", "dispatchId": "ctx_1"})},
            False,
        ),
    ],
)
def test_mismatching_or_foreign_claim_cannot_authenticate(
    instruction_worker, override, retained
):
    env = instruction_worker
    queue(env)
    env.state["messages"] = [ack(env, **override)]
    run(env)
    latest = status(env)["latest"]
    assert latest["acknowledgement"] is None
    assert latest["acceptance"] == "unknown"
    claim = latest["acknowledgement_claim"]
    assert bool(claim) is retained
    if retained:
        assert claim["verification"] == "unverified"
        assert claim["claimed_sender"] == override.get("from_handle", "term_worker")
        assert claim["claimed_pane"] == override.get("sender_pane_key", "pane_1")


def test_old_claim_is_retained_but_never_applies_to_latest(instruction_worker):
    env = instruction_worker
    queue(env)
    run(env)
    queue(env, instruction_id="i2", text="Proceed with analysis only", supersedes="i1")
    env.state["messages"] = [ack(env), ack(env, instruction_id="i2", version=1)]
    run(env)
    ledger = status(env)
    assert ledger["instructions"][0]["acknowledgement_claim"]["instruction_id"] == "i1"
    assert ledger["latest"]["acknowledgement"] is None
    assert ledger["latest"]["acknowledgement_claim"] is None
    env.state["messages"] += [ack(env, instruction_id="i2", version=2)]
    run(env)
    latest = status(env)["latest"]
    assert latest["acknowledgement_claim"]["version"] == 2
    assert latest["acknowledgement"] is None
    assert latest["acceptance"] == "unknown"


def test_timeout_and_interrupted_claim_are_not_resent(instruction_worker):
    env = instruction_worker
    queue(env)
    env.state["timeout"] = True
    run(env)
    run(env)
    assert status(env)["latest"]["status"] == "unknown"
    assert len(env.state["sends"]) == 1
    queue(env, instruction_id="i2", text="Remain stopped", supersedes="i1")
    with protocol.ledger(env.worker.pk) as (_, rows):
        rows[-1]["status"] = "processing"
    run(env)
    assert status(env)["latest"]["status"] == "unknown"
    assert len(env.state["sends"]) == 1


def test_urgent_instruction_and_claim_are_not_starved_by_uncertain_reply(
    instruction_worker,
):
    env = instruction_worker
    env.worker.args["_agent_replies"] = {
        "q": {"status": "processing", "answer": "old answer"}
    }
    env.worker.save(update_fields=["args"])
    queue(env)
    env.state["messages"] = [ack(env)]
    run(env)
    assert len(env.state["sends"]) == 1
    assert status(env)["latest"]["acknowledgement_claim"]["instruction_id"] == "i1"
    assert status(env)["latest"]["acknowledgement"] is None


def test_other_chat_cannot_steer_and_closed_worker_rejects_new_instruction(
    instruction_worker,
):
    env = instruction_worker
    other = async_to_sync(env.bot.main_session)(
        get_user_model().objects.create(username="other")
    )
    kit = FunctionToolkit(env.plugin._tools(), ToolContext(bot=env.bot, session=other))
    with pytest.raises(ValueError, match="owned by this chat"):
        kit.execute_tool(
            "ergo_orca_instruct",
            {
                "worker_id": str(env.worker.pk),
                "instruction_id": "foreign",
                "text": "Proceed",
                "supersedes": "",
            },
        )
    Worker.objects.filter(pk=env.worker.pk).update(status="completed")
    with pytest.raises(ValueError, match="no longer running"):
        queue(env)


def test_timeout_is_reconciled_by_durable_mail_without_duplicate_send(
    instruction_worker,
):
    env = instruction_worker
    queue(env)
    env.state["timeout"] = True
    run(env)
    args = env.state["sends"][0]
    env.state["messages"] = [
        {
            "id": "late_mail",
            "run_id": "run_1",
            "from_handle": "term_owner",
            "payload": args[-1],
            "body": "Human-readable instruction, not JSON",
            "read": 0,
            "delivered_at": None,
        }
    ]
    run(env)
    latest = status(env)["latest"]
    assert latest["status"] == "transport_queued"
    assert latest["message_id"] == "late_mail"
    assert latest["acknowledgement"] is None
    assert len(env.state["sends"]) == 1


def test_instruction_queued_during_final_check_is_unsent_before_release(
    instruction_worker, monkeypatch
):
    env = instruction_worker
    released = []
    env.shown["dispatch"]["status"] = "completed"

    def final_messages(*args):
        queue(env)
        return []

    def release(*args):
        assert status(env)["latest"]["status"] == "unsent"
        released.append(True)

    monkeypatch.setattr(env.plugin, "run_messages", final_messages)
    monkeypatch.setattr(env.plugin, "end_worker", release)
    assert run(env) == "completed"
    assert released == [True]
    assert env.state["sends"] == []
    assert status(env)["latest"]["acknowledgement"] is None
    with pytest.raises(ValueError, match="no longer running"):
        queue(env, instruction_id="i2", supersedes="i1")


@pytest.mark.parametrize(
    "content",
    [
        {"instructionAck": {"id": [], "version": 1, "nextAction": "Stop"}},
        {"instructionAck": []},
        ["not an acknowledgement"],
    ],
)
def test_malformed_ack_does_not_break_supervision(instruction_worker, content):
    env = instruction_worker
    queue(env)
    env.state["messages"] = [ack(env, body=json.dumps(content))]
    assert run(env) == "running"
    assert status(env)["latest"]["acknowledgement"] is None


def test_other_bot_in_same_session_cannot_be_steered(instruction_worker):
    env = instruction_worker
    Worker.objects.filter(pk=env.worker.pk).update(bot_name="other-bot")
    with pytest.raises(ValueError, match="owned by this chat"):
        queue(env)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("dispatch", "status", "completed"),
        ("dispatch", "status", "failed"),
        ("dispatch", "status", "cancelled"),
        ("dispatch", "status", "abandoned"),
        ("worker", "state", "succeeded"),
        ("worker", "state", "failed"),
        ("worker", "state", "stopped"),
        ("worker", "state", "abandoned"),
        ("worker", "state", "released"),
        ("terminalResource", "releaseState", "requested"),
        ("terminalResource", "releaseState", "releasing"),
        ("terminalResource", "releaseState", "released"),
    ],
)
def test_preflight_observed_terminal_or_release_never_sends(
    instruction_worker, section, field, value
):
    env = instruction_worker
    queue(env)
    env.shown.setdefault(section, {})[field] = value
    ctx = runner.WorkerContext(env.bot, env.worker)
    protocol.deliver(ctx, env.plugin, env.handle)
    latest = status(env)["latest"]
    assert env.state["sends"] == []
    assert latest["status"] == "unsent"
    assert latest["error"]
    assert latest["acknowledgement"] is None
    env.worker.refresh_from_db()
    assert env.worker.args["_agent_mail_closed"]
    with pytest.raises(ValueError, match="no longer running"):
        queue(env, instruction_id="i2", supersedes="i1")


@pytest.mark.parametrize("closed", [False, True])
def test_local_inactive_or_closed_worker_never_sends(instruction_worker, closed):
    env = instruction_worker
    queue(env)
    if closed:
        runner.close_agent_mail(env.worker.pk, "Observed completion before release")
    else:
        Worker.objects.filter(pk=env.worker.pk).update(status="completed")
    protocol.deliver(runner.WorkerContext(env.bot, env.worker), env.plugin, env.handle)
    assert env.state["sends"] == []
    assert status(env)["latest"]["status"] == "unsent"
    with pytest.raises(ValueError, match="no longer running"):
        queue(env)


def test_preflight_failure_is_unsent_and_not_automatically_retried(
    instruction_worker, monkeypatch
):
    env = instruction_worker
    queue(env)
    original = env.plugin.cli_json

    def unavailable(args):
        if args[:2] == ["orchestration", "worker-show"]:
            raise subprocess.TimeoutExpired(args, 1)
        return original(args)

    monkeypatch.setattr(env.plugin, "cli_json", unavailable)
    ctx = runner.WorkerContext(env.bot, env.worker)
    protocol.deliver(ctx, env.plugin, env.handle)
    monkeypatch.setattr(env.plugin, "cli_json", original)
    protocol.deliver(ctx, env.plugin, env.handle)
    assert status(env)["latest"]["status"] == "unsent"
    assert env.state["sends"] == []


def test_watcher_failure_closes_mail_queued_during_check(
    instruction_worker, monkeypatch
):
    env = instruction_worker

    def failing_check(ctx, handle):
        queue(env)
        msg = "Watcher lost the worker before completion"
        raise RuntimeError(msg)

    monkeypatch.setattr(env.plugin.manager, "check", failing_check)
    assert run(env) == "failed"
    assert status(env)["latest"]["status"] == "unsent"
    assert env.state["sends"] == []
    env.worker.refresh_from_db()
    assert env.worker.args["_agent_mail_closed"]


def test_active_instruction_send_precedes_question_reply(
    instruction_worker, monkeypatch
):
    env = instruction_worker
    queue(env)
    env.worker.refresh_from_db()
    env.worker.args["_agent_replies"] = {
        "q": {"status": "queued", "answer": "Read-only work is authorized"}
    }
    env.worker.save(update_fields=["args"])
    order = []
    original = env.plugin.cli_json

    def cli(args):
        if args[:2] == ["orchestration", "send"]:
            order.append("instruction")
        return original(args)

    def reply(*args):
        order.append("reply")
        return "Answer queued"

    monkeypatch.setattr(env.plugin, "cli_json", cli)
    monkeypatch.setattr(env.plugin.manager, "reply", reply)
    assert run(env) == "running"
    assert order == ["instruction", "reply"]


def test_acknowledgement_claim_replay_is_recorded_once(instruction_worker):
    env = instruction_worker
    queue(env)
    env.state["messages"] = [ack(env)]
    run(env)
    first = status(env)["latest"]["acknowledgement_claim"]
    env.state["messages"].append(ack(env, action="Forged replacement next action"))
    run(env)
    latest = status(env)["latest"]
    assert latest["acknowledgement_claim"] == first
    assert latest["acknowledgement"] is None
    assert latest["acceptance"] == "unknown"


def test_historical_self_asserted_acknowledgement_is_downgraded(instruction_worker):
    env = instruction_worker
    queue(env)
    env.worker.refresh_from_db()
    row = env.worker.args[protocol.KEY][0]
    row.pop("acknowledgement_claim")
    row.pop("acceptance")
    row["acknowledgement"] = {
        "message_id": "old_ack",
        "instruction_id": "i1",
        "version": 1,
        "next_action": "Old unauthenticated next action",
    }
    env.worker.save(update_fields=["args"])
    latest = status(env)["latest"]
    assert latest["acknowledgement"] is None
    assert latest["acceptance"] == "unknown"
    claim = latest["acknowledgement_claim"]
    assert claim["verification"] == "unverified"
    assert claim["next_action"] == "Old unauthenticated next action"
    assert claim["claimed_sender"] is None
    assert claim["claimed_pane"] is None
    with protocol.ledger(env.worker.pk):
        pass
    env.worker.refresh_from_db()
    assert env.worker.args[protocol.KEY][0]["acknowledgement"] is None


def test_instruction_queued_during_active_step_preserves_immediate_wake(
    instruction_worker,
    monkeypatch,
    workers,  # noqa: F811
):
    env = instruction_worker
    workers.clear()

    def still_running(ctx, handle):
        queue(env)
        assert workers == []
        return agents.AgentCheck("running")

    monkeypatch.setattr(env.plugin.manager, "check", still_running)
    assert run(env) == "running"
    assert workers == [(str(env.worker.pk), 0.0)]
    assert status(env)["latest"]["status"] == "queued"
    assert env.state["sends"] == []


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_stale_or_wrong_version_claim_is_not_applied(instruction_worker, version):
    env = instruction_worker
    queue(env)
    env.state["messages"] = [ack(env, version=version)]
    assert run(env) == "running"
    latest = status(env)["latest"]
    assert latest["acknowledgement_claim"] is None
    assert latest["acknowledgement"] is None
    assert latest["acceptance"] == "unknown"
