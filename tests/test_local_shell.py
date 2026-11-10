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


# A stand-in for `codex exec --json`: one turn with its token usage.
FAKE_CODEX = """\
#!{python}
import json, sys
argv = sys.argv[1:]
def say(event):
    print(json.dumps(event), flush=True)
say({{"type": "thread.started", "thread_id": "t-1"}})
say({{"type": "turn.started"}})
say({{"type": "item.completed", "item": {{"type": "agent_message", "text": "Rendered"}}}})
say({{"type": "turn.completed", "usage": {{"input_tokens": 1000,
     "cached_input_tokens": 900, "output_tokens": 50}}}})
with open(argv[argv.index("--output-last-message") + 1], "w") as f:
    f.write("Rendered the cut")
"""


def yaml_for(tmp_path: Path) -> str:
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    codex = tmp_path / "fake-codex"
    codex.write_text(FAKE_CODEX.format(python=sys.executable))
    codex.chmod(0o755)
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
        executables: {{claude: {fake}, codex: {codex}}}
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


@pytest.mark.django_db
def test_upload_shared_files_are_scoped_to_recipient(settings, tmp_path):
    from types import SimpleNamespace

    from django_ergo.bots.messaging import shared_file
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ThreadMessage

    settings.MEDIA_ROOT = tmp_path / "media"
    bot, _ = make_bot(
        tmp_path,
        yaml_text=yaml_for(tmp_path) + "\n      - name: attachments\n",
        name="ads",
    )
    user = get_user_model().objects.create_user("upload-owner")
    other_user = get_user_model().objects.create_user("upload-other")
    source = ConversationSession.objects.create(user=user, bot_name="source")
    destination = ConversationSession.objects.create(user=user, bot_name=bot.name)
    other_session = ConversationSession.objects.create(user=user, bot_name=bot.name)
    private_session = ConversationSession.objects.create(
        user=other_user, bot_name="source"
    )
    text = save_session_file(source, "brief.txt", b"Shared brief\n")
    binary = save_session_file(source, "image.bin", b"\x00\xffbinary\n")
    unshared = save_session_file(source, "unshared.txt", b"not shared")
    private = save_session_file(private_session, "private.txt", b"private")
    owned = save_session_file(destination, "owned.txt", b"owned bytes")
    ThreadMessage.objects.create(
        sender_session=source,
        recipient_session=destination,
        text="Files for the job",
        metadata={"attachments": [shared_file(text), shared_file(binary)]},
    )
    # Even an explicit reference cannot grant access to another user's file.
    ThreadMessage.objects.create(
        sender_session=private_session,
        recipient_session=destination,
        text="Private file",
        metadata={"attachments": [shared_file(private)]},
    )
    plugin = bot.plugin("local_shell")

    def toolkit(session=destination, viewer=user, current_bot=bot):
        return FunctionToolkit(
            plugin._tools(),
            ToolContext(bot=current_bot, session=session, user=viewer),
        )

    tools = toolkit()
    result = json.loads(
        tools.execute_tool(
            "local_shell_upload",
            {"job": "shared", "files": [str(text.id), str(binary.id)]},
        )
    )
    assert result == [
        {"file": "brief.txt", "path": "inputs/brief.txt"},
        {"file": "image.bin", "path": "inputs/image.bin"},
    ]
    inputs = tmp_path / "jobs" / "shared" / "inputs"
    assert (inputs / "brief.txt").read_bytes() == b"Shared brief\n"
    assert (inputs / "image.bin").read_bytes() == b"\x00\xffbinary\n"

    # Source session selection works for a shared ID, not for an unshared name.
    tools.execute_tool(
        "local_shell_upload",
        {"job": "selected", "files": [str(text.id)], "session_id": str(source.id)},
    )
    assert (
        tmp_path / "jobs" / "selected" / "inputs" / "brief.txt"
    ).read_bytes() == b"Shared brief\n"
    tools.execute_tool(
        "local_shell_upload",
        {"job": "owned", "files": [str(owned.id), "owned.txt"], "folder": "assets"},
    )
    assert (
        tmp_path / "jobs" / "owned" / "assets" / "owned.txt"
    ).read_bytes() == b"owned bytes"

    denied = [
        (tools, str(unshared.id), {}),
        (tools, str(private.id), {}),
        (tools, "brief.txt", {}),
        (tools, "brief.txt", {"session_id": str(source.id)}),
        (tools, str(text.id), {"session_id": str(other_session.id)}),
        (tools, str(owned.id), {"session_id": str(source.id)}),
        (toolkit(session=other_session), str(text.id), {}),
        (toolkit(viewer=other_user), str(text.id), {}),
        (toolkit(current_bot=SimpleNamespace(name="wrong-bot")), str(text.id), {}),
    ]
    for index, (caller, ref, extra) in enumerate(denied):
        job = f"denied-{index}"
        with pytest.raises(ValueError, match="No |Not authorized"):
            caller.execute_tool(
                "local_shell_upload", {"job": job, "files": [ref], **extra}
            )
        assert not (tmp_path / "jobs" / job / "inputs" / "brief.txt").exists()
        assert not (tmp_path / "jobs" / job / "inputs" / "unshared.txt").exists()
        assert not (tmp_path / "jobs" / job / "inputs" / "private.txt").exists()

    attachments = bot.plugin("attachments")
    attachments.other_sessions = False
    with pytest.raises(ValueError, match="only read files in its own session"):
        tools.execute_tool(
            "local_shell_upload", {"job": "restricted", "files": [str(text.id)]}
        )
    tools.execute_tool(
        "local_shell_upload", {"job": "restricted", "files": [str(owned.id)]}
    )

    bot.plugins.remove(attachments)
    with pytest.raises(ValueError, match="requires the attachments plugin"):
        tools.execute_tool(
            "local_shell_upload", {"job": "no-plugin", "files": [str(text.id)]}
        )
    tools.execute_tool(
        "local_shell_upload", {"job": "no-plugin", "files": [str(owned.id)]}
    )


@pytest.mark.django_db
def test_shared_files_can_be_passed_on_and_then_uploaded(settings, tmp_path):
    """A chat that received a share can pass it on (ergo_thread_send), and
    the next chat can copy it into a job; an id quoted in text is no share."""
    from django_ergo.bots.messaging import find_shareable_file
    from django_ergo.bots.messaging import shared_file
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.attachments import save_session_file
    from django_ergo.conversation.models import ConversationSession
    from django_ergo.conversation.models import ThreadMessage

    settings.MEDIA_ROOT = tmp_path / "media"
    bot, _ = make_bot(
        tmp_path,
        yaml_text=yaml_for(tmp_path) + "\n      - name: attachments\n",
        name="ads",
    )
    user = get_user_model().objects.create_user("pass-on-owner")
    stranger = get_user_model().objects.create_user("pass-on-stranger")
    origin = ConversationSession.objects.create(user=user, bot_name="devbox")
    design = ConversationSession.objects.create(user=user, bot_name=bot.name)
    board = ConversationSession.objects.create(user=user, bot_name=bot.name)
    image = save_session_file(origin, "sheet.png", b"\x89PNG sheet")
    ThreadMessage.objects.create(
        sender_session=origin,
        recipient_session=design,
        text="Teaser files",
        metadata={"attachments": [shared_file(image)]},
    )

    def upload(session, ref):
        tools = FunctionToolkit(
            bot.plugin("local_shell")._tools(),
            ToolContext(bot=bot, session=session, user=user),
        )
        return json.loads(
            tools.execute_tool("local_shell_upload", {"job": "art", "files": [ref]})
        )

    # Only quoted in text: the error says how to get a real share.
    ThreadMessage.objects.create(
        sender_session=design, recipient_session=board, text=f"use {image.id}"
    )
    with pytest.raises(ValueError, match="share it here"):
        upload(board, str(image.id))
    with pytest.raises(ValueError, match="share it here"):
        find_shareable_file(board, str(image.id))

    # design passes the share on; it stays stored in its origin chat.
    passed = find_shareable_file(design, str(image.id))
    assert passed.session_id == origin.id
    ThreadMessage.objects.create(
        sender_session=design,
        recipient_session=board,
        text="Refs",
        metadata={"attachments": [shared_file(passed)]},
    )
    assert upload(board, str(image.id)) == [
        {"file": "sheet.png", "path": "inputs/sheet.png"}
    ]
    assert (tmp_path / "jobs" / "art" / "inputs" / "sheet.png").read_bytes() == (
        b"\x89PNG sheet"
    )

    # A share names a file; it can't hand over another person's.
    theirs = save_session_file(
        ConversationSession.objects.create(user=stranger, bot_name="x"),
        "theirs.txt",
        b"no",
    )
    ThreadMessage.objects.create(
        sender_session=origin,
        recipient_session=design,
        text="Theirs",
        metadata={"attachments": [shared_file(theirs)]},
    )
    with pytest.raises(ValueError, match="share it here"):
        find_shareable_file(design, str(theirs.id))


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


def test_a_fresh_run_in_the_same_folder_closes_the_older_worker(tmp_path):
    """Two ergo_agent_start calls on one job folder: the first worker stops
    following the folder instead of staying "running" and repeating the newer
    run's question (seen with three Codex recovery runs on one ads job)."""
    plugin, folder, _ = question_job(tmp_path)
    meta = folder / ".ergo"
    first = {"dir": str(folder), "agent": "codex", "run": 1}
    (meta / "run-1.json").write_text(json.dumps({"agent": "codex"}))
    (meta / "run-1.last.txt").write_text("NEEDS_INPUT: Retry the task?")
    (meta / "run-2.json").write_text(json.dumps({"agent": "codex"}))
    (meta / "run-2.exit.json").write_text(json.dumps({"code": 0}))
    (meta / "run-2.last.txt").write_text("NEEDS_INPUT: Still failing. Retry later?")
    (meta / "heartbeat").touch()
    second = {"dir": str(folder), "agent": "codex", "run": 2}

    class Ctx:
        def activity(self, *a, **k):
            pass

    old = plugin.check(Ctx(), first)
    assert old.status == "done" and not old.questions
    assert "Run 2 has since started" in old.report["summary"]
    assert old.report["superseded_by_run"] == 2
    new = plugin.check(Ctx(), second)
    assert new.status == "running"
    assert [q.id for q in new.questions] == ["job:2"]

    # A run that answers this worker's question is still this worker's.
    (meta / "run-3.json").write_text(
        json.dumps({"agent": "claude", "question_id": "job:2"})
    )
    assert plugin.own_runs(folder, second) == ([2, 3], None)
    # An older handle without "run" follows the whole folder, as before.
    assert plugin.own_runs(folder, {"dir": str(folder)}) == ([1, 2, 3], None)

    # A run that never wrote its exit is dead once a newer run exists, even
    # though the shared heartbeat is fresh.
    (meta / "run-1.json").unlink()
    (meta / "run-1.last.txt").unlink()
    (meta / "run-1.exit.json").unlink(missing_ok=True)
    (meta / "run-1.json").write_text(json.dumps({"agent": "claude"}))
    dead = plugin.check(Ctx(), first)
    assert dead.status == "failed" and "Run 2 has since started" in dead.error


def test_an_agent_killed_for_memory_is_reported_and_its_supervisor_survives(tmp_path):
    """The agent gets the top OOM score, so a job that runs the pod out of
    memory loses the agent, not Ergonaut; the supervisor records the kill."""
    from django_ergo.plugins.local_shell import LocalShellPlugin

    agent = tmp_path / "hog"
    agent.write_text(
        f"#!{sys.executable}\n"
        "import os, signal\n"
        "print(open('/proc/self/oom_score_adj').read().strip(), file=open('score', 'w'))\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    agent.chmod(0o755)
    plugin = LocalShellPlugin(None, {"executables": {"claude": str(agent)}})
    plugin.on_load()
    folder = tmp_path / "job"
    folder.mkdir()
    plugin.launch(None, folder, "claude", [str(agent)], 1)
    wait_for_exit(folder, 1)
    assert (folder / "score").read_text().strip() == "1000"
    ended = json.loads((folder / ".ergo" / "run-1.exit.json").read_text())
    assert ended["reason"] == "killed"

    class Ctx:
        session = None

        def activity(self, *args, **kwargs):
            pass

    check = plugin.check(Ctx(), {"dir": str(folder), "agent": "claude", "run": 1})
    assert check.status == "failed" and "out of memory" in check.error


def test_transcript_usage_reads_claude_and_codex_runs():
    from django_ergo.plugins.local_shell import transcript_usage

    finished = [
        {"type": "system", "subtype": "init", "model": "claude-opus"},
        {
            "type": "result",
            "num_turns": 4,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {
                "claude-opus": {
                    "inputTokens": 10,
                    "outputTokens": 20,
                    "cacheReadInputTokens": 300,
                    "cacheCreationInputTokens": 40,
                },
                "claude-haiku": {"inputTokens": 5, "outputTokens": 6},
            },
        },
    ]
    message = {"id": "m1", "model": "claude-opus", "usage": {"input_tokens": 7}}
    running = [
        {"type": "system", "subtype": "init", "model": "claude-opus"},
        # stream-json repeats a message per content block: counted once
        {"type": "assistant", "message": message},
        {
            "type": "assistant",
            "message": {**message, "usage": {"input_tokens": 7, "output_tokens": 9}},
        },
    ]
    usage = transcript_usage("claude", [finished, running], [{}, {}])
    assert usage["claude-opus"] == {
        "input": 17,
        "cache_write": 40,
        "cache_read": 300,
        "output": 29,
        "reasoning": 0,
        "requests": 5,
    }
    assert usage["claude-haiku"]["output"] == 6

    turn = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1000,
            "cached_input_tokens": 900,
            "output_tokens": 50,
            "reasoning_output_tokens": 30,
        },
    }
    usage = transcript_usage(
        "codex",
        [[turn], [turn]],
        [{"argv": ["codex", "exec", "--model", "gpt-test", "do it"]}, {}],
    )
    assert usage["gpt-test"] == {
        "input": 100,
        "cache_write": 0,
        "cache_read": 900,
        "output": 50,
        "reasoning": 30,
        "requests": 1,
    }
    assert usage["codex"]["cache_read"] == 900  # no --model: the default model


@pytest.mark.django_db(transaction=True)
def test_codex_jobs_record_usage_and_prefer_its_session_files(
    tmp_path,
    workers,  # noqa: F811
    monkeypatch,
):
    """Codex jobs used to record nothing, so a chat of Codex workers showed no
    agent tokens at all."""

    from django_ergo.bots import workers as w
    from django_ergo.bots.agents import worker_of
    from django_ergo.conversation.models import AgentUsage
    from django_ergo.conversation.models import Worker

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bot, plugin, ctx, kit = setup(tmp_path)
    started = json.loads(
        kit.execute_tool(
            "ergo_agent_start",
            {"brief": "Render the cut", "workspace": "cut", "agent": "codex"},
        )
    )
    folder = tmp_path / "jobs" / "cut"
    wait_for_exit(folder, 1)
    assert w.run(started["id"], bot.registry) == "completed"
    usage = AgentUsage.objects.get(worker_id=started["id"])
    assert (usage.model, usage.agent, usage.input_tokens, usage.cache_read_tokens) == (
        "codex",
        "codex",
        100,
        900,
    )

    # Codex's own session file has every request, with its model: it wins.
    worker = Worker.objects.get(pk=started["id"])
    at = worker.created_at.isoformat()
    sessions = tmp_path / "home" / ".codex" / "sessions" / "2026" / "11" / "10"
    sessions.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"cwd": str(folder.resolve())}},
        {"type": "turn_context", "payload": {"model": "gpt-6-codex"}},
        {
            "type": "event_msg",
            "timestamp": at,
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 5000,
                        "cached_input_tokens": 4000,
                        "output_tokens": 70,
                    }
                },
            },
        },
    ]
    (sessions / "rollout-x-t-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows)
    )
    manager, handle = worker_of(bot, worker)
    assert (
        manager.record_usage(worker, handle, dry_run=True)["gpt-6-codex"]["input"]
        == 1000
    )
    assert AgentUsage.objects.get(worker=worker).model == "codex"  # dry run
    manager.record_usage(worker, handle)
    [usage] = AgentUsage.objects.filter(worker=worker)
    assert (usage.model, usage.input_tokens, usage.cache_read_tokens) == (
        "gpt-6-codex",
        1000,
        4000,
    )


@pytest.mark.django_db(transaction=True)
def test_a_running_job_records_usage_as_it_goes(tmp_path, monkeypatch):
    from django_ergo.conversation.models import AgentUsage

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bot, plugin, ctx, kit = setup(tmp_path)
    started = json.loads(
        kit.execute_tool("ergo_agent_start", {"brief": "SLOW job", "workspace": "slow"})
    )
    from django_ergo.bots.workers import WorkerContext
    from django_ergo.conversation.models import Worker

    worker = Worker.objects.get(pk=started["id"])
    folder = tmp_path / "jobs" / "slow"
    transcript_path = folder / ".ergo" / "run-1.jsonl"
    deadline = time.time() + 10
    while not transcript_path.exists() or transcript_path.read_text().count("\n") < 2:
        assert time.time() < deadline, "the job didn't start"
        time.sleep(0.1)
    with (folder / ".ergo" / "run-1.jsonl").open("a") as transcript:
        message = {
            "id": "m1",
            "model": "claude-test",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
        transcript.write(json.dumps({"type": "assistant", "message": message}) + "\n")
    wctx = WorkerContext(bot, worker)
    handle = worker.args["handle"]
    try:
        assert plugin.check(wctx, handle).status == "running"
        usage = AgentUsage.objects.get(worker=worker)
        assert (usage.input_tokens, usage.output_tokens) == (3, 4)
        # Not again until USAGE_SECONDS have passed.
        AgentUsage.objects.all().delete()
        plugin.check(wctx, handle)
        assert not AgentUsage.objects.exists()
    finally:
        plugin.stop(handle)
