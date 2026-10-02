"""Bot management plugin: let a bot edit and publish its own repository.

    plugins:
      - name: bot_management
        mode: propose_pr        # or merge_main
        main_branch: main
        remote: origin
        approve_publish: true   # ergo_config_repo_publish waits for the user's approval
        root_only: true         # only the root session gets these tools

The repository is the git checkout that contains the bot folder. Tools:

- ``ergo_config_repo_status``, ``ergo_config_repo_list``, ``ergo_config_repo_read``, ``ergo_config_repo_diff``: look around.
- ``ergo_config_repo_write``: change a file (nothing is published).
- ``ergo_config_repo_publish``: commit everything. In ``merge_main`` mode it rebases on
  the main branch and pushes to it. In ``propose_pr`` mode it pushes a new
  branch and opens a pull request with the GitHub CLI (``gh``).
- ``ergo_config_repo_discard``: throw away unpublished changes.
- ``ergo_config_repo_pull``: fast-forward the main branch from the remote.
- ``ergo_config_repo_prs``: list open pull requests (``gh``).

In ``merge_main`` mode the bot edits the checkout it runs from. In
``propose_pr`` mode it edits a draft: a separate git worktree of the main
branch (inside ``.git``), so the running bots never see a change until its
pull request is merged and the checkout is pulled. Changes to bot.yaml,
agents.md or tool files take effect when the bot is loaded again.
"""

from __future__ import annotations

import json
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

    @property
    def workdir(self) -> Path:
        """Where the bot reads and writes: the checkout, or its draft."""
        if self.mode == "merge_main":
            return self.repo
        draft = self.draft_dir
        if not draft.is_dir():
            self.git("fetch", self.remote, self.main_branch)
            self.git(
                "worktree",
                "add",
                "--force",
                "-B",
                self.draft_branch,
                str(draft),
                f"{self.remote}/{self.main_branch}",
            )
        return draft

    @property
    def draft_dir(self) -> Path:
        common = Path(self.git("rev-parse", "--git-common-dir").strip())
        if not common.is_absolute():
            common = self.repo / common
        return common.resolve() / f"ergo-draft-{self.bot.name}"

    @property
    def draft_branch(self) -> str:
        return f"bot/{self.bot.name}/draft"

    def drop_draft(self) -> None:
        if self.mode != "merge_main" and self.draft_dir.is_dir():
            self.git("worktree", "remove", "--force", str(self.draft_dir))
            self.git("branch", "-D", self.draft_branch)

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

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return self.run(["git", *args], cwd=cwd)

    def path(self, relative: str) -> Path:
        root = self.workdir
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or ".git" in path.relative_to(root).parts:
            msg = f"{relative} is outside the repository"
            raise ValueError(msg)
        return path

    # -- tools -------------------------------------------------------------

    def status(self) -> str:
        work = self.workdir
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=work).strip()
        changes = self.git("status", "--short", cwd=work).strip() or "(clean)"
        log = self.git("log", "--oneline", "-5", cwd=work).strip()
        where = "draft of " if work != self.repo else ""
        return (
            f"Repository: {where}{self.repo}\nBranch: {branch}\nMode: {self.mode}\n\n"
            f"Changes:\n{changes}\n\nRecent commits:\n{log}"
        )

    def list_files(self, path: str = ".") -> str:
        base = self.path(path)
        work = self.workdir
        files = self.git(
            "ls-files", "--cached", "--others", "--exclude-standard", cwd=work
        )
        prefix = "" if base == work else f"{base.relative_to(work)}/"
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
        return f"Wrote {target.relative_to(self.workdir)} ({len(content)} chars)"

    def diff(self) -> str:
        work = self.workdir
        self.git("add", "--intent-to-add", "--all", cwd=work)
        return self.git("diff", cwd=work).strip() or "(no changes)"

    def discard(self) -> str:
        if self.mode == "merge_main":
            self.git("reset", "--hard")
            self.git("clean", "-fd")
        else:
            self.drop_draft()
        return "Discarded the unpublished changes."

    def pull(self) -> str:
        self.git("checkout", self.main_branch)
        return self.git("pull", "--ff-only", self.remote, self.main_branch).strip()

    def publish(self, message: str, title: str = "", body: str = "") -> str:
        work = self.workdir
        if not self.git("status", "--porcelain", cwd=work).strip():
            return "Nothing to publish: there are no changes."
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
        self.git("checkout", "-b", branch, cwd=work)
        self.git("add", "--all", cwd=work)
        self.git("commit", "-m", message, cwd=work)
        self.git("push", "-u", self.remote, branch, cwd=work)
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
            ],
            cwd=work,
        ).strip()
        self.git("worktree", "remove", "--force", str(work))
        self.git("branch", "-D", self.draft_branch)
        return f"Opened {url} from {branch}; it goes live once merged."

    def prs(self) -> str:
        return self.run(
            ["gh", "pr", "list", "--json", "number,title,url,headRefName,state"]
        ).strip()

    # -- for review screens (Ergonaut's Changes tab) ---------------------------

    def draft_diff(self) -> str:
        """The unpublished changes, without starting a draft if there is none."""
        if self.mode == "merge_main":
            work = self.repo
        elif self.draft_dir.is_dir():
            work = self.draft_dir
        else:
            return ""
        self.git("add", "--intent-to-add", "--all", cwd=work)
        return self.git("diff", cwd=work).strip()

    def pull_requests(self) -> list[dict]:
        """Open pull requests on the bot repository, newest first."""
        fields = "number,title,url,headRefName,author,createdAt,body,additions,deletions,changedFiles"
        return json.loads(
            self.run(["gh", "pr", "list", "--state", "open", "--json", fields]) or "[]"
        )

    def pull_request_diff(self, number: int) -> str:
        return self.run(["gh", "pr", "diff", str(int(number))])

    def merge_pull_request(self, number: int) -> str:
        """Squash-merge a pull request and bring the checkout up to date."""
        self.run(["gh", "pr", "merge", str(int(number)), "--squash", "--delete-branch"])
        if not self.git("status", "--porcelain").strip():
            self.pull()
        return f"Merged #{int(number)}."

    def close_pull_request(self, number: int) -> str:
        self.run(["gh", "pr", "close", str(int(number)), "--delete-branch"])
        return f"Closed #{int(number)}."

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

        @bot_tool(name="ergo_config_repo_status")
        def status() -> str:
            """Show the bot repository's branch, uncommitted changes and recent commits."""
            return plugin.status()

        @bot_tool(name="ergo_config_repo_list")
        def list_files(path: str = ".") -> str:
            """List files in the bot repository, optionally under a folder."""
            return plugin.list_files(path)

        @bot_tool(name="ergo_config_repo_read")
        def read(path: str) -> str:
            """Read a file from the bot repository."""
            return plugin.read(path)

        @bot_tool(name="ergo_config_repo_write")
        def write(path: str, content: str) -> str:
            """Create or replace a file in the bot repository (unpublished until ergo_config_repo_publish)."""
            return plugin.write(path, content)

        @bot_tool(name="ergo_config_repo_diff")
        def diff() -> str:
            """Show uncommitted changes in the bot repository."""
            return plugin.diff()

        @bot_tool(name="ergo_config_repo_pull")
        def pull() -> str:
            """Update the main branch from the remote (fast-forward only)."""
            return plugin.pull()

        @bot_tool(name="ergo_config_repo_discard")
        def discard() -> str:
            """Throw away all unpublished changes."""
            return plugin.discard()

        @bot_tool(
            name="ergo_config_repo_publish",
            description=publish_help,
            requires_approval=self.approve_publish,
        )
        def publish(message: str, title: str = "", body: str = "") -> str:
            return plugin.publish(message, title, body)

        @bot_tool(name="ergo_config_repo_prs")
        def prs() -> str:
            """List open pull requests on the bot repository."""
            return plugin.prs()

        functions = [status, list_files, read, write, diff, discard, pull, publish, prs]
        return [fn.__bot_tool__ for fn in functions]
