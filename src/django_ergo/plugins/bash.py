"""Bash plugin: run shell commands on the host Ergonaut runs on.

    plugins:
      - name: bash
        cwd: ~                     # working directory (default: the user's home)
        approve: true              # every command waits for the user's approval
        timeout: 120               # seconds per command
        root_only: true            # only the root session gets the tool

``ergo_bash_run`` runs one command with ``bash -lc`` as the user Ergonaut
runs as, with that user's environment and permissions. That is a lot of
power: keep ``approve: true`` unless the bot is trusted with the machine.
Output is stdout and stderr together, with the exit code, trimmed to the
first and last part when it is long.
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

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.context import ContextSource
    from django_ergo.conversation.toolkit import Toolkit

MAX_OUTPUT_CHARS = 20_000


def trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the start and the end of long output, where the useful parts are."""
    if len(text) <= limit:
        return text
    head = text[: limit // 4]
    tail = text[-(limit - len(head)) :]
    skipped = len(text) - len(head) - len(tail)
    return f"{head}\n[... {skipped} characters skipped ...]\n{tail}"


class BashPlugin(BotPlugin):
    name = "bash"

    def on_load(self) -> None:
        self.cwd = Path(str(self.config.get("cwd") or "~")).expanduser()
        self.approve = bool(self.config.get("approve", True))
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
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return trim(f"Timed out after {self.timeout}s.\n{partial}".strip())
        output = (proc.stdout + proc.stderr).strip() or "(no output)"
        return trim(f"Exit {proc.returncode}\n{output}")

    def _applies(self, ctx: ToolContext) -> bool:
        return not (self.root_only and ctx.bot and not ctx.bot.is_root(ctx.session))

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        if not self._applies(ctx):
            return []
        return [FunctionToolkit(self._tools(), ctx)]

    def context_sources(self, ctx: ToolContext, message: str) -> list[ContextSource]:
        if not self._applies(ctx):
            return []
        approval = (
            "Each command waits for the user's approval, so say what it does and why."
            if self.approve
            else "Commands run without approval."
        )
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
