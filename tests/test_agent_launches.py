"""DB-backed launch failure tests; all Orca effects and scheduling are fakes."""

import json

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from django_ergo.bots.tools import ToolContext
from django_ergo.bots.workers import WorkerStarter
from django_ergo.conversation.models import AgentLaunch
from django_ergo.conversation.models import Worker
from django_ergo.plugins.orca import OrcaError
from tests.test_bot_plugins import orca_bot
from tests.test_bot_workers import workers  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def launch_env(tmp_path, monkeypatch, workers):  # noqa: F811
    bot, _, plugin = orca_bot(tmp_path)
    user = get_user_model().objects.create(username="launch-owner")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    monkeypatch.setattr("django_ergo.bots.routing.coding_login_error", lambda _: "")
    monkeypatch.setattr(plugin, "resolved_worktree", lambda _: ("id:worktree", "/work"))
    monkeypatch.setattr(plugin, "mailbox", lambda *_: ("mailbox", "run"))
    calls = []
    stored = {}

    def cli(args):
        calls.append(args)
        # The journal is visible before even task-create, with full owner/scope.
        intent = AgentLaunch.objects.latest("created_at")
        assert intent.request["owner"] == str(user.pk)
        assert intent.spec_hash
        if args[:2] == ["orchestration", "task-create"]:
            stored["spec"] = args[args.index("--spec") + 1]
            return {"taskId": "task"}
        if args[:2] == ["orchestration", "task-list"]:
            return {"tasks": [{"id": "task", "spec": stored["spec"]}]}
        if args[:2] == ["orchestration", "worker-start"]:
            assert intent.handle["task"] == "task"
            return {"dispatchId": f"dispatch-{len(calls)}"}
        raise AssertionError(args)

    monkeypatch.setattr(plugin, "cli_json", cli)
    return ctx, plugin, calls, stored


def start(env, key="request-1", brief="Fix one thing", **kwargs):
    ctx, plugin, _, _ = env
    return plugin.start_worker(ctx, brief, "path:/work", launch_key=key, **kwargs)


def test_registration_failure_retains_original_and_recovers_once(
    launch_env, monkeypatch
):
    original = WorkerStarter.start

    def broken(*args, **kwargs):
        msg = "registration unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(WorkerStarter, "start", broken)
    with pytest.raises(ValueError, match="registration unavailable"):
        start(launch_env)
    intent = AgentLaunch.objects.get()
    assert intent.status == "recovery_needed"
    assert intent.handle["dispatch"]
    assert intent.handle["task"] == "task"
    assert intent.worker_id is None
    calls_before = list(launch_env[2])
    monkeypatch.setattr(WorkerStarter, "start", original)
    first = start(launch_env)
    second = start(launch_env)
    assert first["id"] == second["id"]
    assert Worker.objects.count() == 1
    assert launch_env[2] == calls_before
    assert Worker.objects.get().args["handle"]["dispatch"] == intent.handle["dispatch"]


def test_uncertain_response_blocks_same_key_without_relaunch(launch_env, monkeypatch):
    ctx, plugin, calls, _ = launch_env
    original = plugin.cli_json

    def uncertain(args):
        if args[:2] == ["orchestration", "worker-start"]:
            calls.append(args)
            msg = "closed connection after potential launch"
            raise OrcaError(msg)
        return original(args)

    monkeypatch.setattr(plugin, "cli_json", uncertain)
    with pytest.raises(ValueError, match="requires recovery"):
        start(launch_env)
    intent = AgentLaunch.objects.get()
    assert intent.status == "recovery_needed"
    assert intent.handle["task"] == "task"
    assert "dispatch" not in intent.handle
    before = list(calls)
    with pytest.raises(ValueError, match="Do not issue a replacement start"):
        start(launch_env)
    assert calls == before
    assert Worker.objects.count() == 0
    # A process death before it can mark recovery_needed stays fenced too.
    intent.status = "launching"
    intent.save()
    with pytest.raises(ValueError, match="requires recovery"):
        start(launch_env)
    assert calls == before


def test_explicit_distinct_keys_allow_identical_jobs(launch_env):
    assert start(launch_env, "one")["id"] != start(launch_env, "two")["id"]
    assert AgentLaunch.objects.count() == 2
    assert Worker.objects.count() == 2


@pytest.mark.parametrize("change", [{"brief": "Changed scope"}, {"model": "other"}])
def test_changed_spec_same_key_rejected_before_effect(launch_env, change):
    start(launch_env)
    before = list(launch_env[2])
    with pytest.raises(ValueError, match="different spec or authorization scope"):
        start(launch_env, **change)
    assert launch_env[2] == before


def test_scope_change_cannot_recover_under_broader_configuration(launch_env):
    start(launch_env)
    launch_env[1].scoped_repositories = {"new-scope": "repo"}
    with pytest.raises(ValueError, match="authorization scope"):
        start(launch_env)


def test_full_sentinel_and_oversize_before_external_effect(launch_env):
    brief = "long requirement\n" * 2000 + "ACCEPTANCE_SENTINEL"
    start(launch_env, brief=brief)
    assert launch_env[3]["spec"].startswith(brief + "\n\n")
    assert AgentLaunch.objects.get().request["spec"]["brief"] == brief
    before = list(launch_env[2])
    with pytest.raises(ValueError, match="65536-byte"):
        start(launch_env, "oversize", brief="é" * 40000)
    assert launch_env[2] == before


@pytest.mark.parametrize("fallback", [True, False])
def test_task_list_versions_fail_closed_on_missing_page(
    launch_env, monkeypatch, fallback
):
    _, plugin, calls, _ = launch_env
    original = plugin.cli_json

    def paged(args):
        if args[:2] == ["orchestration", "task-list"]:
            calls.append(args)
            if fallback and "--full" in args:
                msg = "Unknown flag --full"
                raise OrcaError(msg)
            return {"tasks": [], "nextCursor": "another-page"}
        return original(args)

    monkeypatch.setattr(plugin, "cli_json", paged)
    with pytest.raises(ValueError, match="could not be verified"):
        start(launch_env)
    assert not any(args[:2] == ["orchestration", "worker-start"] for args in calls)
    listings = [args for args in calls if args[:2] == ["orchestration", "task-list"]]
    assert len(listings) == (2 if fallback else 1)


def test_old_task_list_full_rows_succeed(launch_env, monkeypatch):
    _, plugin, _, _ = launch_env
    original = plugin.cli_json

    def old(args):
        if args[:2] == ["orchestration", "task-list"] and "--full" in args:
            msg = "Unknown flag --full"
            raise OrcaError(msg)
        return original(args)

    monkeypatch.setattr(plugin, "cli_json", old)
    assert start(launch_env)["orca"]["dispatch"]


def test_known_identity_in_failed_cli_response_is_recoverable(launch_env, monkeypatch):
    import subprocess

    _, plugin, _, _ = launch_env
    original = plugin.cli_json

    def failed_start(args):
        if args[:2] == ["orchestration", "worker-start"]:
            # Exercise real CLI envelope decoding, including structured identity.
            return type(plugin).cli_json(plugin, args)
        return original(args)

    monkeypatch.setattr(plugin, "cli_json", failed_start)
    body = {
        "ok": False,
        "result": {
            "dispatchId": "original-dispatch",
            "nativeUsageIdentity": {"sessionId": "native-original"},
        },
        "error": {"code": "registration_failed", "message": "lost registration"},
    }
    monkeypatch.setattr(
        "django_ergo.plugins.orca.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(body), stderr=""
        ),
    )
    with pytest.raises(ValueError, match="requires recovery"):
        start(launch_env)
    intent = AgentLaunch.objects.get()
    assert intent.handle["dispatch"] == "original-dispatch"
    assert intent.handle["nativeUsageIdentity"] == {"sessionId": "native-original"}
    recovered = start(launch_env)
    assert recovered["orca"]["dispatch"] == "original-dispatch"
    assert Worker.objects.count() == 1


def test_postcommit_registration_error_keeps_linked_watcher(launch_env, monkeypatch):
    from django_ergo.bots import workers as worker_module

    def fail_notify(worker):
        msg = "notification failed after commit"
        raise RuntimeError(msg)

    monkeypatch.setattr(worker_module, "_notify", fail_notify)
    with pytest.raises(ValueError, match="notification failed"):
        start(launch_env)
    intent = AgentLaunch.objects.get()
    assert intent.worker_id
    before = list(launch_env[2])
    assert start(launch_env)["id"] == str(intent.worker_id)
    assert Worker.objects.count() == 1
    assert launch_env[2] == before


def test_retry_during_inflight_launch_does_not_start_again(launch_env, monkeypatch):
    _, plugin, _, _ = launch_env
    original = plugin.cli_json
    retried = []

    def inflight(args):
        if args[:2] == ["orchestration", "worker-start"]:
            with pytest.raises(ValueError, match="requires recovery"):
                start(launch_env)
            retried.append(True)
        return original(args)

    monkeypatch.setattr(plugin, "cli_json", inflight)
    start(launch_env)
    assert retried == [True]
    assert AgentLaunch.objects.count() == 1
    assert Worker.objects.count() == 1


def test_missing_dispatch_receipt_stays_blocked(launch_env, monkeypatch):
    _, plugin, calls, _ = launch_env
    original = plugin.cli_json

    def missing(args):
        if args[:2] == ["orchestration", "worker-start"]:
            calls.append(args)
            return {}
        return original(args)

    monkeypatch.setattr(plugin, "cli_json", missing)
    with pytest.raises(ValueError, match="no dispatch id"):
        start(launch_env)
    before = list(calls)
    with pytest.raises(ValueError, match="Do not issue a replacement start"):
        start(launch_env)
    assert calls == before


def test_recovery_revalidates_original_authenticated_authority(launch_env, monkeypatch):
    from django_ergo.conversation.models import SessionMessage

    ctx, _, calls, _ = launch_env
    message = SessionMessage.objects.create(
        session=ctx.session,
        role="user",
        sequence=1,
        author={
            "kind": "django_user",
            "ref": str(ctx.session.user_id),
            "authenticated_by": "ergonaut_session",
        },
    )
    original = WorkerStarter.start

    def broken(*args, **kwargs):
        msg = "registration failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(WorkerStarter, "start", broken)
    with pytest.raises(ValueError, match="registration failed"):
        start(launch_env, authority_message_id=str(message.pk))
    intent = AgentLaunch.objects.get()
    assert intent.request["authority"]["message_id"] == str(message.pk)
    assert intent.handle["authority"] == intent.request["authority"]
    before = list(calls)
    monkeypatch.setattr(WorkerStarter, "start", original)
    message.provenance = {"forwarded": True}
    message.save()
    with pytest.raises(ValueError, match="authenticated user message"):
        start(launch_env, authority_message_id=str(message.pk))
    assert not Worker.objects.exists()
    assert calls == before
    message.provenance = {}
    message.save()
    assert (
        start(launch_env, authority_message_id=str(message.pk))["orca"]["dispatch"]
        == intent.handle["dispatch"]
    )
    assert calls == before


def test_other_user_context_cannot_recover_launch(launch_env):
    start(launch_env)
    launch_env[0].user = get_user_model().objects.create(username="different-owner")
    before = list(launch_env[2])
    with pytest.raises(ValueError, match="owner context"):
        start(launch_env)
    assert launch_env[2] == before


def test_authority_overhead_is_sized_before_scoped_worktree_effect(launch_env):
    from django_ergo.bots.agents import AgentSpec
    from django_ergo.conversation.models import SessionMessage
    from django_ergo.plugins.orca_instructions import CHECKPOINT_GUIDANCE

    ctx, plugin, calls, _ = launch_env
    message = SessionMessage.objects.create(
        session=ctx.session,
        role="user",
        sequence=1,
        author={
            "kind": "django_user",
            "ref": str(ctx.session.user_id),
            "authenticated_by": "ergonaut_session",
        },
    )
    plugin.scoped_repositories = {"demo": "repo"}
    # Fits with checkpoint guidance alone; authority context exceeds the bound.
    brief = "x" * (65536 - len(CHECKPOINT_GUIDANCE.encode()) - 2 - 10)
    with pytest.raises(ValueError, match="65536-byte"):
        plugin.start_agent(
            ctx, AgentSpec(brief, "demo", authority_message_id=str(message.pk))
        )
    assert calls == []


def test_journal_protects_individual_watcher_but_allows_chat_deletion(launch_env):
    from django.db.models.deletion import RestrictedError

    start(launch_env)
    worker = Worker.objects.get()
    with pytest.raises(RestrictedError):
        worker.delete()
    assert Worker.objects.count() == 1
    launch_env[0].session.delete()
    assert not Worker.objects.exists()
    assert not AgentLaunch.objects.exists()
