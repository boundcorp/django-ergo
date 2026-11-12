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
        codex_network_access: true    # set false to disable network in workspace-write
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

import fcntl
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
from datetime import timedelta
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
from django_ergo.plugins._common import clip
from django_ergo.plugins._common import is_secret
from django_ergo.plugins._common import record_agent_usage
from django_ergo.protected_env import is_protected_env

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
USAGE_SECONDS = 300  # how often a running job's token usage is stored
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
MAX_ATTACH_BYTES = 50_000_000  # a job file attached to the chat
MAX_UPLOAD_BYTES = 200_000_000  # a chat file copied into a job (streamed)
MAX_OUTPUTS = 200
ACTIVITY_ENTRIES = 8
MAX_LISTED = 10
CLIP = 300
JOB_PROMPT = """\
You are working headless in a job folder: {folder}. Work only inside it and
write every result file there; the person reviews those files. Inputs and the
brief are in this folder (brief.md). Nobody can answer you while you run. If
you can't go on without a decision, stop and make your final message start
with "NEEDS_INPUT:" followed by the question and the options. Otherwise end
with a short summary: what you made (file paths), how, and anything that cost
money."""


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _runs(folder: Path) -> list[int]:
    return sorted(
        int(p.name[4:-5])
        for p in (folder / META).glob("run-*.json")
        if re.fullmatch(r"run-\d+\.json", p.name)
    )


def _run_state(folder: Path, number: int) -> dict:
    """One run: its spec, exit (when it ended) and whether its supervisor is alive."""
    meta = folder / META
    spec = _read_json(meta / f"run-{number}.json") or {}
    ended = _read_json(meta / f"run-{number}.exit.json")
    beat = meta / "heartbeat"
    last = beat.stat().st_mtime if beat.exists() else spec.get("started_at", 0)
    alive = ended is None and time.time() - float(last or 0) < STALE_SECONDS
    return {"spec": spec, "exit": ended, "alive": alive, "beat": last}


def job_running(folder: Path) -> bool:
    """Whether a job's supervisor is alive now. It runs inside the process (pod)
    that launched it, so a restart kills it; restarts wait for these."""
    runs = _runs(folder)
    return bool(runs) and _run_state(folder, runs[-1])["alive"]


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


TEMPLATE_MANIFEST = "template.json"


def _template_files(source: Path, target: Path):
    """(relative name, template file, job file) for each regular template file,
    skipping symlinks on either side, ``.ergo`` and folders the job replaced."""
    stack = [Path()]
    while stack:
        rel = stack.pop()
        dest_dir = target / rel
        if dest_dir.is_symlink() or (dest_dir.exists() and not dest_dir.is_dir()):
            continue
        for path in sorted((source / rel).iterdir()):
            name = rel / path.name
            if path.is_symlink() or name.parts[0] == META:
                continue
            if path.is_dir():
                stack.append(name)
                continue
            dest = target / name
            if (
                path.is_file()
                and not dest.is_symlink()
                and (not dest.exists() or dest.is_file())
            ):
                yield name.as_posix(), path, dest


def _sync_template(source: Path, target: Path) -> tuple[list[str], list[str]]:
    """Bring a job folder's copy of the template up to date: (added, updated).

    A file the job lacks is copied in. A file still exactly as the template
    last gave it (``.ergo/template.json`` keeps that file's hash) is replaced
    by the template's new version. A file the job changed is left alone, as
    is anything behind a symlink on either side. A job made before the
    manifest existed gets new files only, until its files match the template.
    """
    manifest_path = target / META / TEMPLATE_MANIFEST
    manifest = _read_json(manifest_path) or {}
    added, updated = [], []
    for key, path, dest in _template_files(source, target):
        new = _sha256(path)
        if dest.exists():
            have = _sha256(dest)
            if have == new:
                manifest[key] = new
                continue
            if manifest.get(key) != have:
                continue  # the job changed it (or it predates the manifest)
            updated.append(key)
        else:
            added.append(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        manifest[key] = new
    if manifest:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return sorted(added), sorted(updated)


def _warnings(notes: dict) -> list[str]:
    out = []
    if added := notes.get("template_added"):
        more = len(added) - MAX_LISTED
        shown = ", ".join(added[:MAX_LISTED]) + (
            f" and {more} more" if more > 0 else ""
        )
        out.append(f"Added new template files to the job: {shown}.")
    if updated := notes.get("template_updated"):
        more = len(updated) - MAX_LISTED
        shown = ", ".join(updated[:MAX_LISTED]) + (
            f" and {more} more" if more > 0 else ""
        )
        out.append(f"Updated template files the job hadn't changed: {shown}.")
    if missing := notes.get("missing_secrets"):
        out.append(
            f"Not set where this job was launched, so the job doesn't have them: "
            f"{', '.join(missing)}. If they were added recently, restart the "
            "processes that run tools and workers so they read the new environment."
        )
    return out


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


def _usage_row(totals: dict[str, dict], model: str) -> dict:
    return totals.setdefault(
        model,
        {
            "input": 0,
            "cache_write": 0,
            "cache_read": 0,
            "output": 0,
            "reasoning": 0,
            "requests": 0,
        },
    )


def _claude_run_usage(totals: dict[str, dict], events: list[dict]) -> None:
    model = next(
        (
            str(e["model"])
            for e in events
            if e.get("type") == "system" and e.get("model")
        ),
        "claude",
    )
    result = next(
        (
            e
            for e in reversed(events)
            if e.get("type") == "result" and isinstance(e.get("usage"), dict)
        ),
        None,
    )
    if result is not None:
        turns = int(result.get("num_turns") or 1)
        by_model = result.get("modelUsage")
        if isinstance(by_model, dict) and by_model:
            # Per model, sub-agents and side calls (Haiku) included.
            for name, usage in by_model.items():
                if not isinstance(usage, dict):
                    continue
                row = _usage_row(totals, str(name))
                row["input"] += int(usage.get("inputTokens") or 0)
                row["cache_write"] += int(usage.get("cacheCreationInputTokens") or 0)
                row["cache_read"] += int(usage.get("cacheReadInputTokens") or 0)
                row["output"] += int(usage.get("outputTokens") or 0)
            _usage_row(totals, model)["requests"] += turns
            return
        usage = result["usage"]
        row = _usage_row(totals, model)
        row["input"] += int(usage.get("input_tokens") or 0)
        row["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        row["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        row["output"] += int(usage.get("output_tokens") or 0)
        row["requests"] += turns
        return
    # No result yet (still running, or killed): the assistant messages so far.
    # stream-json repeats a message once per content block; keep its largest usage.
    messages: dict[str, tuple[str, dict]] = {}
    for event in events:
        message = event.get("message") if event.get("type") == "assistant" else None
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        key = str(message.get("id") or len(messages))
        usage = message["usage"]
        if key not in messages or sum(
            int(v or 0) for v in usage.values() if isinstance(v, int | float)
        ) > sum(
            int(v or 0) for v in messages[key][1].values() if isinstance(v, int | float)
        ):
            messages[key] = (str(message.get("model") or model), usage)
    for name, usage in messages.values():
        row = _usage_row(totals, name)
        row["input"] += int(usage.get("input_tokens") or 0)
        row["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        row["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        row["output"] += int(usage.get("output_tokens") or 0)
        row["requests"] += 1


def _codex_run_usage(totals: dict[str, dict], events: list[dict], spec: dict) -> None:
    argv = [str(a) for a in spec.get("argv") or []]
    model = argv[argv.index("--model") + 1] if "--model" in argv[:-1] else "codex"
    for event in events:
        usage = event.get("usage")
        if event.get("type") != "turn.completed" or not isinstance(usage, dict):
            continue
        cached = int(usage.get("cached_input_tokens") or 0)
        row = _usage_row(totals, model)
        row["input"] += max(0, int(usage.get("input_tokens") or 0) - cached)
        row["cache_read"] += cached
        row["output"] += int(usage.get("output_tokens") or 0)
        row["reasoning"] += int(usage.get("reasoning_output_tokens") or 0)
        row["requests"] += 1


def transcript_usage(
    agent: str, transcripts: list[list[dict]], specs: list[dict]
) -> dict[str, dict]:
    """Token totals per model from a worker's run transcripts: Claude Code's
    ``result`` events (``modelUsage`` when present), or its assistant messages
    while a run is going; ``codex exec --json``'s ``turn.completed`` events."""
    totals: dict[str, dict] = {}
    for events, spec in zip(transcripts, specs, strict=False):
        if agent == "claude":
            _claude_run_usage(totals, events)
        elif agent == "codex":
            _codex_run_usage(totals, events, spec)
    return {model: row for model, row in totals.items() if any(row.values())}


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
        self.codex_network_access: bool = config.get("codex_network_access", True)
        if not isinstance(self.codex_network_access, bool):
            msg = "local_shell codex_network_access must be a boolean"
            raise TypeError(msg)
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
            if "/" in name or "\\" in name:
                msg += (
                    f". A job is a folder under {self.root}, named, not a path: use a "
                    "short name like 2026-w45-sume-voice-pr, and put any other folder "
                    "the agent should work in (a repo checkout) in the brief"
                )
            raise LocalShellError(msg)
        return self.root / name

    def runs(self, folder: Path) -> list[int]:
        return _runs(folder)

    def run_state(self, folder: Path, number: int) -> dict:
        return _run_state(folder, number)

    def running(self, folder: Path) -> bool:
        return job_running(folder)

    def active_jobs(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and self.running(p)
        )

    def missing_secrets(self, ctx) -> list[str]:
        """Listed ``secrets`` this process can't see. Pods read their environment
        when they start, so a key added later is missing until they restart."""
        return [name for name in self.secrets if not ctx.secret(name)]

    def prepare(self, ctx, folder: Path) -> dict:
        """Bring a job folder up to date before a run: what was added and what's missing."""
        notes = {}
        if self.template and self.template.is_dir() and folder.is_dir():
            added, updated = _sync_template(self.template, folder)
            if added:
                notes["template_added"] = added
            if updated:
                notes["template_updated"] = updated
        if missing := self.missing_secrets(ctx):
            notes["missing_secrets"] = missing
        return notes

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
            network = str(self.codex_network_access).lower()
            argv += ["-c", f"sandbox_workspace_write.network_access={network}"]
            argv += ["--output-last-message", f"{META}/run-{number}.last.txt"]
            if model:
                argv += ["--model", model]
            argv.append(f"{system}\n\n{prompt}")
        else:
            msg = f"local_shell can run {', '.join(self.agents)}, not {agent!r}"
            raise LocalShellError(msg)
        return [*self.command_prefix, *argv]

    def launch(  # noqa: PLR0913
        self,
        ctx,
        folder: Path,
        agent: str,
        argv: list[str],
        number: int,
        *,
        question_id: str = "",
    ) -> None:
        meta = folder / META
        meta.mkdir(parents=True, exist_ok=True)
        spec = {
            "agent": agent,
            "argv": argv,
            "timeout_seconds": self.timeout_seconds,
            "started_at": time.time(),
            "before": _snapshot(folder),
        }
        if question_id:
            spec["question_id"] = question_id
        spec_path = meta / f"run-{number}.json"
        tmp = meta / f"run-{number}.json.tmp"
        tmp.write_text(json.dumps(spec), encoding="utf-8")
        tmp.replace(spec_path)
        try:
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
        except Exception as exc:
            (meta / f"run-{number}.err").write_text(str(exc), encoding="utf-8")
            exit_tmp = meta / f"run-{number}.exit.json.tmp"
            exit_tmp.write_text(
                json.dumps(
                    {"code": None, "reason": "launch_error", "ended_at": time.time()}
                ),
                encoding="utf-8",
            )
            exit_tmp.replace(meta / f"run-{number}.exit.json")
            raise

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
        notes = self.prepare(ctx, folder)
        (folder / "brief.md").write_text(spec.brief, encoding="utf-8")
        number = (self.runs(folder) or [0])[-1] + 1
        prompt = "Do the task in brief.md.\n\n" + spec.brief
        argv = self.argv(agent, prompt, model=spec.model, number=number)
        self.launch(ctx, folder, agent, argv, number)
        handle = {"job": folder.name, "dir": str(folder), "agent": agent, "run": number}
        if warnings := _warnings(notes):
            handle["warnings"] = warnings
        return handle

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

    def usage(self, worker, handle: dict) -> tuple[str, dict[str, dict], datetime]:
        """The agent's token totals per model for ``worker``'s own runs, read now.

        The agent's own session files under ``~/.claude`` or ``~/.codex`` come
        first: they hold every request (sub-agents too) with its model and time,
        and grow while the run is going. When they can't be read (an agent
        running as another user), the run transcripts are summed instead.
        Returns the agent, the totals and the end of the worker's span.
        """
        from django.utils import timezone

        from django_ergo.conversation.history import as_aware
        from django_ergo.conversation.history import db_timestamp
        from django_ergo.conversation.history import parse_timestamp
        from django_ergo.plugins import agent_usage_scan

        folder = Path(handle["dir"])
        runs, newer = self.own_runs(folder, handle)
        specs = [self.run_state(folder, number)["spec"] for number in runs]
        agent = str(
            next((s.get("agent") for s in specs if s.get("agent")), None)
            or handle.get("agent")
            or "claude"
        )
        until = as_aware(worker.completed_at or timezone.now())
        if newer is not None:
            started = self.run_state(folder, newer)["spec"].get("started_at")
            if started:
                until = min(until, datetime.fromtimestamp(float(started), UTC))
        since = as_aware(worker.created_at) - timedelta(minutes=1)
        scan = {
            "claude": agent_usage_scan.claude_usage,
            "codex": agent_usage_scan.codex_usage,
        }
        models: dict[str, dict] = {}
        if agent in scan:
            cwd = str(folder.resolve())
            try:
                models = scan[agent](Path.home(), cwd, since, until)
            except OSError:
                log.warning("Couldn't read %s session files for %s", agent, cwd)
        for totals in models.values():
            for key in ("first_at", "last_at"):
                totals[key] = db_timestamp(parse_timestamp(totals.get(key)))
        if not models:
            models = transcript_usage(
                agent, [self.transcript(folder, n) for n in runs], specs
            )
        return agent, models, db_timestamp(until)

    def record_usage(self, ctx, handle: dict, number: int, *, settled: bool) -> None:
        """Store the worker's agent usage for Costs and the chat's usage chart:
        every ``USAGE_SECONDS`` while it runs, and once more when it settles."""
        worker = getattr(ctx, "worker", None)
        if worker is None or getattr(ctx, "session", None) is None:
            return
        now = time.time()
        if settled:
            # Once per settled run, not on every poll while a question waits.
            if ctx.state.get("usage_settled") == number:
                return
            ctx.state["usage_settled"] = number
        elif now - float(ctx.state.get("usage_at") or 0) < USAGE_SECONDS:
            return
        ctx.state["usage_at"] = now
        try:
            self.store_usage(worker, handle)
        except Exception:  # noqa: BLE001 — usage is best effort
            log.warning("Couldn't record usage for worker %s", worker.pk, exc_info=True)

    def store_usage(
        self, worker, handle: dict, *, dry_run: bool = False
    ) -> dict[str, dict]:
        """Read the worker's usage now and, unless ``dry_run``, save it (its rows
        for models no longer seen are dropped). Returns the totals per model."""
        agent, models, until = self.usage(worker, handle)
        if models and not dry_run:
            record_agent_usage(
                worker,
                session=worker.session,
                source="local_shell",
                agent=agent,
                models=models,
                first_at=worker.created_at,
                last_at=until,
                replace=True,
            )
        return models

    def own_runs(self, folder: Path, handle: dict) -> tuple[list[int], int | None]:
        """The runs that belong to this worker, and the fresh run that replaced it.

        A worker owns the run it started and the runs that answered its
        questions (their spec has ``question_id``). Another ``ergo_agent_start``
        in the same folder starts a fresh run: that one belongs to a new
        worker, and this one stops following the folder there. Handles from
        before ``run`` was recorded follow the whole folder, as they used to.
        """
        runs = self.runs(folder)
        first = handle.get("run")
        if not isinstance(first, int) or first not in runs:
            return runs, None
        own = [first]
        for number in runs[runs.index(first) + 1 :]:
            if not self.run_state(folder, number)["spec"].get("question_id"):
                return own, number
            own.append(number)
        return own, None

    def check(self, ctx, handle: dict) -> AgentCheck:  # noqa: PLR0911 — one return per outcome
        folder = Path(handle["dir"])
        runs, newer = self.own_runs(folder, handle)
        if not runs:
            return AgentCheck("failed", error=f"Job {folder.name} has no runs")
        number = runs[-1]
        state = self.run_state(folder, number)
        self.record_usage(
            ctx,
            handle,
            number,
            settled=newer is not None
            or state["exit"] is not None
            or not state["alive"],
        )
        if newer is not None:
            # The folder's heartbeat is the newer run's now, so don't read it
            # as this run's, and don't show the newer run's transcript here.
            return self.superseded(folder, number, newer, state)
        agent = str(state["spec"].get("agent") or handle.get("agent") or "claude")
        events = self.transcript(folder, number)
        entries = self.entries(agent, events)
        ctx.activity(
            [{**e, "text": clip(e["text"], CLIP)} for e in entries[-ACTIVITY_ENTRIES:]],
            at=state["beat"] if state["alive"] else None,
            source="transcript",
            stall_after=600,
            checked_at=time.time(),
        )
        if state["exit"] is None:
            if state["alive"]:
                last = entries[-1]["text"] if entries else "starting"
                return AgentCheck("running", progress=clip(last, 120))
            return AgentCheck(
                "failed",
                error=f"Job {folder.name}'s run stopped without finishing: its supervisor died (Ergonaut restarted, or its pod ran out of memory)",
            )
        ended = state["exit"]
        text, agent_error = self.final_text(folder, number, agent, events)
        if ended.get("reason") == "killed":
            return AgentCheck(
                "failed",
                error=f"Job {folder.name}'s agent was killed (SIGKILL), most likely for "
                "running out of memory. Make the step that was running use less memory "
                "before starting it again.",
            )
        if ended.get("reason") in ("timeout", "stopped"):
            how = "timed out" if ended["reason"] == "timeout" else "was stopped"
            return AgentCheck(
                "failed", error=f"Job {folder.name} {how}. {clip(text, CLIP)}".strip()
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
                error=f"Job {folder.name} failed (exit {ended.get('code')}): {clip(text or tail, 800)}",
            )
        if text.lstrip().startswith(QUESTION):
            body = text.lstrip()[len(QUESTION) :].strip()
            if agent != "claude":
                action = (
                    "This run has exited and cannot be resumed. If authorized, start a "
                    "new run with the answer, existing artifacts and constraints in its brief."
                )
                return AgentCheck(
                    "done",
                    progress="Needs a new run; the agent process has exited",
                    questions=[
                        AgentQuestion(
                            id=f"{folder.name}:{number}",
                            body=body,
                            reply_supported=False,
                            action=action,
                        )
                    ],
                    report={
                        "job": folder.name,
                        "folder": str(folder),
                        "summary": text.strip(),
                        "status": "needs_new_run",
                        "resume_supported": False,
                        "action": action,
                        "outputs": self.outputs(
                            folder, state["spec"].get("before") or {}
                        ),
                    },
                )
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

    def superseded(
        self, folder: Path, number: int, newer: int, state: dict
    ) -> AgentCheck:
        """Close a worker whose job folder has a fresh run (``newer``) started
        by another worker, so it neither stays "running" nor repeats the newer
        run's question."""
        later = f"Run {newer} has since started in job {folder.name}"
        if state["exit"] is None:
            return AgentCheck(
                "failed",
                error=f"Job {folder.name}'s run {number} stopped without finishing. {later}.",
            )
        agent = str(state["spec"].get("agent") or "claude")
        text, agent_error = self.final_text(
            folder, number, agent, self.transcript(folder, number)
        )
        if state["exit"].get("code") != 0 or agent_error:
            return AgentCheck(
                "failed",
                error=f"Job {folder.name} failed (exit {state['exit'].get('code')}): {clip(text, 800)}",
            )
        if text.lstrip().startswith(QUESTION):
            text = ""  # the newer run took the question over
        return AgentCheck(
            "done",
            report={
                "job": folder.name,
                "folder": str(folder),
                "summary": f"{text.strip()}\n\n{later}; follow that worker.".strip(),
                "outputs": [],
                "superseded_by_run": newer,
            },
        )

    def reply(self, ctx, handle: dict, question_id: str, text: str) -> str:
        folder = Path(handle["dir"])
        meta = folder / META
        if not meta.is_dir():
            msg = f"{question_id} isn't this job's open question"
            raise LocalShellError(msg)
        # A lock in the shared job folder serializes replies across processes and
        # pods, not just threads in this process. The run spec is the durable
        # record of which question was consumed.
        with (meta / "reply.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            runs = self.runs(folder)
            for number in runs:
                state = self.run_state(folder, number)
                if state["spec"].get("question_id") == question_id:
                    if (state["exit"] or {}).get("reason") == "launch_error":
                        error = (meta / f"run-{number}.err").read_text(encoding="utf-8")
                        msg = f"Couldn't launch job {folder.name}: {error}"
                        raise LocalShellError(msg)
                    return f"Resumed job {folder.name} with the answer (run {number})."
            if not runs or question_id != f"{folder.name}:{runs[-1]}":
                msg = f"{question_id} isn't this job's open question"
                raise LocalShellError(msg)
            number = runs[-1]
            state = self.run_state(folder, number)
            if state["alive"]:
                msg = f"Job {folder.name} is already running"
                raise LocalShellError(msg)
            agent = str(state["spec"].get("agent") or handle.get("agent") or "claude")
            result, agent_error = self.final_text(
                folder, number, agent, self.transcript(folder, number)
            )
            if (
                not state["exit"]
                or state["exit"].get("code") != 0
                or state["exit"].get("reason") in ("timeout", "stopped")
                or agent_error
                or not result.lstrip().startswith(QUESTION)
            ):
                msg = f"{question_id} isn't this job's open question"
                raise LocalShellError(msg)
            if agent != "claude":
                msg = "Codex jobs can't be resumed here; start a new run with the answer in the brief"
                raise LocalShellError(msg)
            session = self.session_id(folder)
            if not session:
                msg = "Couldn't find the Claude session to resume"
                raise LocalShellError(msg)
            number += 1
            notes = self.prepare(ctx, folder)
            argv = self.argv(agent, text, model="", number=number, resume=session)
            self.launch(ctx, folder, agent, argv, number, question_id=question_id)
            done = f"Resumed job {folder.name} with the answer (run {number})."
            return " ".join([done, *_warnings(notes)])

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
        for number in self.own_runs(folder, handle)[0]:
            spec = _read_json(folder / META / f"run-{number}.json") or {}
            agent = str(spec.get("agent") or "claude")
            entries += self.entries(agent, self.transcript(folder, number))
        return {
            "source": "transcript",
            "entries": [{**e, "text": clip(e["text"], 4000)} for e in entries[-200:]],
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

    def _upload_file(self, ctx: ToolContext, ref: str, session_id: str):
        import uuid

        from django_ergo.bots.messaging import not_shared_error
        from django_ergo.bots.messaging import shared_with
        from django_ergo.conversation.attachments import find_session_file

        try:
            row = find_session_file(ctx.session, ref)
        except ValueError as not_found:
            try:
                file_id = str(uuid.UUID(str(ref).strip()))
            except ValueError:
                raise not_found from None
            if not shared_with(ctx.session, file_id):
                raise not_shared_error(ref) from None
            attachments = ctx.bot.plugin("attachments") if ctx.bot else None
            if attachments is None:
                msg = "Reading a shared file requires the attachments plugin"
                raise ValueError(msg) from None
            row = attachments.file_for(ctx, file_id)
        if session_id and str(row.session_id) != session_id:
            msg = f"No file {ref!r} in that session"
            raise ValueError(msg)
        return row

    def upload(
        self,
        ctx: ToolContext,
        job: str,
        files: list[str],
        folder: str = "inputs",
        session_id: str = "",
    ) -> list[dict]:
        from django_ergo.bots.registry import may_use

        if ctx.session is None:
            msg = "Uploading needs a chat"
            raise LocalShellError(msg)
        if (ctx.user is not None and ctx.user.pk != ctx.session.user_id) or (
            ctx.bot is not None
            and (
                ctx.bot.name != ctx.session.bot_name
                or not may_use(ctx.bot, ctx.session.user)
            )
        ):
            msg = "Not authorized for this chat"
            raise LocalShellError(msg)
        base = self.folder(job)
        base.mkdir(parents=True, exist_ok=True)
        target = self.inside(base, folder or ".")
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for ref in files:
            row = self._upload_file(ctx, ref, session_id)
            name = Path(row.filename).name
            if is_secret(name):
                msg = f"{row.filename} can't be copied into a job"
                raise LocalShellError(msg)
            if row.size > MAX_UPLOAD_BYTES:
                msg = f"{row.filename} is larger than {MAX_UPLOAD_BYTES} bytes"
                raise LocalShellError(msg)
            dest = target / name
            if dest.is_symlink():
                msg = f"Refusing to write {name} through a symlink"
                raise LocalShellError(msg)
            with row.file.open("rb") as handle, dest.open("wb") as out:
                shutil.copyfileobj(handle, out, 1024 * 1024)
            written.append(
                {"file": row.filename, "path": dest.relative_to(base).as_posix()}
            )
        return written

    # -- plugin hooks --------------------------------------------------------

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
                "Copy this chat's files, or files explicitly shared with it by ID, "
                "into a job folder (default inputs) before starting the agent."
            ),
            parameters={
                "job": {"type": "string", "description": "The job name"},
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Current-chat filenames or IDs; shared files by ID",
                },
                "folder": {
                    "type": "string",
                    "description": 'Subfolder (default "inputs")',
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional source session ID; all files must belong to it",
                },
            },
            required=["job", "files"],
        )
        def upload(
            ctx: ToolContext,
            job: str,
            files: list[str],
            folder: str = "inputs",
            session_id: str = "",
        ) -> list[dict]:
            return plugin.upload(ctx, job, files, folder, session_id)

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

    def validate_reply(self, handle: dict, question_id: str) -> None:
        folder = Path(handle["dir"])
        runs, _ = self.plugin.own_runs(folder, handle)
        if runs and question_id == f"{folder.name}:{runs[-1]}":
            state = self.plugin.run_state(folder, runs[-1])
            agent = str(state["spec"].get("agent") or handle.get("agent") or "claude")
            if agent != "claude":
                msg = "Codex jobs can't be resumed here; start a new run with the answer, existing artifacts and constraints in the brief"
                raise LocalShellError(msg)

    def reply(self, ctx, handle: dict, question_id: str, text: str) -> str:
        return self.plugin.reply(ctx, handle, question_id, text)

    def stop(self, handle: dict) -> str:
        return self.plugin.stop(handle)

    def log(self, worker, handle: dict) -> dict | None:
        return self.plugin.worker_log(worker)

    def record_usage(
        self, worker, handle: dict, *, dry_run: bool = False
    ) -> dict[str, dict] | None:
        return self.plugin.store_usage(worker, handle, dry_run=dry_run)
