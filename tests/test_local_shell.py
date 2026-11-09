from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from tests.test_bot_workers import workers  # noqa: F401 — fixture
from tests.test_bots import make_bot

if TYPE_CHECKING:
    from pathlib import Path

# A stand-in for `claude -p ... --output-format stream-json`: it writes a file,
# asks a question when the brief says ASK (until resumed), and sleeps on SLOW.
FAKE_CLAUDE = """\
#!{python}
import json, os, sys, time
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
resumed = "--resume" in argv
def say(event):
    print(json.dumps(event), flush=True)
say({{"type": "system", "subtype": "init", "session_id": "sess-1", "model": "claude-test"}})
say({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "Write", "input": {{"file_path": "out/ad.txt"}}}}]}}}})
if "SLOW" in prompt:
    time.sleep(60)
if "FAIL" in prompt:
    print("boom", file=sys.stderr)
    sys.exit(3)
os.makedirs("out", exist_ok=True)
with open("out/ad.txt", "w") as f:
    f.write(prompt)
with open("env.json", "w") as f:
    json.dump(sorted(os.environ), f)
if "ASK" in prompt and not resumed:
    text = "NEEDS_INPUT: Blue or red?"
else:
    text = "Made out/ad.txt"
say({{"type": "result", "subtype": "success", "is_error": False, "result": text,
     "session_id": "sess-1", "num_turns": 2,
     "usage": {{"input_tokens": 10, "output_tokens": 5,
               "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}}}})
"""


def yaml_for(tmp_path: Path) -> str:
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    template = tmp_path / "kit"
    (template / "renderers").mkdir(parents=True)
    (template / "renderers" / "render.py").write_text("print('render')\n")
    (template / "linked").symlink_to("/etc/hostname")
    return f"""
    name: ads
    plugins:
      - name: local_shell
        root: {tmp_path / "jobs"}
        template: {template}
        agents: [claude, codex]
        executables: {{claude: {fake}}}
        secrets: [HIGGSFIELD_API_KEY]
        approve: false
        max_concurrent: 1
        timeout_minutes: 1
    """


def setup(tmp_path):
    from django_ergo.bots.agents import agent_toolkit
    from django_ergo.bots.tools import ToolContext

    bot, _ = make_bot(tmp_path, yaml_text=yaml_for(tmp_path), name="ads")
    user = get_user_model().objects.create(username="lee")
    session = async_to_sync(bot.main_session)(user)
    ctx = ToolContext(bot=bot, session=session, user=user)
    plugin = bot.plugin("local_shell")
    return bot, plugin, ctx, agent_toolkit(bot, ctx)


def wait_for_exit(folder: Path, number: int, seconds: float = 20) -> None:
    deadline = time.time() + seconds
    while not (folder / ".ergo" / f"run-{number}.exit.json").exists():
        assert time.time() < deadline, "the run didn't finish"
        time.sleep(0.1)


@pytest.mark.parametrize(
    ("config", "network", "sandbox"),
    [
        ({}, "true", "workspace-write"),
        ({"codex_network_access": False}, "false", "workspace-write"),
        ({"codex_sandbox": "read-only"}, "true", "read-only"),
    ],
)
def test_codex_network_and_sandbox_configuration(config, network, sandbox):
    from django_ergo.plugins.local_shell import LocalShellPlugin

    plugin = LocalShellPlugin(None, {**config, "command_prefix": ["env"]})
    plugin.on_load()
    argv = plugin.argv("codex", "brief", model="model-name", number=1)
    assert argv[:3] == ["env", "codex", "exec"]
    assert argv[argv.index("--sandbox") + 1] == sandbox
    assert argv[argv.index("-c") + 1] == (
        f"sandbox_workspace_write.network_access={network}"
    )
    assert argv[argv.index("--model") + 1] == "model-name"

    claude = plugin.argv("claude", "brief", model="model-name", number=1)
    assert claude[:2] == ["env", "claude"]
    assert "-c" not in claude and "--sandbox" not in claude
    assert claude[claude.index("--model") + 1] == "model-name"


def test_codex_network_access_requires_boolean():
    from django_ergo.plugins.local_shell import LocalShellPlugin

    plugin = LocalShellPlugin(None, {"codex_network_access": "false"})
    with pytest.raises(TypeError, match="codex_network_access must be a boolean"):
        plugin.on_load()


@pytest.mark.django_db(transaction=True)
def test_a_job_asks_is_answered_and_reports_its_files(tmp_path, workers, monkeypatch):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import AgentUsage
    from django_ergo.conversation.models import ThreadMessage
    from django_ergo.conversation.models import Worker

    monkeypatch.setenv("HIGGSFIELD_API_KEY", "hf-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    monkeypatch.setenv("AD_MANAGER_ADS_API_KEY", "must-not-pass")
    bot, plugin, ctx, kit = setup(tmp_path)
    started = json.loads(
        kit.execute_tool(
            "ergo_agent_start",
            {"brief": "Make an ad. ASK about color.", "workspace": "2026-w42"},
        )
    )
    worker = Worker.objects.get(pk=started["id"])
    assert worker.function == "agent:local"
    folder = tmp_path / "jobs" / "2026-w42"
    assert (folder / "brief.md").read_text().startswith("Make an ad.")
    assert (folder / "renderers" / "render.py").exists()
    assert not (folder / "linked").exists()  # template symlinks aren't copied

    wait_for_exit(folder, 1)
    env = json.loads((folder / "env.json").read_text())
    assert "HIGGSFIELD_API_KEY" in env
    assert "ANTHROPIC_API_KEY" not in env and "AD_MANAGER_ADS_API_KEY" not in env
    argv = json.loads((folder / ".ergo" / "run-1.json").read_text())["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--allowedTools" in argv

    assert w.run(started["id"], bot.registry) == "running"
    [asked] = ThreadMessage.objects.filter(metadata__worker=started["id"])
    assert "Blue or red?" in asked.text and "2026-w42:1" in asked.text

    said = kit.execute_tool(
        "ergo_agent_reply",
        {"worker_id": started["id"], "question_id": "2026-w42:1", "answer": "Blue"},
    )
    assert "queued" in said
    w.run(started["id"], bot.registry)
    wait_for_exit(folder, 2)
    argv = json.loads((folder / ".ergo" / "run-2.json").read_text())["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-1"

    assert w.run(started["id"], bot.registry) == "completed"
    report = Worker.objects.get(pk=started["id"]).result
    assert report["summary"] == "Made out/ad.txt"
    paths = {o["path"] for o in report["outputs"]}
    assert "out/ad.txt" in paths and "renderers/render.py" not in paths
    usage = AgentUsage.objects.get(worker_id=started["id"])
    assert (usage.model, usage.source, usage.input_tokens) == (
        "claude-test",
        "local_shell",
        20,
    )

    attached = json.loads(
        kit_tools(bot, ctx).execute_tool(
            "local_shell_attach", {"job": "2026-w42", "path": "out/ad.txt"}
        )
    )
    assert attached["filename"] == "ad.txt"
    with pytest.raises(ValueError, match="outside job"):
        kit_tools(bot, ctx).execute_tool(
            "local_shell_attach", {"job": "2026-w42", "path": "../x"}
        )
    with pytest.raises(ValueError, match="bookkeeping"):
        kit_tools(bot, ctx).execute_tool(
            "local_shell_attach", {"job": "2026-w42", "path": ".ergo/run-1.json"}
        )
    [job] = json.loads(kit_tools(bot, ctx).execute_tool("local_shell_jobs", {}))
    assert (job["job"], job["status"], job["runs"]) == ("2026-w42", "ended", 2)


@pytest.mark.django_db(transaction=True)
def test_runs_add_new_template_files_and_flag_missing_secrets(
    tmp_path,
    workers,  # noqa: F811
    monkeypatch,
):
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import ThreadMessage

    monkeypatch.delenv("HIGGSFIELD_API_KEY", raising=False)
    bot, plugin, ctx, kit = setup(tmp_path)
    started = json.loads(
        kit.execute_tool(
            "ergo_agent_start", {"brief": "Make an ad. ASK first.", "workspace": "old"}
        )
    )
    [warning] = started["warnings"]
    assert "HIGGSFIELD_API_KEY" in warning and "restart" in warning
    folder = tmp_path / "jobs" / "old"
    wait_for_exit(folder, 1)
    assert "HIGGSFIELD_API_KEY" not in json.loads((folder / "env.json").read_text())

    # The kit gains a helper after the job was made, and the job edited a file.
    kit_dir = tmp_path / "kit"
    (kit_dir / "bin").mkdir()
    (kit_dir / "bin" / "sume").write_text("#!/bin/sh\n")
    (kit_dir / "renderers" / "render.py").write_text("print('new')\n")
    (folder / "renderers" / "render.py").write_text("print('job edit')\n")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "hf-test")

    w.run(started["id"], bot.registry)
    kit.execute_tool(
        "ergo_agent_reply",
        {"worker_id": started["id"], "question_id": "old:1", "answer": "Go"},
    )
    w.run(started["id"], bot.registry)
    wait_for_exit(folder, 2)
    assert (folder / "bin" / "sume").read_text() == "#!/bin/sh\n"
    assert (folder / "renderers" / "render.py").read_text() == "print('job edit')\n"
    assert (folder / ".ergo" / "template.json").exists()
    assert not (folder / "linked").exists()
    assert "HIGGSFIELD_API_KEY" in json.loads((folder / "env.json").read_text())
    w.run(started["id"], bot.registry)
    told = " ".join(
        ThreadMessage.objects.filter(metadata__worker=started["id"]).values_list(
            "text", flat=True
        )
    )
    assert "Added new template files to the job: bin/sume." in told
    assert "Not set where" not in told


def kit_tools(bot, ctx):
    return bot.plugin("local_shell").toolkits(ctx)[0]


@pytest.mark.django_db(transaction=True)
def test_failures_limits_and_stopping(tmp_path, workers):  # noqa: F811
    from django_ergo.bots import workers as w
    from django_ergo.conversation.models import Worker

    bot, plugin, ctx, kit = setup(tmp_path)
    with pytest.raises(ValueError, match="isn't a job name"):
        kit.execute_tool("ergo_agent_start", {"brief": "x", "workspace": "../etc"})
    with pytest.raises(ValueError, match="not a path: use a short name"):
        kit.execute_tool(
            "ergo_agent_start", {"brief": "x", "workspace": "/home/dev/p/repo"}
        )

    failing = json.loads(
        kit.execute_tool("ergo_agent_start", {"brief": "FAIL", "workspace": "bad"})
    )
    wait_for_exit(tmp_path / "jobs" / "bad", 1)
    assert w.run(failing["id"], bot.registry) == "failed"
    assert "exit 3" in Worker.objects.get(pk=failing["id"]).error

    slow = json.loads(
        kit.execute_tool("ergo_agent_start", {"brief": "SLOW", "workspace": "slow"})
    )
    with pytest.raises(ValueError, match="the limit is 1"):
        kit.execute_tool("ergo_agent_start", {"brief": "x", "workspace": "other"})
    with pytest.raises(ValueError, match="already running"):
        kit.execute_tool("ergo_agent_start", {"brief": "x", "workspace": "slow"})
    assert w.run(slow["id"], bot.registry) == "running"
    said = kit.execute_tool("ergo_agent_stop", {"worker_id": slow["id"]})
    assert "Asked job slow to stop" in said
    wait_for_exit(tmp_path / "jobs" / "slow", 1)
    ended = json.loads(
        (tmp_path / "jobs" / "slow" / ".ergo" / "run-1.exit.json").read_text()
    )
    assert ended["reason"] == "stopped"


@pytest.mark.django_db(transaction=True)
def test_a_dead_runner_fails_the_job(tmp_path, workers):  # noqa: F811
    bot, plugin, ctx, kit = setup(tmp_path)
    folder = tmp_path / "jobs" / "ghost" / ".ergo"
    folder.mkdir(parents=True)
    (folder / "run-1.json").write_text(
        json.dumps({"agent": "claude", "argv": [], "started_at": 0})
    )
    (folder / "heartbeat").touch()
    import os

    os.utime(folder / "heartbeat", (0, 0))

    class Ctx:
        def activity(self, *a, **k):
            pass

    check = plugin.check(Ctx(), {"dir": str(folder.parent), "agent": "claude"})
    assert check.status == "failed" and "without finishing" in check.error
    from django_ergo.plugins.local_shell import LocalShellPlugin

    refused = LocalShellPlugin(bot, {"secrets": ["ANTHROPIC_API_KEY"]})
    with pytest.raises(ValueError, match="won't pass ANTHROPIC_API_KEY"):
        refused.on_load()


def question_job(tmp_path):
    from django_ergo.plugins.local_shell import LocalShellPlugin

    plugin = LocalShellPlugin(None, {})
    plugin.on_load()
    folder = tmp_path / "job"
    meta = folder / ".ergo"
    meta.mkdir(parents=True)
    (meta / "run-1.json").write_text(json.dumps({"agent": "claude"}))
    (meta / "run-1.exit.json").write_text(json.dumps({"code": 0}))
    (meta / "run-1.jsonl").write_text(
        json.dumps(
            {"type": "result", "result": "NEEDS_INPUT: Which?", "session_id": "session"}
        )
        + "\n"
    )
    return plugin, folder, {"dir": str(folder), "agent": "claude"}


def test_concurrent_replies_launch_only_once(tmp_path, monkeypatch):
    plugin, folder, handle = question_job(tmp_path)
    entered = Event()
    release = Event()
    calls = []

    def launch(ctx, folder, agent, argv, number, *, question_id):  # noqa: PLR0913
        calls.append(argv)
        entered.set()
        assert release.wait(5)
        (folder / ".ergo" / f"run-{number}.json").write_text(
            json.dumps({"agent": agent, "question_id": question_id})
        )

    monkeypatch.setattr(plugin, "launch", launch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(plugin.reply, None, handle, "job:1", "first")
        assert entered.wait(5)
        second = pool.submit(plugin.reply, None, handle, "job:1", "second")
        release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert len(calls) == 1
    assert calls[0][calls[0].index("-p") + 1] == "first"
    assert plugin.reply(None, handle, "job:1", "third").endswith("(run 2).")
    assert plugin.runs(folder) == [1, 2]


def test_reply_launch_failure_is_visible_and_not_retried(tmp_path, monkeypatch):
    from django_ergo.plugins.local_shell import LocalShellError

    plugin, folder, handle = question_job(tmp_path)

    def fail(*args, **kwargs):
        msg = "cannot start supervisor"
        raise OSError(msg)

    monkeypatch.setattr("django_ergo.plugins.local_shell.subprocess.Popen", fail)
    with pytest.raises(OSError, match="cannot start supervisor"):
        plugin.reply(None, handle, "job:1", "answer")

    class Ctx:
        def activity(self, *args, **kwargs):
            pass

    check = plugin.check(Ctx(), handle)
    assert check.status == "failed"
    assert "cannot start supervisor" in check.error
    with pytest.raises(LocalShellError, match="cannot start supervisor"):
        plugin.reply(None, handle, "job:1", "answer")
    assert plugin.runs(folder) == [1, 2]


def test_reply_rejects_a_completed_run_without_a_question(tmp_path):
    from django_ergo.plugins.local_shell import LocalShellError

    plugin, folder, handle = question_job(tmp_path)
    (folder / ".ergo" / "run-1.jsonl").write_text(
        json.dumps({"type": "result", "result": "Done", "session_id": "session"}) + "\n"
    )
    with pytest.raises(LocalShellError, match="isn't this job's open question"):
        plugin.reply(None, handle, "job:1", "answer")
    assert plugin.runs(folder) == [1]


def test_missing_agent_executable_is_reported_by_supervisor(tmp_path):
    from django_ergo.plugins.local_shell import LocalShellPlugin

    plugin = LocalShellPlugin(
        None, {"executables": {"claude": str(tmp_path / "missing")}}
    )
    plugin.on_load()
    folder = tmp_path / "job"
    folder.mkdir()
    argv = plugin.argv("claude", "do work", model="", number=1)
    plugin.launch(None, folder, "claude", argv, 1)
    wait_for_exit(folder, 1)

    class Ctx:
        session = None

        def activity(self, *args, **kwargs):
            pass

    check = plugin.check(Ctx(), {"dir": str(folder), "agent": "claude"})
    assert check.status == "failed"
    assert "missing" in check.error


def test_template_updates_reach_files_the_job_left_alone(tmp_path):
    from django_ergo.plugins.local_shell import _copy_template
    from django_ergo.plugins.local_shell import _sync_template

    kit, job = tmp_path / "kit", tmp_path / "job"
    (kit / "bin").mkdir(parents=True)
    (kit / "bin" / "helper").write_text("v1")
    (kit / "bin" / "edited").write_text("v1")
    _copy_template(kit, job)
    assert _sync_template(kit, job) == ([], [])  # records what the job got

    (job / "bin" / "edited").write_text("job's own")
    (kit / "bin" / "helper").write_text("v2")
    (kit / "bin" / "edited").write_text("v2")
    (kit / "bin" / "new").write_text("v1")
    assert _sync_template(kit, job) == (["bin/new"], ["bin/helper"])
    assert (job / "bin" / "helper").read_text() == "v2"
    assert (job / "bin" / "edited").read_text() == "job's own"

    # A job from before the manifest keeps differing files, and picks up updates once it matches.
    old = tmp_path / "old"
    (old / "bin").mkdir(parents=True)
    (old / "bin" / "helper").write_text("v0")
    assert _sync_template(kit, old) == (["bin/edited", "bin/new"], [])
    assert (old / "bin" / "helper").read_text() == "v0"
