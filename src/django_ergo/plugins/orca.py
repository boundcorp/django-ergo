"""Orca plugin: manage Orca worktrees, terminals and workers with the Orca CLI.

    plugins:
      - name: orca
        environment: devbox        # pin every call to this Orca environment
        executable: orca-ide       # default: orca-ide if installed, else orca
        approve_changes: true      # orca_run waits for the user's approval
        root_only: true            # only the root session gets these tools
        timeout: 120               # seconds per command

The bot runs the CLI on the host Ergonaut runs on, as that user. Tools:

- ``orca_read``: read-only commands, no approval: ``status``, ``worktree
  ps|list|show|current``, ``terminal list|read|show``, ``repo
  list|show|search-refs``, ``orchestration run-list|worker-list|worker-read|
  worker-show``, ``search``, ``skills get`` (the CLI's own guides) and any
  command with ``--help``.
- ``orca_run``: every other command (creating worktrees, starting and
  stopping workers, sending to terminals...). Each call needs approval unless
  ``approve_changes: false``.

Arguments are passed as a list, never through a shell. ``--json`` is added
where the CLI accepts it, and with ``environment`` set the bot can't point a
call anywhere else.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

MAX_OUTPUT_CHARS = 20_000
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
    ("orchestration", "worker-show"),
    ("search",),
    ("skills", "get"),
}
NO_JSON = {("skills", "get")}
TARGET_FLAGS = ("--environment", "--pairing-code")
ARGS_SCHEMA = {
    "args": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            'CLI arguments after the executable, e.g. ["worktree", "ps"] or '
            '["terminal", "read", "--terminal", "<handle>"]'
        ),
    }
}


def command_of(args: list[str]) -> tuple[str, ...]:
    """The subcommand words, e.g. ("orchestration", "worker-list")."""
    words = []
    for arg in args:
        if arg.startswith("-"):
            break
        words.append(arg)
    return tuple(words[:2])


def is_read_only(args: list[str]) -> bool:
    if "--help" in args or "-h" in args:
        return True
    command = command_of(args)
    return any(command[: len(known)] == known for known in READ_ONLY)


class OrcaPlugin(BotPlugin):
    name = "orca"

    def on_load(self) -> None:
        self.environment = str(self.config.get("environment") or "")
        self.executable = str(
            self.config.get("executable")
            or ("orca-ide" if shutil.which("orca-ide") else "orca")
        )
        self.approve_changes = bool(self.config.get("approve_changes", True))
        self.root_only = bool(self.config.get("root_only", True))
        self.timeout = int(self.config.get("timeout", 120))

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

    def run(self, args: list[str]) -> str:
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
        output = (proc.stdout + (proc.stderr if proc.returncode else "")).strip()
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n[truncated]"
        if proc.returncode:
            return f"Exit {proc.returncode}:\n{output}"
        return output or "(no output)"

    def read(self, args: list[str]) -> str:
        if not is_read_only(args):
            return (
                f"{' '.join(command_of(args)) or args!r} can change Orca state; "
                "use orca_run, which asks the user first."
            )
        return self.run(args)

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
                f"Use orca_read for inventory; {approval}. Before starting or "
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
        def read(args: list[str]) -> str:
            return plugin.read(args)

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
        def run(args: list[str]) -> str:
            return plugin.run(args)

        return [read.__bot_tool__, run.__bot_tool__]
