"""Bash plugin: run shell commands on the host Ergonaut runs on.

    plugins:
      - name: bash
        cwd: ~                     # working directory (default: the user's home)
        approve: true              # true: every command waits for the user's approval
                                   # review: a reviewer may let routine reads through
                                   # false: commands run without approval
        reviewer:                  # only with approve: review (all keys optional)
          model: auto/small        # providers.yaml model or auto/<tier>
          ssh_hosts: [rigel]       # ssh aliases whose remote reads may be reviewed
          timeout: 45              # seconds; a slower review waits for the user
        timeout: 120               # seconds per command
        root_only: true            # only the root session gets the tool

``ergo_bash_run`` runs one command with ``bash -lc`` as the user Ergonaut
runs as, with that user's environment and permissions. That is a lot of
power: keep ``approve: true`` unless the bot is trusted with the machine.
Output is stdout and stderr together, with the exit code, trimmed to the
first and last part when it is long.

``approve: review`` keeps human approval for everything except commands the
reviewer (``plugins.bash_review``) finds read-only, clearly asked for by the
user in the current turn, and low-risk; any doubt or failure waits for the
user as with ``approve: true``.

Protected variables (``FIREWORKS_*`` and ``DJANGO_ERGO["PROTECTED_ENV_PREFIXES"]``)
are removed from the command's environment. That is defense-in-depth, not a
sandbox: a login shell still reads the user's startup files, and the command
runs as the same OS user as Ergonaut.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.conversation.context import TextContextSource
from django_ergo.protected_env import scrubbed_env

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

from django_ergo.plugins._common import trim
from django_ergo.plugins.bash_review import ReviewerConfig
from django_ergo.plugins.bash_review import review


class _ReviewedBashToolkit(FunctionToolkit):
    """The bash tool with ``approve: review``: each call is reviewed on its own."""

    def __init__(self, plugin: BashPlugin, ctx: ToolContext):
        super().__init__(plugin._tools(), ctx)  # noqa: SLF001 — toolkit belongs to this plugin
        self.plugin = plugin

    async def auto_approve(self, tool_name: str, arguments: dict, tool_id: str) -> bool:
        return tool_name == "ergo_bash_run" and await review(
            self.context, self.plugin, tool_id, arguments
        )


class BashPlugin(BotPlugin):
    name = "bash"
    description = "Run shell commands on the host (each one approved)"

    def on_load(self) -> None:
        self.cwd = Path(str(self.config.get("cwd") or "~")).expanduser()
        approve = self.config.get("approve", True)
        if approve not in (True, False, "review"):
            msg = f"bash: approve must be true, false or review, not {approve!r}"
            raise ValueError(msg)
        # review still requires approval: the reviewer may only waive it per call.
        self.approve = approve is not False
        self.reviewer = (
            ReviewerConfig.parse(self.config.get("reviewer"))
            if approve == "review"
            else None
        )
        if self.reviewer is None and self.config.get("reviewer") is not None:
            msg = "bash: reviewer needs approve: review"
            raise ValueError(msg)
        self.timeout = int(self.config.get("timeout", 120))
        self.root_only = bool(self.config.get("root_only", True))

    def run(self, command: str, cwd: str = "") -> str:
        if not command.strip():
            return "Give a command to run."
        folder = Path(cwd).expanduser() if cwd else self.cwd
        if not folder.is_dir():
            return f"No such directory: {folder}"
        try:
            proc = subprocess.run(  # noqa: S603 — running commands is this tool's job
                ["bash", "-lc", command],  # noqa: S607
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                env=scrubbed_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return trim(f"Timed out after {self.timeout}s.\n{partial}".strip())
        output = (proc.stdout + proc.stderr).strip() or "(no output)"
        return trim(f"Exit {proc.returncode}\n{output}")

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        if not self._applies(ctx):
            return []
        if self.reviewer is not None:
            return [_ReviewedBashToolkit(self, ctx)]
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if not self._applies(ctx):
            return []
        if self.reviewer is not None:
            approval = (
                "A reviewer lets routine read-only commands the user clearly asked for "
                "run at once; every other command waits for the user's approval, so "
                "say what it does and why."
            )
        elif self.approve:
            approval = "Each command waits for the user's approval, so say what it does and why."
        else:
            approval = "Commands run without approval."
        return [
            TextContextSource(
                "Shell",
                f"ergo_bash_run runs bash commands on this host as {os.environ.get('USER', 'the server user')}, "
                f"starting in {self.cwd}. {approval} Prefer read-only commands, "
                "keep output short (pipe to head, use --quiet flags), and never print secrets.",
            )
        ]

    def _tools(self) -> list[BotTool]:
        plugin = self

        @bot_tool(
            name="ergo_bash_run",
            description=(
                "Run a bash command on the host and return its exit code and output. "
                "Optionally give a working directory."
            ),
            parameters={
                "command": {"type": "string", "description": "The bash command"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory (default: the configured one)",
                },
            },
            required=["command"],
            requires_approval=self.approve,
        )
        def run(command: str, cwd: str = "") -> str:
            return plugin.run(command, cwd)

        return [run.__bot_tool__]
