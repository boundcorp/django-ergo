"""Orca plugin: manage Orca worktrees, terminals and workers with the Orca CLI.

    plugins:
      - name: orca
        environment: devbox        # pin every call to this Orca environment
        executable: orca-ide       # default: orca-ide if installed, else orca
        approve_changes: true      # orca_run waits for the user's approval
        root_only: true            # only the root session gets these tools
        timeout: 120               # seconds per command
        files_host: devbox         # ssh host holding the worktrees (default: the environment;
                                   # "" reads them on this host)
        max_attach_bytes: 20000000
        agents: [codex, claude, omp]  # the agents ergo_agent_start may start here
        worker_poll_seconds: 120   # how often an agent's watcher checks it
        usage_minutes: 10          # minimum minutes between agent-session usage scans
        stall_minutes: 10          # a running worker with no new output this long shows as stalled

The bot runs the CLI on the host Ergonaut runs on, as that user. Tools:

- ``orca_read``: read-only commands, no approval: ``status``, ``worktree
  ps|list|show|current``, ``terminal list|read|show``, ``repo
  list|show|search-refs``, ``orchestration run-list|worker-list|worker-read|
  worker-show``, ``search``, ``skills get`` (the CLI's own guides) and
  ``<subcommand> --help``.
- ``orca_screenshot``: capture the browser tab (in a worktree, or by page
  id) and attach the image to the chat. No approval: it only looks.
- ``orca_attach``: copy a file from an Orca worktree into the chat, as an
  attachment (screenshots, reports, logs a worker wrote). The path must stay
  inside the worktree (symlinks resolved), and secret-looking files (``.env*``,
  ``*.pem``, ``*.key``, anything named like a secret) are refused. Worktree
  files are read over ``ssh <files_host>``.
- ``orca_upload``: copy chat files (this chat's, or files another chat shared,
  by ``session_id``) into a folder of an Orca worktree, so a worker can use
  them (design mockups, specs). Same approval as ``orca_run``; the folder must
  stay inside the worktree, and secret-looking names are refused.
- An agent manager named ``orca`` (see bots.agents): ``ergo_agent_start``
  starts a supervised coding agent on a task as an Orca worker (approval),
  watched by a thread Worker (``agent:orca``) that polls the dispatch, passes
  the agent's questions to the chat (``ergo_agent_reply`` answers them with
  ``orchestration reply``) and brings its ``worker_done`` report back as a
  message. ``orca_start_worker`` does the same with Orca's parameter names. Each chat gets its own Orca Run
  and mailbox terminal, made on first use. Each check also reads the agent's
  latest output (``worker-read``: its transcript, or its terminal) into the
  worker's activity, which Ergonaut's worker cards show with the time since it
  last did something; ``worker_log`` reads the whole recent log on demand.
- The watcher also reads the coding agent's own session files from
  ``files_host`` (or this host) every ``usage_minutes`` and when it settles.
  It records Claude Code, Codex, and omp token counts per worker for Costs;
  scanning is best effort and never changes the worker outcome.
- ``orca_run``: every other command (creating worktrees, starting and
  stopping workers, sending to terminals...). Each call needs approval unless
  ``approve_changes: false``.

Arguments are passed as a list, never through a shell. ``--json`` is added
where the CLI accepts it, and with ``environment`` set the bot can't point a
call anywhere else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
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
from django_ergo.plugins.bash import trim

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

READ_ONLY = {
    ("status",),
    ("worktree", "ps"),
    ("worktree", "list"),
    ("worktree", "show"),
    ("worktree", "current"),
    ("terminal", "list"),
    ("terminal", "wait"),  # waits for a terminal's state; changes nothing
    ("terminal", "read"),
    ("terminal", "show"),
    ("repo", "list"),
    ("repo", "show"),
    ("repo", "search-refs"),
    ("orchestration", "run-list"),
    ("orchestration", "worker-list"),
    ("orchestration", "worker-read"),
    # Looking at the browser (no clicks, typing or navigation).
    ("tab", "list"),
    ("tab", "show"),
    ("tab", "current"),
    ("snapshot",),
    ("get",),
    ("is",),
    ("orchestration", "worker-show"),
    ("search",),
    ("skills", "get"),
}
NO_JSON = {("skills", "get")}
MAX_OUTPUT_CHARS = 40_000  # about 10k tokens; a 26-worktree list is ~28k compact
TARGET_FLAGS = ("--environment", "--pairing-code")
ARGS_SCHEMA = {
    "args": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            'CLI arguments after the executable, e.g. ["worktree", "list"] or '
            '["terminal", "read", "--terminal", "<handle>"]'
        ),
    },
    "fields": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Optional: keep only these keys in each item of the JSON lists, "
            'e.g. ["id", "path", "branch"] for a short worktree list'
        ),
    },
}


def keep_fields(value, fields: set[str]):
    """Drop every key not in ``fields`` from the dicts inside lists."""
    if isinstance(value, list):
        return [
            {k: v for k, v in item.items() if k in fields}
            if isinstance(item, dict)
            else keep_fields(item, fields)
            for item in value
        ]
    if isinstance(value, dict):
        return {k: keep_fields(v, fields) for k, v in value.items()}
    return value


def compact(stdout: str, fields: list[str] | None = None) -> str:
    """The CLI's JSON without indentation (and narrowed to ``fields``)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    if fields:
        data = keep_fields(data, set(fields))
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def command_of(args: list[str]) -> tuple[str, ...]:
    """The subcommand words, e.g. ("orchestration", "worker-list")."""
    words = []
    for arg in args:
        if arg.startswith("-"):
            break
        words.append(arg)
    return tuple(words[:2])


def is_read_only(args: list[str]) -> bool:
    # Help only as `<subcommand words> --help`: anywhere else "--help" or "-h"
    # could be the value of a flag (`--text -h`), and the command would run.
    if args and args[-1] == "--help" and not any(a.startswith("-") for a in args[:-1]):
        return True
    command = command_of(args)
    return any(command[: len(known)] == known for known in READ_ONLY)


def is_secret(name: str) -> bool:
    name = name.lower()
    return (
        name.startswith((".env", "id_rsa", "id_ed25519"))
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or "secret" in name
        or "credential" in name
    )


POLL_SECONDS = 120  # how often a watched Orca worker is checked (worker_poll_seconds)
SETTLED = ("completed", "failed", "cancelled", "abandoned")
# An agent terminal that has exited or vanished for this long, with no worker_done,
# fails the worker (Orca keeps such a dispatch "dispatched" indefinitely).
TERMINAL_GONE_GRACE_SECONDS = 300
STALL_MINUTES = 10  # no new output for this long: the card says stalled (stall_minutes)
EPOCH_MS_FROM = 1e11  # epoch times bigger than this are milliseconds
ACTIVITY_ENTRIES = 8  # entries kept on the worker for its card
ACTIVITY_CHARS = 300  # per entry on the card
LOG_CHARS = 4000  # per entry in the full log
# The full log's screen lines (a transcript stops at Orca's 50 messages).
USAGE_MINUTES = 10  # session-file scan interval (usage_minutes)
log = logging.getLogger(__name__)
LOG_LINES = 400
ANSI = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()#][0-9A-Za-z]|[@-Z\\-_])"
)
# Tool inputs that say what a call did, best first (Bash's command, Read's path...).
INPUT_KEYS = (
    "command",
    "cmd",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tool_input(value) -> str:
    """A tool call's input in a line: its telling field, else compact JSON."""
    if isinstance(value, dict):
        for key in INPUT_KEYS:
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
    if value in (None, {}, []):
        return ""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _block_text(block) -> str:
    """A transcript block's text (text blocks, or a result's output)."""
    if not isinstance(block, dict):
        return str(block)
    for key in ("text", "output", "content"):
        value = block.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(_block_text(v) for v in value)
    return ""


def _epoch(value) -> float | None:
    """Seconds since the epoch from Orca's times: epoch ms/s numbers or ISO text."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int | float):
        return value / 1000 if value > EPOCH_MS_FROM else float(value)
    try:
        when = datetime.fromisoformat(
            str(value).strip().replace(" ", "T").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)  # SQLite's datetime('now') is UTC
    return when.timestamp()


def output_entries(read: dict, chars: int = LOG_CHARS) -> tuple[str, list[dict]]:
    """``orchestration worker-read`` as (source, entries), oldest first. An entry is
    ``{"kind", "text", "at"}``: kind is the speaker (assistant, user, reasoning),
    ``tool`` for a call, ``result`` or ``error`` for its output, or ``terminal``
    for a screen line; ``at`` is epoch seconds when the source has a time."""
    transcript = read.get("transcript")
    if isinstance(transcript, dict):
        entries = []
        for message in transcript.get("messages") or []:
            role = str(message.get("role") or "assistant")
            at = _epoch(message.get("timestamp"))
            for block in message.get("blocks") or []:
                kind = block.get("type") if isinstance(block, dict) else "text"
                if kind == "tool-call":
                    name = str(block.get("name") or "tool")
                    text = f"{name} {_tool_input(block.get('input'))}".strip()
                    entries.append(
                        {"kind": "tool", "text": _clip(text, chars), "at": at}
                    )
                elif kind == "tool-result":
                    text = _block_text(block)
                    entries.append(
                        {
                            "kind": "error" if block.get("isError") else "result",
                            "text": _clip(text, chars),
                            "at": at,
                        }
                    )
                elif kind == "text":
                    text = _block_text(block)
                    if text.strip():
                        entries.append(
                            {"kind": role, "text": _clip(text, chars), "at": at}
                        )
        return "transcript", entries
    terminal = read.get("terminal") or {}
    lines = [ANSI.sub("", str(line)).rstrip() for line in terminal.get("tail") or []]
    entries = [
        {"kind": "terminal", "text": _clip(line, chars), "at": None}
        for line in lines
        if line.strip()
    ]
    return "terminal", entries


def _first_key(value, keys: tuple[str, ...]):
    """The first non-empty string under any of ``keys`` in a JSON value (receipts nest them)."""
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return None
    for item in values:
        if found := _first_key(item, keys):
            return found
    return None


def _first_path(value):
    """The first "path" string in a JSON value (worktree show nests it)."""
    if isinstance(value, dict):
        if isinstance(value.get("path"), str):
            return value["path"]
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return None
    for item in values:
        if found := _first_path(item):
            return found
    return None


class OrcaPlugin(BotPlugin):
    name = "orca"
    description = "Manage Orca worktrees, terminals and workers"

    def on_load(self) -> None:
        self.environment = str(self.config.get("environment") or "")
        self.executable = str(
            self.config.get("executable")
            or ("orca-ide" if shutil.which("orca-ide") else "orca")
        )
        self.approve_changes = bool(self.config.get("approve_changes", True))
        self.root_only = bool(self.config.get("root_only", True))
        self.timeout = int(self.config.get("timeout", 120))
        self.files_host = str(self.config.get("files_host", self.environment) or "")
        self.usage_minutes = float(self.config.get("usage_minutes", USAGE_MINUTES))
        self.max_attach_bytes = int(self.config.get("max_attach_bytes", 20_000_000))
        self.poll_seconds = float(self.config.get("worker_poll_seconds", POLL_SECONDS))
        self.stall_minutes = float(self.config.get("stall_minutes", STALL_MINUTES))
        self.manager = OrcaAgents(self)

    def argv(self, args: list[str]) -> list[str]:
        args = [str(a) for a in args]
        if not args:
            msg = "Give the Orca command to run, e.g. ['status']"
            raise ValueError(msg)
        if self.environment:
            if any(a.split("=")[0] in TARGET_FLAGS for a in args):
                msg = f"This bot only manages the {self.environment!r} environment."
                raise ValueError(msg)
            args += ["--environment", self.environment]
        if "--json" not in args and command_of(args) not in NO_JSON:
            args.append("--json")
        return [self.executable, *args]

    def run(self, args: list[str], fields: list[str] | None = None) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 — argv list, no shell
                self.argv(args),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            return f"The Orca CLI ({self.executable}) is not installed on this host."
        except subprocess.TimeoutExpired:
            return f"Timed out after {self.timeout}s."
        if proc.returncode:
            output = f"Exit {proc.returncode}:\n{(proc.stdout + proc.stderr).strip()}"
            return trim(output, MAX_OUTPUT_CHARS)
        return (
            trim(compact(proc.stdout.strip(), fields), MAX_OUTPUT_CHARS)
            or "(no output)"
        )

    def read(self, args: list[str], fields: list[str] | None = None) -> str:
        if not is_read_only(args):
            return (
                f"{' '.join(command_of(args)) or args!r} can change Orca state; "
                "use orca_run, which asks the user first."
            )
        return self.run(args, fields)

    # -- attaching worktree files -------------------------------------------

    def worktree_path(self, worktree: str) -> str:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            self.argv(["worktree", "show", "--worktree", worktree]),
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        found = (
            _first_path(json.loads(proc.stdout))
            if proc.returncode == 0 and proc.stdout.strip()
            else None
        )
        if not found:
            msg = f"No worktree {worktree!r}: {(proc.stderr or proc.stdout).strip()[:300]}"
            raise ValueError(msg)
        return found

    def fetch(self, root: str, path: str) -> tuple[str, bytes]:
        """A file inside the worktree at ``root``: its resolved path and bytes."""
        import posixpath
        import shlex

        target = posixpath.normpath(
            path if path.startswith("/") else posixpath.join(root, path)
        )
        if not target.startswith(root.rstrip("/") + "/"):
            msg = f"{path} is outside the worktree"
            raise ValueError(msg)
        if is_secret(posixpath.basename(target)):
            msg = f"{posixpath.basename(target)} looks like a secret; it can't be attached"
            raise ValueError(msg)
        limit = self.max_attach_bytes
        script = (
            f"set -e; r=$(realpath -e -- {shlex.quote(root)}); f=$(realpath -e -- {shlex.quote(target)}); "
            'case "$f" in "$r"/*) ;; *) echo "outside the worktree" >&2; exit 3;; esac; '
            'test -f "$f" || { echo "not a file" >&2; exit 4; }; '
            f'printf "%s\\0" "$f"; head -c {limit + 1} -- "$f"'
        )
        argv = (
            ["ssh", "-o", "BatchMode=yes", self.files_host, script]
            if self.files_host
            else ["sh", "-c", script]
        )
        proc = subprocess.run(  # noqa: S603 — fixed script; paths are shell-quoted
            argv, capture_output=True, timeout=self.timeout, check=False
        )
        if proc.returncode:
            msg = f"Couldn't read {path}: {proc.stderr.decode(errors='replace').strip()[:300]}"
            raise ValueError(msg)
        resolved, _, data = proc.stdout.partition(b"\0")
        if len(data) > limit:
            msg = f"{path} is larger than {limit} bytes"
            raise ValueError(msg)
        resolved = resolved.decode(errors="replace")
        if is_secret(posixpath.basename(resolved)):  # a symlink to a secret
            msg = f"{path} points at a secret-looking file; it can't be attached"
            raise ValueError(msg)
        return resolved, data

    def screenshot(
        self,
        ctx: ToolContext,
        worktree: str = "",
        page: str = "",
        image_format: str = "png",
    ) -> dict:
        """Capture a browser tab and attach the image to the chat."""
        import base64
        from datetime import UTC
        from datetime import datetime

        from django_ergo.conversation.attachments import save_session_file

        if ctx.session is None:
            msg = "Screenshots go into a chat"
            raise ValueError(msg)
        image_format = "jpeg" if image_format in ("jpg", "jpeg") else "png"
        args = ["screenshot", "--format", image_format]
        if worktree:
            args += ["--worktree", worktree]
        if page:
            args += ["--page", page]
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            self.argv(args),
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = {}
        data = (payload.get("result") or {}).get("data") if payload.get("ok") else None
        if not data:
            error = (payload.get("error") or {}).get("message") or (
                proc.stderr or proc.stdout
            ).strip()[:500]
            msg = f"Screenshot failed: {error}"
            raise ValueError(msg)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        row = save_session_file(
            ctx.session,
            f"screenshot-{stamp}.{'jpg' if image_format == 'jpeg' else 'png'}",
            base64.b64decode(data),
            source="bot",
            metadata={
                "from_orca": {"screenshot": {"worktree": worktree, "page": page}}
            },
        )
        return {"id": str(row.id), "filename": row.filename, "size": row.size}

    def attach(
        self, ctx: ToolContext, worktree: str, path: str, filename: str = ""
    ) -> dict:
        import posixpath

        from django_ergo.conversation.attachments import save_session_file

        if ctx.session is None:
            msg = "Attachments need a chat"
            raise ValueError(msg)
        resolved, data = self.fetch(self.worktree_path(worktree), path)
        row = save_session_file(
            ctx.session,
            filename or posixpath.basename(resolved),
            data,
            source="bot",
            metadata={"from_orca": {"worktree": worktree, "path": resolved}},
        )
        return {
            "id": str(row.id),
            "filename": row.filename,
            "media_type": row.media_type,
            "size": row.size,
        }

    def push(self, root: str, folder: str, name: str, data: bytes) -> str:
        """Write ``data`` as ``folder/name`` inside the worktree at ``root``; returns its path."""
        import posixpath
        import shlex

        if "/" in name or name in ("", ".", "..") or is_secret(name):
            msg = f"{name!r} can't be written to a worktree"
            raise ValueError(msg)
        target = posixpath.normpath(
            folder if folder.startswith("/") else posixpath.join(root, folder or ".")
        )
        if target != root.rstrip("/") and not target.startswith(root.rstrip("/") + "/"):
            msg = f"{folder} is outside the worktree"
            raise ValueError(msg)
        script = (
            f"set -e; r=$(realpath -e -- {shlex.quote(root)}); "
            f"mkdir -p -- {shlex.quote(target)}; d=$(realpath -e -- {shlex.quote(target)}); "
            'case "$d" in "$r"|"$r"/*) ;; *) echo "outside the worktree" >&2; exit 3;; esac; '
            f'f="$d"/{shlex.quote(name)}; '
            'test -L "$f" && { echo "refusing to write through a symlink" >&2; exit 5; }; '
            'cat > "$f"; printf "%s" "$f"'
        )
        argv = (
            ["ssh", "-o", "BatchMode=yes", self.files_host, script]
            if self.files_host
            else ["sh", "-c", script]
        )
        proc = subprocess.run(  # noqa: S603 — fixed script; paths are shell-quoted
            argv, input=data, capture_output=True, timeout=self.timeout, check=False
        )
        if proc.returncode:
            msg = f"Couldn't write {name}: {proc.stderr.decode(errors='replace').strip()[:300]}"
            raise ValueError(msg)
        return proc.stdout.decode(errors="replace").strip()

    def upload(
        self,
        ctx: ToolContext,
        worktree: str,
        files: list[str],
        folder: str = "design",
        session_id: str = "",
    ) -> list[dict]:
        """Copy chat files into ``folder`` of a worktree."""
        from django_ergo.conversation.attachments import find_session_file
        from django_ergo.conversation.models import ConversationSession

        if ctx.session is None:
            msg = "Uploading needs a chat"
            raise ValueError(msg)
        source = ctx.session
        if session_id and session_id != str(ctx.session.id):
            source = ConversationSession.objects.filter(
                id=session_id, user_id=ctx.session.user_id
            ).first()
            if source is None:
                msg = f"No chat {session_id} of yours"
                raise ValueError(msg)
        rows = [find_session_file(source, ref) for ref in files]
        root = self.worktree_path(worktree)
        written = []
        for row in rows:
            if row.size > self.max_attach_bytes:
                msg = f"{row.filename} is larger than {self.max_attach_bytes} bytes"
                raise ValueError(msg)
            with row.file.open("rb") as handle:
                data = handle.read()
            path = self.push(root, folder, row.filename, data)
            written.append({"file": row.filename, "path": path, "size": len(data)})
        return written

    # -- supervised Orca workers as thread Workers ---------------------------

    def cli_json(self, args: list[str]) -> dict:
        """Run a CLI command and return its JSON result; raise with Orca's error."""
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            self.argv(args),
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = {}
        if proc.returncode or payload.get("ok") is False:
            error = (payload.get("error") or {}).get("message") or (
                proc.stderr or proc.stdout
            ).strip()[:600]
            msg = f"orca {' '.join(command_of(args))} failed: {error}"
            raise ValueError(msg)
        return payload.get("result", payload)

    def mailbox(self, ctx: ToolContext, worktree: str) -> tuple[str, str]:
        """This chat's Orca mailbox terminal and Run, created on first use and kept in
        the session's metadata. Workers report to the Run; Orca needs a terminal to
        bind the Run to (a remote run-create otherwise binds whatever is active)."""
        from django_ergo.conversation.models import ConversationSession

        session = ctx.session
        meta = dict(session.metadata or {})
        place = self.environment or "local"
        saved = (meta.get("orca") or {}).get(place) or {}
        if saved.get("mailbox") and saved.get("run"):
            return saved["mailbox"], saved["run"]
        title = (meta.get("title") or f"{ctx.bot.name} chat")[:60]
        terminal = self.cli_json(
            [
                "terminal",
                "create",
                "--worktree",
                worktree,
                "--title",
                f"Ergo {ctx.bot.name} mailbox: {title}",
            ]
        )
        handle = _first_key(terminal, ("handle", "terminalHandle"))
        if not handle:
            msg = f"Orca made no mailbox terminal: {json.dumps(terminal)[:300]}"
            raise ValueError(msg)
        run = self.cli_json(
            [
                "orchestration",
                "run-create",
                "--objective",
                f"{ctx.bot.name}: {title}",
                "--from",
                handle,
            ]
        )
        run_id = _first_key(run, ("runId", "run_id", "id"))
        orca = dict(meta.get("orca") or {})
        orca[place] = {"mailbox": handle, "run": run_id}
        meta["orca"] = orca
        ConversationSession.objects.filter(pk=session.pk).update(metadata=meta)
        session.metadata = meta
        return handle, run_id

    def resolved_worktree(self, selector: str) -> tuple[str, str | None]:
        """Resolve the stable Orca selector and, when encoded, its host path."""
        if selector.startswith("id:"):
            _, separator, path = selector.partition("::")
            return selector, path if separator and path.startswith("/") else None
        try:
            shown = self.cli_json(["worktree", "show", "--worktree", selector])
        except (OSError, subprocess.TimeoutExpired, ValueError):
            log.warning(
                "Couldn't resolve worktree path for usage capture", exc_info=True
            )
            return selector, None
        worktree_id = _first_key(shown, ("id",))
        if not worktree_id:
            msg = f"No worktree {selector!r}"
            raise ValueError(msg)
        return f"id:{worktree_id}", _first_path(shown)

    def pin_omp_model(self, worktree: str, model: str, effort: str = "") -> None:
        """Make omp in this worktree use ``model`` (at ``effort``): a project
        ``.omp/config.yml`` overrides its default model role. The folder ignores
        itself, so the setting never shows up in git."""
        root = self.worktree_path(worktree)
        selector = f"{model}:{effort}" if effort else model
        config = f"modelRoles:\n  default: {json.dumps(selector)}\n"
        self.push(root, ".omp", "config.yml", config.encode())
        self.push(root, ".omp", ".gitignore", b"*\n")

    def dispatch(  # noqa: PLR0913
        self,
        ctx: ToolContext,
        spec: str,
        worktree: str,
        agent: str,
        title: str,
        model: str = "",
        effort: str = "",
    ) -> tuple[str, str, dict]:
        """Create the task and start its worker: (task id, run id, receipt)."""
        mailbox, run_id = self.mailbox(ctx, worktree)
        task = self.cli_json(
            [
                "orchestration",
                "task-create",
                "--spec",
                spec,
                "--task-title",
                title,
                "--run",
                run_id,
                "--from",
                mailbox,
            ]
        )
        task_id = _first_key(task, ("taskId", "task_id", "id"))
        args = [
            "orchestration",
            "worker-start",
            "--task",
            task_id,
            "--worktree",
            worktree,
            "--agent",
            agent,
            "--run",
            run_id,
            "--from",
            mailbox,
        ]
        if agent == "omp" and model:
            # Orca can't pass a model to omp at launch; omp reads it from the worktree.
            self.pin_omp_model(worktree, model, effort)
        elif agent == "omp" and effort:
            msg = "omp takes effort only together with a model"
            raise ValueError(msg)
        else:
            if model:
                args += ["--model", model]
            if effort:
                args += ["--effort", effort]
        receipt = self.cli_json(args)
        return task_id, run_id, receipt

    def forget_mailbox(self, ctx: ToolContext) -> bool:
        """Drop the chat's saved mailbox and Run; returns whether there was one."""
        from django_ergo.conversation.models import ConversationSession

        meta = dict(ctx.session.metadata or {})
        orca = dict(meta.get("orca") or {})
        if orca.pop(self.environment or "local", None) is None:
            return False
        meta["orca"] = orca
        ConversationSession.objects.filter(pk=ctx.session.pk).update(metadata=meta)
        ctx.session.metadata = meta
        return True

    def start_worker(  # noqa: PLR0913
        self,
        ctx: ToolContext,
        spec: str,
        worktree: str,
        agent: str = "codex",
        title: str = "",
        model: str = "",
        effort: str = "",
        tier: str = "",
    ) -> dict:
        """``orca_start_worker``: ``bots.agents.start`` on this plugin's manager."""
        from django_ergo.bots import agents
        from django_ergo.bots.workers import describe

        worker = agents.start(
            ctx,
            AgentSpec(spec, worktree, agent, model, effort, title, tier),
            self.manager.name,
        )
        handle = worker.args["handle"]
        return {
            **describe(worker),
            "orca": {
                key: handle[key] for key in ("run", "task", "dispatch", "worktree")
            },
        }

    def start_agent(self, ctx: ToolContext, spec: AgentSpec) -> dict:
        """Create the Orca task and worker for ``spec``; the manager's handle."""
        worktree, worktree_path = self.resolved_worktree(spec.workspace)
        args = (spec.brief, worktree, spec.agent, spec.title, spec.model, spec.effort)
        try:
            task_id, run_id, receipt = self.dispatch(ctx, *args)
        except ValueError as exc:
            # The chat's saved mailbox terminal or Run is gone (its worktree was
            # removed, Orca restarted): make new ones and try once more.
            if "selector_not_found" not in str(exc) or not self.forget_mailbox(ctx):
                raise
            task_id, run_id, receipt = self.dispatch(ctx, *args)
        dispatch_id = _first_key(receipt, ("dispatchId", "dispatch_id"))
        if not dispatch_id:
            msg = f"worker-start gave no dispatch id: {json.dumps(receipt)[:400]}"
            raise ValueError(msg)
        return {
            "dispatch": dispatch_id,
            "run": run_id,
            "task": task_id,
            "worktree": worktree,
            "path": worktree_path or "",
        }

    def capture_native_history(
        self, ctx, agent: str, worktree: str, history=None
    ) -> None:
        """Ingest exact omp session bytes returned by the execution host."""
        if agent != "omp":
            return
        from django_ergo.conversation.agent_history import ingest_omp_content
        from django_ergo.conversation.agent_history import omp_session_rows

        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                try:
                    content = base64.b64decode(str(item.get("content") or ""))
                except ValueError:
                    continue
                ingest_omp_content(
                    content,
                    source_name=str(item.get("name") or "omp.jsonl"),
                    worker=ctx.worker,
                    host_namespace=self.files_host or "local",
                )
            return
        if self.files_host:
            return
        root = Path.home() / ".omp" / "agent" / "sessions"
        for path in root.glob("**/*.jsonl"):
            header, _ = omp_session_rows(path.read_bytes())
            if header.get("cwd") == worktree:
                ingest_omp_content(
                    path.read_bytes(), source_name=path.name, worker=ctx.worker
                )

    def scan_usage(self, ctx, *, settled: bool = False) -> None:
        """Best-effort usage scan; failure leaves the last stored snapshot intact."""
        handle = (ctx.worker.args or {}).get("handle") or {}
        worktree = ctx.state.get("worktree") or handle.get("path")
        if not (agent := ctx.state.get("agent")) or not worktree:
            return
        now = time.time()
        previous = float(ctx.state.get("usage_scanned_at") or 0)
        if not settled and now - previous < self.usage_minutes * 60:
            return
        from django.utils import timezone

        from django_ergo.conversation.models import AgentUsage

        since = ctx.worker.created_at - timedelta(minutes=1)
        until = ctx.worker.completed_at or timezone.now()
        script = (
            Path(__file__).with_name("agent_usage_scan.py").read_text(encoding="utf-8")
        )
        args = [
            str(agent),
            str(worktree),
            since.isoformat(),
            until.isoformat(),
            "--history",
        ]
        argv = (
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.files_host,
                f"python3 - {shlex.join(args)}",
            ]
            if self.files_host
            else ["python3", "-", *args]
        )
        try:
            proc = subprocess.run(  # noqa: S603 — fixed interpreter and scanner source
                argv,
                input=script,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
            if proc.returncode:
                log.warning(
                    "Couldn't scan %s usage for worker %s: %s",
                    agent,
                    ctx.worker.pk,
                    (proc.stderr or proc.stdout).strip()[:300],
                )
                return
            data = json.loads(proc.stdout)
            models = data.get("models") if isinstance(data, dict) else None
            if not isinstance(models, dict):
                log.warning(
                    "Usage scanner returned no models for worker %s", ctx.worker.pk
                )
                return

            def parse_at(value):
                return (
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if isinstance(value, str)
                    else None
                )

            seen = []
            for model, usage in models.items():
                if not isinstance(model, str) or not isinstance(usage, dict):
                    continue
                seen.append(model)
                AgentUsage.objects.update_or_create(
                    worker=ctx.worker,
                    model=model,
                    defaults={
                        "session": ctx.session,
                        "bot_name": ctx.worker.bot_name,
                        "source": "orca",
                        "agent": agent,
                        "input_tokens": int(usage.get("input", 0)),
                        "cache_write_tokens": int(usage.get("cache_write", 0)),
                        "cache_read_tokens": int(usage.get("cache_read", 0)),
                        "output_tokens": int(usage.get("output", 0)),
                        "reasoning_tokens": int(usage.get("reasoning", 0)),
                        "requests": int(usage.get("requests", 0)),
                        "first_at": parse_at(usage.get("first_at")),
                        "last_at": parse_at(usage.get("last_at")),
                    },
                )
            AgentUsage.objects.filter(worker=ctx.worker).exclude(
                model__in=seen
            ).delete()
            ctx.state["usage_scanned_at"] = now
            self.capture_native_history(ctx, agent, str(worktree), data.get("history"))
        except Exception:  # noqa: BLE001 — agent session files are best effort
            log.warning(
                "Couldn't scan %s usage for worker %s",
                agent,
                ctx.worker.pk,
                exc_info=True,
            )

    def watch(self, ctx, dispatch: str, run: str):
        """Worker function ``orca:watch``, for workers started before ``agent:orca``."""
        from django_ergo.bots.agents import watch

        task = ctx.state.get("task", "")
        return watch(
            ctx, self.manager, {"dispatch": dispatch, "run": run, "task": task}
        )

    def check(self, ctx, handle: dict) -> AgentCheck:
        """One look at an Orca worker: its status, new messages, latest output and
        usage. Fails it when its agent terminal is gone (``fail_if_terminal_gone``)."""
        dispatch, run = handle["dispatch"], handle["run"]
        shown = self.cli_json(["orchestration", "worker-show", "--dispatch", dispatch])
        status = str((shown.get("dispatch") or {}).get("status") or "")
        state = str((shown.get("worker") or {}).get("state") or "")
        liveness = str((shown.get("observation") or {}).get("status") or "")
        report = None
        questions = []
        for message in self.run_messages(run, dispatch, handle.get("task", "")):
            kind = message.get("type")
            if kind == "worker_done":
                report = message
            elif kind in ("question", "escalation"):
                questions.append(
                    AgentQuestion(
                        id=message["id"],
                        body=message.get("body") or "",
                        subject=message.get("subject") or "",
                        kind=kind,
                    )
                )
        self.record_activity(ctx, dispatch, shown)
        self.scan_usage(ctx, settled=report is not None or status in SETTLED)
        if report is not None or status in SETTLED:
            body = (report or {}).get("body") or ""
            if status == "failed" and not body:
                failure = (shown.get("dispatch") or {}).get(
                    "last_failure"
                ) or "no report"
                return AgentCheck(
                    "failed",
                    questions=questions,
                    error=f"The Orca worker failed: {failure}",
                )
            return AgentCheck(
                "done",
                questions=questions,
                report={
                    "status": status or "completed",
                    "subject": (report or {}).get("subject") or "",
                    "report": body,
                    "dispatch": dispatch,
                },
            )
        if not ctx.stopping:
            self.fail_if_terminal_gone(ctx, shown)
            self.submit_brief_once(ctx, shown, status)
        progress = state or status or "starting"
        if liveness:
            progress += f" · {liveness}"
        if ctx.state.get("terminal_gone_since"):
            progress += f" · agent terminal {ctx.state.get('terminal')}; failing it if it stays that way"
        return AgentCheck("running", progress=progress, questions=questions)

    def agent_terminal(self, shown: dict) -> tuple[str, str]:
        """The agent's terminal: ("live" | "exited" | "gone" | "unknown", last output)."""
        handle = (shown.get("worker") or {}).get("agent_terminal_handle") or (
            shown.get("dispatch") or {}
        ).get("assignee_handle")
        if not handle:
            return "unknown", ""
        try:
            terminal = self.cli_json(["terminal", "show", "--terminal", handle])
        except ValueError as exc:
            stale = "terminal_handle_stale" in str(exc) or "not_found" in str(exc)
            return ("gone" if stale else "unknown"), ""
        info = terminal.get("terminal", terminal) if isinstance(terminal, dict) else {}
        preview = str(info.get("preview") or "")
        if info.get("connected") is False and info.get("paneRuntimeId") == -1:
            return "exited", preview
        return "live", preview

    def fail_if_terminal_gone(self, ctx, shown: dict) -> None:
        """Fail the worker when its agent terminal exited or vanished without a
        worker_done and stays that way for ``TERMINAL_GONE_GRACE_SECONDS``."""
        from django.utils import timezone

        state, preview = self.agent_terminal(shown)
        if state not in ("exited", "gone"):
            ctx.state.pop("terminal_gone_since", None)
            ctx.state.pop("terminal", None)
            return
        now = timezone.now()
        since = ctx.state.get("terminal_gone_since")
        if not since:
            ctx.state["terminal_gone_since"] = now.isoformat()
            ctx.state["terminal"] = state
            return
        gone_for = (now - datetime.fromisoformat(since)).total_seconds()
        if gone_for < TERMINAL_GONE_GRACE_SECONDS:
            return
        last = f" Its last output: {preview.strip()[:300]}" if preview.strip() else ""
        msg = (
            f"The Orca worker's agent terminal {state} without reporting done.{last} "
            "Start it again with ergo_agent_start if the task still needs doing."
        )
        raise RuntimeError(msg)

    def record_activity(self, ctx, dispatch: str, shown: dict) -> None:
        """Keep the agent's latest output on the worker, with when it last did
        something: the newest transcript time or heartbeat, or this check if its
        output changed since the last one. Best effort: a failed read keeps the last."""
        try:
            read = self.cli_json(
                [
                    "orchestration",
                    "worker-read",
                    "--dispatch",
                    dispatch,
                    "--limit",
                    "20",
                ]
            )
        except Exception:  # noqa: BLE001 — the card keeps what the last check read
            read = None
        now = time.time()
        previous = ctx.state.get("activity") or {}
        source, entries = output_entries(read, ACTIVITY_CHARS) if read else ("", [])
        fingerprint = hashlib.sha1(  # noqa: S324 — a change check, not security
            json.dumps(entries, sort_keys=True).encode()
        ).hexdigest()
        times = [e["at"] for e in entries if e["at"]]
        times.append(_epoch((shown.get("dispatch") or {}).get("last_heartbeat_at")))
        if read is not None and fingerprint != ctx.state.get("activity_fingerprint"):
            times.append(now)
        times.append(_epoch(previous.get("at")))
        last = max((t for t in times if t), default=None)
        if read is not None:
            ctx.state["activity_fingerprint"] = fingerprint
        wait = (shown.get("observation") or {}).get("agentWait") or {}
        liveness = ((shown.get("projection") or {}).get("liveness") or {}).get(
            "verdict"
        )
        ctx.activity(
            entries[-ACTIVITY_ENTRIES:]
            if read is not None
            else previous.get("entries") or [],
            at=last,
            source=source or previous.get("source") or "",
            waiting=str(wait.get("reason") or "interactive prompt") if wait else "",
            liveness=str(liveness or ""),
            stall_after=self.stall_minutes * 60,
            checked_at=now,
        )

    def worker_log(self, worker) -> dict | None:
        """The whole recent output of an Orca agent's worker, read now."""
        handle = self.manager.worker_handle(worker) or (worker.args or {}).get("handle")
        dispatch = (handle or {}).get("dispatch")
        if not dispatch:
            return None
        # Orca clamps the limit: 50 transcript messages, or this many screen lines.
        read = self.cli_json(
            [
                "orchestration",
                "worker-read",
                "--dispatch",
                dispatch,
                "--limit",
                str(LOG_LINES),
            ]
        )
        source, entries = output_entries(read)
        return {"source": source, "entries": entries}

    def submit_brief_once(self, ctx, shown: dict, status: str) -> None:
        """Older Orca hosts can leave the injected brief unsubmitted in the agent's input
        box. On the first check, if the agent hasn't checked in yet, press Enter once in
        its terminal (an empty Enter does nothing to an agent that's already working)."""
        if ctx.state.get("nudged") or status != "dispatched":
            return
        ctx.state["nudged"] = True
        dispatch = shown.get("dispatch") or {}
        handle = (shown.get("worker") or {}).get(
            "agent_terminal_handle"
        ) or dispatch.get("assignee_handle")
        if dispatch.get("last_heartbeat_at") or not handle:
            return
        try:
            self.cli_json(["terminal", "send", "--terminal", handle, "--enter"])
            ctx.progress("pressed Enter to submit the brief")
        except ValueError:
            pass  # best effort; the next check reports what the agent is doing

    def run_messages(self, run: str, dispatch: str, task: str) -> list[dict]:
        """Messages to this Run about this dispatch, read without consuming them."""
        inbox = self.cli_json(["orchestration", "inbox", "--limit", "100"])
        rows = inbox.get("messages", []) if isinstance(inbox, dict) else inbox
        mine = []
        for row in rows:
            if row.get("run_id") != run:
                continue
            try:
                payload = json.loads(row.get("payload") or "{}")
            except ValueError:
                payload = {}
            ids = {payload.get("dispatchId"), payload.get("dispatch_id")}
            tasks = {payload.get("taskId"), payload.get("task_id")}
            if dispatch in ids or (task and task in tasks):
                mine.append(row)
        return sorted(mine, key=lambda r: r.get("sequence") or 0)

    def worker_functions(self) -> dict:
        return {"watch": self.watch}

    def agent_managers(self) -> dict[str, AgentManager]:
        return {self.manager.name: self.manager}

    @property
    def skill_requires(self) -> list[str]:
        return ["agents"]

    # -- plugin hooks ------------------------------------------------------

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        if self.root_only and ctx.bot and not ctx.bot.is_root(ctx.session):
            return []
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if self.root_only and ctx.bot and not ctx.bot.is_root(ctx.session):
            return []
        where = (
            f"the {self.environment!r} environment" if self.environment else "this host"
        )
        approval = (
            "orca_run asks the user to approve each call"
            if self.approve_changes
            else "orca_run runs without approval"
        )
        return [
            TextContextSource(
                "Orca",
                f"You manage Orca on {where} with the {self.executable} CLI. "
                f"Use orca_read for inventory; {approval}. Output is compact JSON; long "
                'output keeps only its start and end, so pass fields (e.g. ["id", "path", "branch"]) '
                "to list commands. Start coding agents only with ergo_agent_start (or "
                "orca_start_worker): it makes a supervised Orca worker that reports back to "
                "this chat. Never start one by "
                "creating a terminal with --command and sending it text; that agent is "
                "unsupervised and invisible to Orca's worker list. Before stopping workers or "
                'anything else unusual, read the CLI\'s guides with orca_read ["skills", '
                '"get", "orca-cli"] and ["skills", "get", "orchestration"], and check '
                "the run and worker lists so you don't duplicate work.",
            )
        ]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="orca_read",
            description=(
                "Run a read-only Orca CLI command: status, worktree ps/list/show, "
                "terminal list/read/show, repo list/show, orchestration "
                "run-list/worker-list/worker-read/worker-show, search, skills get, "
                "or any command with --help."
            ),
            parameters=ARGS_SCHEMA,
            required=["args"],
        )
        def read(args: list[str], fields: list[str] | None = None) -> str:
            return plugin.read(args, fields)

        @bot_tool(
            name="orca_run",
            description=(
                "Run an Orca CLI command that changes state: create or remove "
                "worktrees and terminals, send to a terminal, start, stop or "
                "release workers, create runs and tasks."
            ),
            parameters=ARGS_SCHEMA,
            required=["args"],
            requires_approval=self.approve_changes,
        )
        def run(args: list[str], fields: list[str] | None = None) -> str:
            return plugin.run(args, fields)

        @bot_tool(
            name="orca_attach",
            takes_context=True,
            description=(
                "Copy a file from an Orca worktree into this chat as an attachment, e.g. a "
                "screenshot, report or log a worker wrote. Lee can then open it in the chat; "
                "use ergo_attachments_look to see an image or PDF yourself."
            ),
            parameters={
                "worktree": {
                    "type": "string",
                    "description": "Worktree selector, e.g. id:<repo-id>::<path> or a path",
                },
                "path": {
                    "type": "string",
                    "description": "File path, relative to the worktree (or absolute inside it)",
                },
                "filename": {
                    "type": "string",
                    "description": "Name for the attachment (default: the file's name)",
                },
            },
            required=["worktree", "path"],
        )
        def attach(
            ctx: ToolContext, worktree: str, path: str, filename: str = ""
        ) -> dict:
            return plugin.attach(ctx, worktree, path, filename)

        @bot_tool(
            name="orca_upload",
            takes_context=True,
            description=(
                "Copy files from this chat (or files another chat shared with you: pass "
                "its session_id) into a folder of an Orca worktree, so a worker can use "
                "them, e.g. design mockups and a Penpot tree export. Tell the worker the "
                "paths in its brief."
            ),
            parameters={
                "worktree": {
                    "type": "string",
                    "description": "Worktree selector, e.g. id:<repo-id>::<path> or a path",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filenames or file ids",
                },
                "folder": {
                    "type": "string",
                    "description": 'Folder in the worktree (default "design"); created if missing',
                },
                "session_id": {
                    "type": "string",
                    "description": "The chat the files are in (default: this one)",
                },
            },
            required=["worktree", "files"],
            requires_approval=self.approve_changes,
        )
        def upload(
            ctx: ToolContext,
            worktree: str,
            files: list[str],
            folder: str = "design",
            session_id: str = "",
        ) -> list[dict]:
            return plugin.upload(ctx, worktree, files, folder, session_id)

        @bot_tool(
            name="orca_screenshot",
            takes_context=True,
            description=(
                "Take a screenshot of an Orca browser tab and attach it to this chat (no approval: "
                "it only looks). Give the worktree, and the page id from tab list if the worktree "
                "has several tabs. Use ergo_attachments_look to see it yourself."
            ),
            parameters={
                "worktree": {
                    "type": "string",
                    "description": "Worktree selector, e.g. path:/home/dev/p/x",
                },
                "page": {
                    "type": "string",
                    "description": "Browser page id (tab list); default the active tab",
                },
                "image_format": {"type": "string", "enum": ["png", "jpeg"]},
            },
            required=[],
        )
        def screenshot(
            ctx: ToolContext,
            worktree: str = "",
            page: str = "",
            image_format: str = "png",
        ) -> dict:
            return plugin.screenshot(ctx, worktree, page, image_format)

        @bot_tool(
            name="orca_start_worker",
            takes_context=True,
            description=(
                "Start a supervised coding agent (an Orca worker) on a task in a worktree. It runs "
                "for minutes to hours; this chat shows it as a worker, and its report comes back here "
                "as a message when it's done, so don't wait or poll. The spec must be self-contained: "
                "target, change, constraints, ownership, and how to prove it's done."
            ),
            parameters={
                "spec": {"type": "string", "description": "The task, self-contained"},
                "worktree": {
                    "type": "string",
                    "description": "Exact worktree selector (id:<repo-id>::<path>) or path:<path>",
                },
                "agent": {
                    "type": "string",
                    "description": "codex, claude or omp (default codex)",
                },
                "title": {
                    "type": "string",
                    "description": "A short title for the task",
                },
                "model": {
                    "type": "string",
                    "description": "Model id or provider/model selector (for omp, e.g. anthropic/claude-sonnet-5-5)",
                },
                "effort": {
                    "type": "string",
                    "description": "Reasoning effort (needs model)",
                },
                "tier": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "Pick agent, model and effort for this tier from the subscription "
                        "with the most room (replaces agent, model and effort)"
                    ),
                },
            },
            required=["spec", "worktree"],
            requires_approval=self.approve_changes,
        )
        def start_worker(  # noqa: PLR0913
            ctx: ToolContext,
            spec: str,
            worktree: str,
            agent: str = "codex",
            title: str = "",
            model: str = "",
            effort: str = "",
            tier: str = "",
        ) -> dict:
            return plugin.start_worker(
                ctx, spec, worktree, agent, title, model, effort, tier
            )

        return [
            read.__bot_tool__,
            run.__bot_tool__,
            attach.__bot_tool__,
            upload.__bot_tool__,
            screenshot.__bot_tool__,
            start_worker.__bot_tool__,
        ]


class OrcaAgents(AgentManager):
    """Coding agents as supervised Orca workers, each in an Orca worktree."""

    name = "orca"

    def __init__(self, plugin: OrcaPlugin):
        self.plugin = plugin
        self.agents = tuple(plugin.config.get("agents") or ("codex", "claude", "omp"))
        self.requires_approval = plugin.approve_changes
        self.poll_seconds = plugin.poll_seconds
        place = plugin.environment or "this host"
        self.where = f"Orca worktrees on {place}"

    def available(self, ctx) -> bool:
        return not (
            self.plugin.root_only and ctx.bot and not ctx.bot.is_root(ctx.session)
        )

    def start(self, ctx, spec: AgentSpec) -> dict:
        return self.plugin.start_agent(ctx, spec)

    def check(self, ctx, handle: dict) -> AgentCheck:
        return self.plugin.check(ctx, handle)

    def reply(self, ctx, handle: dict, question_id: str, text: str) -> str:
        self.plugin.cli_json(
            ["orchestration", "reply", "--id", question_id, "--body", text]
        )
        return f"Sent the answer to {question_id}."

    def stop(self, handle: dict) -> str:
        return (
            f"The Orca worker (dispatch {handle.get('dispatch')}) may keep running; "
            "stop it with orca_run as the CLI's orchestration guide says (orca_read "
            '["skills", "get", "orchestration"])'
        )

    def log(self, worker, handle: dict) -> dict | None:
        return self.plugin.worker_log(worker)

    def worker_handle(self, worker) -> dict | None:
        if worker.function != "orca:watch":
            return None
        args = worker.args or {}
        return {
            "dispatch": args.get("dispatch", ""),
            "run": args.get("run", ""),
            "task": (worker.state or {}).get("task", ""),
        }
