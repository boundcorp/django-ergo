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
- ``orca_run``: every other command (creating worktrees, starting and
  stopping workers, sending to terminals...). Each call needs approval unless
  ``approve_changes: false``.

Arguments are passed as a list, never through a shell. ``--json`` is added
where the CLI accepts it, and with ``environment`` set the bot can't point a
call anywhere else.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

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
        self.max_attach_bytes = int(self.config.get("max_attach_bytes", 20_000_000))

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
                "to list commands. Before starting or "
                'stopping workers, read the CLI\'s guides with orca_read ["skills", '
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

        return [
            read.__bot_tool__,
            run.__bot_tool__,
            attach.__bot_tool__,
            screenshot.__bot_tool__,
        ]
