"""Local shell plugin: run agent CLIs (Claude Code, Codex) in job folders on this host.

    plugins:
      - name: local_shell
        root: ~/agent-jobs/ads        # every job is a folder here: <root>/<workspace>
        template: ~/kits/creative     # optional: copied into each new job folder
        agents: [claude, codex]       # what ergo_agent_start may start here
        executables: {claude: claude, codex: codex}   # default: the agent's name
        command_prefix: []            # e.g. [sudo, -n, -u, agentrunner] or a bwrap line
        allowed_tools: [Read, Write, Edit, Glob, Grep, Bash]   # Claude Code --allowedTools
        disallowed_tools: [WebSearch]                          # Claude Code --disallowedTools
        permission_mode: dontAsk      # Claude Code: anything not allowed is refused
        codex_sandbox: workspace-write
        max_turns: 100
        pass_env: [PATH, HOME, LANG, LC_ALL, TZ, USER]   # the only host variables passed on
        secrets: [HIGGSFIELD_API_KEY] # bot secrets every job gets (API-billed tools)
        timeout_minutes: 60           # per run
        max_concurrent: 2             # running jobs at once, across this root
        approve: true                 # starting, answering and stopping ask first
        root_only: false
        poll_seconds: 30
        instructions: ""              # added to every job's system prompt

An agent manager named ``local`` (see bots.agents): ``ergo_agent_start``
with ``workspace`` set to a job name makes ``<root>/<name>/`` (copying the
template the first time), writes the brief to ``brief.md`` and starts the
agent there, headless (``claude -p`` with stream-json output, or ``codex
exec --json``). A small supervisor (``local_shell_runner.py``) runs it
detached in its own process group and keeps its transcript, heartbeat and
exit status in ``<job>/.ergo/``, so the watcher can check it from any
process that sees the folder (Ergonaut runs tools and workers in different
pods that share the home volume). A job that ends with a final message
starting ``NEEDS_INPUT:`` asks the chat; ``ergo_agent_reply`` resumes the
same Claude session with the answer. When it's done, the report lists the
files the run made or changed, with sizes and SHA-256 hashes.

Permissions, outermost first: approval on start/reply/stop; the job folder
is the agent's working directory (and, with a sandboxing
``command_prefix``, the only place it can write); the environment is
rebuilt from ``pass_env`` plus the listed ``secrets``, never API keys that
would switch an agent CLI off its subscription, and never protected
variables (``FIREWORKS_*`` and ``DJANGO_ERGO["PROTECTED_ENV_PREFIXES"]``,
defense-in-depth rather than a sandbox); Claude Code gets
``--permission-mode``, ``--allowedTools``/``--disallowedTools`` and
``--max-turns``; runs time out; and at most ``max_concurrent`` jobs run.
The agent never gets this bot's other secrets.

Tools: ``local_shell_jobs`` lists job folders and their state;
``local_shell_upload`` copies chat files into a job folder;
``local_shell_attach`` copies a job's file into the chat. None needs
approval: they only read, or write inside a job folder.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.agents import AgentCheck
from django_ergo.bots.agents import AgentManager
from django_ergo.bots.agents import AgentQuestion
from django_ergo.bots.agents import AgentSpec
from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource
from django_ergo.conversation.inference_policy import is_protected_env
from django_ergo.plugins.orca import is_secret

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

log = logging.getLogger(__name__)
JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
RUNNER = Path(__file__).with_name("local_shell_runner.py")
META = ".ergo"
QUESTION = "NEEDS_INPUT:"
# A run whose supervisor hasn't touched its heartbeat for this long is dead
# (its pod restarted); the runner beats every few seconds.
STALE_SECONDS = 90
# Variables that would move an agent CLI off its subscription onto API billing.
NEVER_PASS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}
DEFAULT_PASS_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "USER")
DEFAULT_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash")
MAX_ATTACH_BYTES = 50_000_000
MAX_OUTPUTS = 200
ACTIVITY_ENTRIES = 8
CLIP = 300
JOB_PROMPT = """\
You are working headless in a job folder: {folder}. Work only inside it and
write every result file there; the person reviews those files. Inputs and the
brief are in this folder (brief.md). Nobody can answer you while you run. If
you can't go on without a decision, stop and make your final message start
with "NEEDS_INPUT:" followed by the question and the options. Otherwise end
with a short summary: what you made (file paths), how, and anything that cost
money."""


def _clip(text: str, limit: int = CLIP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _events(path: Path) -> list[dict]:
    """The JSON lines of a transcript; lines that aren't JSON are skipped."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _snapshot(folder: Path) -> dict[str, list[int]]:
    """Every regular file under ``folder`` (not .ergo, not through symlinks): size and mtime."""
    found = {}
    for path in folder.rglob("*"):
        rel = path.relative_to(folder)
        if rel.parts[0] == META or path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        found[rel.as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_template(source: Path, target: Path) -> None:
    """Copy a template's regular files and folders; symlinks are left out."""

    def skip_links(folder, names):
        return [n for n in names if (Path(folder) / n).is_symlink()]

    shutil.copytree(source, target, ignore=skip_links, dirs_exist_ok=True)


def claude_entries(events: list[dict]) -> list[dict]:
    """What a Claude Code stream-json transcript did: text and tool calls."""
    entries = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                entries.append({"kind": "text", "text": block["text"], "at": None})
            elif block.get("type") == "tool_use":
                arg = block.get("input") or {}
                detail = next(
                    (
                        arg[k]
                        for k in ("command", "file_path", "path", "pattern", "url")
                        if isinstance(arg, dict) and arg.get(k)
                    ),
                    "",
                )
                entries.append(
                    {
                        "kind": "tool",
                        "text": f"{block.get('name', 'tool')} {detail}".strip(),
                        "at": None,
                    }
                )
    return entries


def codex_entries(events: list[dict]) -> list[dict]:
    """What a ``codex exec --json`` transcript did (item events)."""
    entries = []
    for event in events:
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if not item or event.get("type") != "item.completed":
            continue
        kind = item.get("type") or item.get("item_type") or ""
        if kind in ("agent_message", "assistant_message") and item.get("text"):
            entries.append({"kind": "text", "text": item["text"], "at": None})
        elif kind in ("command_execution", "file_change", "mcp_tool_call"):
            detail = item.get("command") or item.get("path") or item.get("tool") or ""
            entries.append({"kind": "tool", "text": f"{kind} {detail}", "at": None})
    return entries


class LocalShellError(ValueError):
    pass


class LocalShellPlugin(BotPlugin):
    name = "local_shell"
    description = (
        "Run agent CLIs (Claude Code, Codex) on tasks in job folders on this host"
    )

    def on_load(self) -> None:
        config = self.config
        self.root = Path(str(config.get("root") or "~/agent-jobs")).expanduser()
        template = config.get("template")
        self.template = Path(str(template)).expanduser() if template else None
        self.agents = tuple(config.get("agents") or ("claude", "codex"))
        self.executables = dict(config.get("executables") or {})
        self.command_prefix = [str(a) for a in config.get("command_prefix") or []]
        self.allowed_tools = list(config.get("allowed_tools") or DEFAULT_TOOLS)
        self.disallowed_tools = list(config.get("disallowed_tools") or [])
        self.permission_mode = str(config.get("permission_mode") or "dontAsk")
        self.codex_sandbox = str(config.get("codex_sandbox") or "workspace-write")
        self.max_turns = int(config.get("max_turns", 100))
        self.pass_env = tuple(config.get("pass_env") or DEFAULT_PASS_ENV)
        self.secrets = [str(s) for s in config.get("secrets") or []]
        if bad := sorted(set(self.secrets) & NEVER_PASS):
            msg = (
                f"local_shell won't pass {', '.join(bad)}: agents run on subscriptions"
            )
            raise ValueError(msg)
        if bad := sorted(s for s in self.secrets if is_protected_env(s)):
            msg = f"local_shell won't pass {', '.join(bad)}: reserved for private inference"
            raise ValueError(msg)
        self.timeout_seconds = float(config.get("timeout_minutes", 60)) * 60
        self.max_concurrent = int(config.get("max_concurrent", 2))
        self.approve = bool(config.get("approve", True))
        self.root_only = bool(config.get("root_only", False))
        self.poll_seconds = float(config.get("poll_seconds", 30))
        self.instructions = str(config.get("instructions") or "")
        self.manager = LocalShellAgents(self)

    # -- job folders ---------------------------------------------------------

    def folder(self, name: str) -> Path:
        name = str(name).strip()
        if not JOB_NAME.match(name):
            msg = f"{name!r} isn't a job name (letters, digits, . _ -; up to 80)"
            raise LocalShellError(msg)
        return self.root / name

    def runs(self, folder: Path) -> list[int]:
        return sorted(
            int(p.name[4:-5])
            for p in (folder / META).glob("run-*.json")
            if re.fullmatch(r"run-\d+\.json", p.name)
        )

    def run_state(self, folder: Path, number: int) -> dict:
        """One run: its spec, exit (when it ended) and whether its supervisor is alive."""
        meta = folder / META
        spec = _read_json(meta / f"run-{number}.json") or {}
        ended = _read_json(meta / f"run-{number}.exit.json")
        beat = meta / "heartbeat"
        last = beat.stat().st_mtime if beat.exists() else spec.get("started_at", 0)
        alive = ended is None and time.time() - float(last or 0) < STALE_SECONDS
        return {"spec": spec, "exit": ended, "alive": alive, "beat": last}

    def running(self, folder: Path) -> bool:
        runs = self.runs(folder)
        return bool(runs) and self.run_state(folder, runs[-1])["alive"]

    def active_jobs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and self.running(p)
        )

    def env(self, ctx) -> dict[str, str]:
        env = {
            k: os.environ[k]
            for k in self.pass_env
            if k in os.environ and k not in NEVER_PASS and not is_protected_env(k)
        }
        for name in self.secrets:
            if value := ctx.secret(name):
                env[name] = value
        return env

    def argv(
        self, agent: str, prompt: str, *, model: str, number: int, resume: str = ""
    ) -> list[str]:
        exe = str(self.executables.get(agent) or agent)
        system = JOB_PROMPT.format(folder="this folder")
        if self.instructions:
            system += "\n\n" + self.instructions
        if agent == "claude":
            argv = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose"]
            argv += ["--permission-mode", self.permission_mode]
            argv += ["--max-turns", str(self.max_turns)]
            argv += ["--append-system-prompt", system]
            if self.allowed_tools:
                argv += ["--allowedTools", ",".join(self.allowed_tools)]
            if self.disallowed_tools:
                argv += ["--disallowedTools", ",".join(self.disallowed_tools)]
            if model:
                argv += ["--model", model]
            if resume:
                argv += ["--resume", resume]
        elif agent == "codex":
            if resume:
                msg = "Codex jobs can't be resumed here; start a new run with the answer in the brief"
                raise LocalShellError(msg)
            argv = [exe, "exec", "--json", "--skip-git-repo-check"]
            argv += ["--sandbox", self.codex_sandbox]
            argv += ["--output-last-message", f"{META}/run-{number}.last.txt"]
            if model:
                argv += ["--model", model]
            argv.append(f"{system}\n\n{prompt}")
        else:
            msg = f"local_shell can run {', '.join(self.agents)}, not {agent!r}"
            raise LocalShellError(msg)
        return [*self.command_prefix, *argv]

    def launch(
        self, ctx, folder: Path, agent: str, argv: list[str], number: int
    ) -> None:
        meta = folder / META
        meta.mkdir(parents=True, exist_ok=True)
        (meta / f"run-{number}.json").write_text(
            json.dumps(
                {
                    "agent": agent,
                    "argv": argv,
                    "timeout_seconds": self.timeout_seconds,
                    "started_at": time.time(),
                    "before": _snapshot(folder),
                }
            ),
            encoding="utf-8",
        )
        (meta / "stop").unlink(missing_ok=True)
        (meta / "heartbeat").touch()
        subprocess.Popen(  # noqa: S603 — fixed interpreter and runner; argv from config
            [sys.executable, str(RUNNER), str(folder), str(number)],
            cwd=folder,
            env=self.env(ctx),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def start_agent(self, ctx, spec: AgentSpec) -> dict:
        folder = self.folder(spec.workspace)
        agent = spec.agent or self.manager.default_agent
        if agent not in self.agents:
            msg = f"local_shell runs {', '.join(self.agents)}, not {agent!r}"
            raise LocalShellError(msg)
        if self.running(folder):
            msg = f"Job {folder.name} is already running; wait for it or stop it first"
            raise LocalShellError(msg)
        active = self.active_jobs()
        if len(active) >= self.max_concurrent:
            msg = f"{len(active)} jobs are running ({', '.join(active)}); the limit is {self.max_concurrent}"
            raise LocalShellError(msg)
        if not folder.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            if self.template:
                _copy_template(self.template, folder)
            folder.mkdir(exist_ok=True)
        (folder / "brief.md").write_text(spec.brief, encoding="utf-8")
        number = (self.runs(folder) or [0])[-1] + 1
        prompt = "Do the task in brief.md.\n\n" + spec.brief
        argv = self.argv(agent, prompt, model=spec.model, number=number)
        self.launch(ctx, folder, agent, argv, number)
        return {"job": folder.name, "dir": str(folder), "agent": agent}

    # -- watching ------------------------------------------------------------

    def transcript(self, folder: Path, number: int) -> list[dict]:
        return _events(folder / META / f"run-{number}.jsonl")

    def entries(self, agent: str, events: list[dict]) -> list[dict]:
        return claude_entries(events) if agent == "claude" else codex_entries(events)

    def final_text(
        self, folder: Path, number: int, agent: str, events: list[dict]
    ) -> tuple[str, bool]:
        """The run's last message and whether the agent itself reported an error."""
        if agent == "claude":
            result = next(
                (e for e in reversed(events) if e.get("type") == "result"), None
            )
            if result is None:
                return "", True
            return str(result.get("result") or ""), bool(result.get("is_error"))
        path = folder / META / f"run-{number}.last.txt"
        text = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        return text, False

    def outputs(self, folder: Path, before: dict) -> list[dict]:
        found = []
        for rel, stat in sorted(_snapshot(folder).items()):
            if before.get(rel) == stat or rel == "brief.md":
                continue
            path = folder / rel
            found.append({"path": rel, "size": stat[0], "sha256": _sha256(path)})
            if len(found) >= MAX_OUTPUTS:
                break
        return found

    def session_id(self, folder: Path) -> str:
        for number in reversed(self.runs(folder)):
            for event in reversed(self.transcript(folder, number)):
                if event.get("session_id"):
                    return str(event["session_id"])
        return ""

    def record_usage(self, ctx, folder: Path, agent: str) -> None:
        """Claude Code's token counts from every run's result events, for Costs."""
        if agent != "claude" or ctx.session is None:
            return
        from django.utils import timezone

        from django_ergo.conversation.models import AgentUsage

        totals: dict[str, dict] = {}
        for number in self.runs(folder):
            events = self.transcript(folder, number)
            model = next(
                (
                    str(e.get("model"))
                    for e in events
                    if e.get("type") == "system" and e.get("model")
                ),
                "claude",
            )
            for event in events:
                if event.get("type") != "result" or not isinstance(
                    event.get("usage"), dict
                ):
                    continue
                usage = event["usage"]
                row = totals.setdefault(
                    model,
                    {"input": 0, "write": 0, "read": 0, "output": 0, "requests": 0},
                )
                row["input"] += int(usage.get("input_tokens") or 0)
                row["write"] += int(usage.get("cache_creation_input_tokens") or 0)
                row["read"] += int(usage.get("cache_read_input_tokens") or 0)
                row["output"] += int(usage.get("output_tokens") or 0)
                row["requests"] += int(event.get("num_turns") or 1)
        for model, row in totals.items():
            AgentUsage.objects.update_or_create(
                worker=ctx.worker,
                model=model,
                defaults={
                    "session": ctx.session,
                    "bot_name": ctx.worker.bot_name,
                    "source": "local_shell",
                    "agent": agent,
                    "input_tokens": row["input"],
                    "cache_write_tokens": row["write"],
                    "cache_read_tokens": row["read"],
                    "output_tokens": row["output"],
                    "requests": row["requests"],
                    "first_at": ctx.worker.created_at,
                    "last_at": timezone.now(),
                },
            )

    def check(self, ctx, handle: dict) -> AgentCheck:  # noqa: PLR0911 — one return per outcome
        folder = Path(handle["dir"])
        runs = self.runs(folder)
        if not runs:
            return AgentCheck("failed", error=f"Job {folder.name} has no runs")
        number = runs[-1]
        state = self.run_state(folder, number)
        agent = str(state["spec"].get("agent") or handle.get("agent") or "claude")
        events = self.transcript(folder, number)
        entries = self.entries(agent, events)
        ctx.activity(
            [{**e, "text": _clip(e["text"])} for e in entries[-ACTIVITY_ENTRIES:]],
            at=state["beat"] if state["alive"] else None,
            source="transcript",
            stall_after=600,
            checked_at=time.time(),
        )
        if state["exit"] is None:
            if state["alive"]:
                last = entries[-1]["text"] if entries else "starting"
                return AgentCheck("running", progress=_clip(last, 120))
            return AgentCheck(
                "failed",
                error=f"Job {folder.name}'s run stopped without finishing (Ergonaut restarted?)",
            )
        try:
            self.record_usage(ctx, folder, agent)
        except Exception:  # noqa: BLE001 — usage is best effort
            log.warning("Couldn't record usage for job %s", folder.name, exc_info=True)
        ended = state["exit"]
        text, agent_error = self.final_text(folder, number, agent, events)
        if ended.get("reason") in ("timeout", "stopped"):
            how = "timed out" if ended["reason"] == "timeout" else "was stopped"
            return AgentCheck(
                "failed", error=f"Job {folder.name} {how}. {_clip(text)}".strip()
            )
        if ended.get("code") != 0 or agent_error:
            err = folder / META / f"run-{number}.err"
            tail = (
                err.read_text(encoding="utf-8", errors="replace")[-800:]
                if err.exists()
                else ""
            )
            return AgentCheck(
                "failed",
                error=f"Job {folder.name} failed (exit {ended.get('code')}): {_clip(text or tail, 800)}",
            )
        if text.lstrip().startswith(QUESTION):
            body = text.lstrip()[len(QUESTION) :].strip()
            return AgentCheck(
                "running",
                progress="waiting for an answer",
                questions=[AgentQuestion(id=f"{folder.name}:{number}", body=body)],
            )
        return AgentCheck(
            "done",
            report={
                "job": folder.name,
                "folder": str(folder),
                "summary": text.strip(),
                "outputs": self.outputs(folder, state["spec"].get("before") or {}),
            },
        )

    def reply(self, ctx, handle: dict, question_id: str, text: str) -> str:
        folder = Path(handle["dir"])
        runs = self.runs(folder)
        if not runs or question_id != f"{folder.name}:{runs[-1]}":
            msg = f"{question_id} isn't this job's open question"
            raise LocalShellError(msg)
        if self.running(folder):
            msg = f"Job {folder.name} is already running"
            raise LocalShellError(msg)
        agent = str(handle.get("agent") or "claude")
        session = self.session_id(folder)
        if agent == "claude" and not session:
            msg = "Couldn't find the Claude session to resume"
            raise LocalShellError(msg)
        number = runs[-1] + 1
        argv = self.argv(agent, text, model="", number=number, resume=session)
        self.launch(ctx, folder, agent, argv, number)
        return f"Resumed job {folder.name} with the answer (run {number})."

    def stop(self, handle: dict) -> str:
        folder = Path(handle["dir"])
        if not self.running(folder):
            return f"Job {folder.name} isn't running"
        (folder / META / "stop").touch()
        return f"Asked job {folder.name} to stop"

    def worker_log(self, worker) -> dict | None:
        handle = (worker.args or {}).get("handle") or {}
        if not handle.get("dir"):
            return None
        folder = Path(handle["dir"])
        entries = []
        for number in self.runs(folder):
            spec = _read_json(folder / META / f"run-{number}.json") or {}
            agent = str(spec.get("agent") or "claude")
            entries += self.entries(agent, self.transcript(folder, number))
        return {
            "source": "transcript",
            "entries": [{**e, "text": _clip(e["text"], 4000)} for e in entries[-200:]],
        }

    # -- files ---------------------------------------------------------------

    def inside(self, folder: Path, path: str) -> Path:
        target = (folder / path).resolve()
        base = folder.resolve()
        if target != base and base not in target.parents:
            msg = f"{path} is outside job {folder.name}"
            raise LocalShellError(msg)
        if target.parts[len(base.parts) : len(base.parts) + 1] == (META,):
            msg = f"{path} is the job's own bookkeeping"
            raise LocalShellError(msg)
        return target

    def jobs(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        found = []
        for folder in sorted(self.root.iterdir()):
            runs = self.runs(folder) if folder.is_dir() else []
            if not runs:
                continue
            state = self.run_state(folder, runs[-1])
            status = (
                "running" if state["alive"] else ("ended" if state["exit"] else "died")
            )
            started = state["spec"].get("started_at")
            found.append(
                {
                    "job": folder.name,
                    "status": status,
                    "runs": len(runs),
                    "agent": state["spec"].get("agent"),
                    "started_at": datetime.fromtimestamp(started, UTC).isoformat()
                    if started
                    else None,
                }
            )
        return found

    def attach(self, ctx: ToolContext, job: str, path: str) -> dict:
        from django_ergo.conversation.attachments import save_session_file

        if ctx.session is None:
            msg = "Attachments need a chat"
            raise LocalShellError(msg)
        folder = self.folder(job)
        target = self.inside(folder, path)
        if is_secret(target.name):
            msg = f"{target.name} looks like a secret; it can't be attached"
            raise LocalShellError(msg)
        if not target.is_file():
            msg = f"No file {path} in job {job}"
            raise LocalShellError(msg)
        if target.stat().st_size > MAX_ATTACH_BYTES:
            msg = f"{path} is larger than {MAX_ATTACH_BYTES} bytes"
            raise LocalShellError(msg)
        row = save_session_file(
            ctx.session,
            target.name,
            target.read_bytes(),
            source="bot",
            metadata={
                "from_local_shell": {
                    "job": job,
                    "path": path,
                    "sha256": _sha256(target),
                }
            },
        )
        return {
            "id": str(row.id),
            "filename": row.filename,
            "media_type": row.media_type,
            "size": row.size,
        }

    def upload(
        self, ctx: ToolContext, job: str, files: list[str], folder: str = "inputs"
    ) -> list[dict]:
        from django_ergo.conversation.attachments import find_session_file

        if ctx.session is None:
            msg = "Uploading needs a chat"
            raise LocalShellError(msg)
        base = self.folder(job)
        base.mkdir(parents=True, exist_ok=True)
        target = self.inside(base, folder or ".")
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for ref in files:
            row = find_session_file(ctx.session, ref)
            name = Path(row.filename).name
            if is_secret(name) or row.size > MAX_ATTACH_BYTES:
                msg = f"{row.filename} can't be copied into a job"
                raise LocalShellError(msg)
            dest = target / name
            if dest.is_symlink():
                msg = f"Refusing to write {name} through a symlink"
                raise LocalShellError(msg)
            with row.file.open("rb") as handle:
                dest.write_bytes(handle.read())
            written.append(
                {"file": row.filename, "path": dest.relative_to(base).as_posix()}
            )
        return written

    # -- plugin hooks --------------------------------------------------------

    def _applies(self, ctx: ToolContext) -> bool:
        return not (self.root_only and ctx.bot and not ctx.bot.is_root(ctx.session))

    def agent_managers(self) -> dict[str, AgentManager]:
        return {self.manager.name: self.manager}

    @property
    def skill_requires(self) -> list[str]:
        return ["agents"]

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        return [FunctionToolkit(self._tools(), ctx)] if self._applies(ctx) else []

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if not self._applies(ctx):
            return []
        return [
            TextContextSource(
                "Local agent jobs",
                f"ergo_agent_start with manager local runs {', '.join(self.agents)} headless in a "
                f"job folder under {self.root}: workspace is the job name (e.g. 2026-w42-ads). "
                "The same name reuses the folder and its files. Put everything the agent needs "
                "in the brief, copy chat files in first with local_shell_upload, and say which "
                "files to make. It can't see this chat or ask mid-run; if it needs a decision "
                "it stops with a question that arrives here. Its report lists the files it "
                "made; show them to the person with local_shell_attach. "
                f"At most {self.max_concurrent} jobs run at once.",
            )
        ]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="local_shell_jobs",
            description="List job folders and whether each is running.",
        )
        def jobs() -> list[dict]:
            return plugin.jobs()

        @bot_tool(
            name="local_shell_attach",
            takes_context=True,
            description="Copy a file from a job folder into this chat as an attachment.",
            parameters={
                "job": {"type": "string", "description": "The job name"},
                "path": {"type": "string", "description": "Path inside the job folder"},
            },
            required=["job", "path"],
        )
        def attach(ctx: ToolContext, job: str, path: str) -> dict:
            return plugin.attach(ctx, job, path)

        @bot_tool(
            name="local_shell_upload",
            takes_context=True,
            description=(
                "Copy files from this chat into a job folder (default subfolder inputs), "
                "before starting the agent; name the paths in its brief."
            ),
            parameters={
                "job": {"type": "string", "description": "The job name"},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filenames or file ids",
                },
                "folder": {
                    "type": "string",
                    "description": 'Subfolder (default "inputs")',
                },
            },
            required=["job", "files"],
        )
        def upload(
            ctx: ToolContext, job: str, files: list[str], folder: str = "inputs"
        ) -> list[dict]:
            return plugin.upload(ctx, job, files, folder)

        return [t.__bot_tool__ for t in (jobs, attach, upload)]


class LocalShellAgents(AgentManager):
    """Agent CLIs in job folders on this host."""

    name = "local"
    default_agent = "claude"

    def __init__(self, plugin: LocalShellPlugin):
        self.plugin = plugin
        self.agents = plugin.agents
        self.default_agent = plugin.agents[0] if plugin.agents else "claude"
        self.requires_approval = plugin.approve
        self.poll_seconds = plugin.poll_seconds
        self.where = f"job folders under {plugin.root}"

    def available(self, ctx) -> bool:
        return self.plugin._applies(ctx)  # noqa: SLF001

    def start(self, ctx, spec: AgentSpec) -> dict:
        return self.plugin.start_agent(ctx, spec)

    def check(self, ctx, handle: dict) -> AgentCheck:
        return self.plugin.check(ctx, handle)

    def reply(self, ctx, handle: dict, question_id: str, text: str) -> str:
        return self.plugin.reply(ctx, handle, question_id, text)

    def stop(self, handle: dict) -> str:
        return self.plugin.stop(handle)

    def log(self, worker, handle: dict) -> dict | None:
        return self.plugin.worker_log(worker)
