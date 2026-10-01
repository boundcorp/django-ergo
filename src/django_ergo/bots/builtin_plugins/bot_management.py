"""Bot management plugin: let a bot edit and publish its own repository.

    plugins:
      - name: bot_management
        mode: propose_pr        # or merge_main
        main_branch: main
        remote: origin
        approve_publish: true   # repo_publish waits for the user's approval
        root_only: true         # only the root session gets these tools

The repository is the git checkout that contains the bot folder. Tools:

- ``repo_status``, ``repo_list``, ``repo_read``, ``repo_diff``: look around.
- ``repo_write``: change a file in the working copy (nothing is published).
- ``repo_publish``: commit everything. In ``merge_main`` mode it rebases on
  the main branch and pushes to it. In ``propose_pr`` mode it pushes a new
  branch and opens a pull request with the GitHub CLI (``gh``), then returns
  to the main branch.
- ``repo_pull``: fast-forward the main branch from the remote.
- ``repo_prs``: list open pull requests (``gh``).

Changes to bot.yaml, agents.md or tool files take effect when the bot is
loaded again.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.toolkit import Toolkit

MODES = {"merge_main", "propose_pr"}
COMMAND_TIMEOUT = 120
MAX_READ_CHARS = 50_000


class BotManagementPlugin(BotPlugin):
    name = "bot_management"

    def on_load(self) -> None:
        self.mode = self.config.get("mode", "propose_pr")
        if self.mode not in MODES:
            msg = f"bot_management mode must be one of {sorted(MODES)}"
            raise ValueError(msg)
        self.main_branch = self.config.get("main_branch", "main")
        self.remote = self.config.get("remote", "origin")
        self.approve_publish = bool(self.config.get("approve_publish", True))
        self.root_only = bool(self.config.get("root_only", True))
        self._repo: Path | None = None

    # -- commands ----------------------------------------------------------

    @property
    def repo(self) -> Path:
        if self._repo is None:
            folder = self.bot.definition.root_dir
            if folder is None:
                msg = "bot_management needs a bot loaded from a folder"
                raise ValueError(msg)
            top = self.run(["git", "rev-parse", "--show-toplevel"], cwd=folder)
            self._repo = Path(top.strip()).resolve()
        return self._repo

    def run(self, args: list[str], cwd: Path | None = None) -> str:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            args,
            cwd=cwd or self.repo,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            msg = f"{' '.join(args[:3])} failed: {detail}"
            raise ValueError(msg)
        return proc.stdout

    def git(self, *args: str) -> str:
        return self.run(["git", *args])

    def path(self, relative: str) -> Path:
        path = (self.repo / relative).resolve()
        if (
            not path.is_relative_to(self.repo)
            or ".git" in path.relative_to(self.repo).parts
        ):
            msg = f"{relative} is outside the repository"
            raise ValueError(msg)
        return path

    # -- tools -------------------------------------------------------------

    def status(self) -> str:
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        changes = self.git("status", "--short").strip() or "(clean)"
        log = self.git("log", "--oneline", "-5").strip()
        return (
            f"Repository: {self.repo}\nBranch: {branch}\nMode: {self.mode}\n\n"
            f"Changes:\n{changes}\n\nRecent commits:\n{log}"
        )

    def list_files(self, path: str = ".") -> str:
        base = self.path(path)
        files = self.git("ls-files", "--cached", "--others", "--exclude-standard")
        prefix = "" if base == self.repo else f"{base.relative_to(self.repo)}/"
        return "\n".join(f for f in files.splitlines() if f.startswith(prefix))

    def read(self, path: str) -> str:
        text = self.path(path).read_text()
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + "\n[truncated]"
        return text

    def write(self, path: str, content: str) -> str:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {target.relative_to(self.repo)} ({len(content)} chars)"

    def diff(self) -> str:
        self.git("add", "--intent-to-add", "--all")
        return self.git("diff").strip() or "(no changes)"

    def pull(self) -> str:
        self.git("checkout", self.main_branch)
        return self.git("pull", "--ff-only", self.remote, self.main_branch).strip()

    def publish(self, message: str, title: str = "", body: str = "") -> str:
        if not self.git("status", "--porcelain").strip():
            return "Nothing to publish: the working copy is clean."
        if self.mode == "merge_main":
            self.git("add", "--all")
            self.git("commit", "-m", message)
            self.git("pull", "--rebase", self.remote, self.main_branch)
            self.git("push", self.remote, f"HEAD:{self.main_branch}")
            sha = self.git("rev-parse", "--short", "HEAD").strip()
            return f"Pushed {sha} to {self.main_branch}."

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", (title or message).lower()).strip("-")
        branch = f"bot/{self.bot.name}/{stamp}-{slug[:40]}".rstrip("-")
        self.git("checkout", "-b", branch)
        try:
            self.git("add", "--all")
            self.git("commit", "-m", message)
            self.git("push", "-u", self.remote, branch)
            url = self.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    self.main_branch,
                    "--head",
                    branch,
                    "--title",
                    title or message.splitlines()[0],
                    "--body",
                    body or message,
                ]
            ).strip()
        finally:
            self.git("checkout", self.main_branch)
        return f"Opened {url} from {branch}."

    def prs(self) -> str:
        return self.run(
            ["gh", "pr", "list", "--json", "number,title,url,headRefName,state"]
        ).strip()

    def toolkits(self, ctx: ToolContext) -> list[Toolkit]:
        if self.root_only and not ctx.bot.is_root(ctx.session):
            return []
        return [FunctionToolkit(self._tools(), ctx)]

    def _tools(self) -> list[BotTool]:
        plugin = self
        publish_help = (
            "Commit all changes and push them to the main branch."
            if self.mode == "merge_main"
            else "Commit all changes on a new branch and open a pull request."
        )

        @bot_tool(name="repo_status")
        def status() -> str:
            """Show the bot repository's branch, uncommitted changes and recent commits."""
            return plugin.status()

        @bot_tool(name="repo_list")
        def list_files(path: str = ".") -> str:
            """List files in the bot repository, optionally under a folder."""
            return plugin.list_files(path)

        @bot_tool(name="repo_read")
        def read(path: str) -> str:
            """Read a file from the bot repository."""
            return plugin.read(path)

        @bot_tool(name="repo_write")
        def write(path: str, content: str) -> str:
            """Create or replace a file in the bot repository's working copy."""
            return plugin.write(path, content)

        @bot_tool(name="repo_diff")
        def diff() -> str:
            """Show uncommitted changes in the bot repository."""
            return plugin.diff()

        @bot_tool(name="repo_pull")
        def pull() -> str:
            """Update the main branch from the remote (fast-forward only)."""
            return plugin.pull()

        @bot_tool(
            name="repo_publish",
            description=publish_help,
            requires_approval=self.approve_publish,
        )
        def publish(message: str, title: str = "", body: str = "") -> str:
            return plugin.publish(message, title, body)

        @bot_tool(name="repo_prs")
        def prs() -> str:
            """List open pull requests on the bot repository."""
            return plugin.prs()

        functions = [status, list_files, read, write, diff, pull, publish, prs]
        return [fn.__bot_tool__ for fn in functions]
