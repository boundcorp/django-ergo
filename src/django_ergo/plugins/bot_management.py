"""Bot management plugin: let a bot edit and publish its own repository.

    plugins:
      - name: bot_management
        mode: propose_pr        # or merge_main
        main_branch: main
        remote: origin
        approve_publish: true   # ergo_config_repo_publish waits for the user's approval
        root_only: false        # true: only top-level chats get these tools, not threads
        run:                    # optional: commands ergo_config_repo_run may start in the draft
          approve: true         # each run waits for the user's approval (default)
          timeout: 300          # seconds per run
          commands:
            toolkit-test: {argv: [npm, test], cwd: ficsit/toolkit}
            node: {argv: [node], cwd: ficsit/toolkit, args: true}   # the bot adds arguments

The repository is the git checkout that contains the bot folder. Tools:

- ``ergo_config_repo_status``, ``ergo_config_repo_list``, ``ergo_config_repo_read``,
  ``ergo_config_repo_grep`` and ``ergo_config_repo_diff``: look around.
- ``ergo_config_repo_write`` replaces a file; ``ergo_config_repo_edit`` replaces exact
  text in one.
- ``ergo_config_repo_delete``: delete a file (to move one, write it anew, then delete).
- ``ergo_config_repo_preview``: render a ``.jhtml`` page from the changes, with
  the draft's tables and sample rows, all rolled back (``ergo_bot_preview``).
- ``ergo_config_repo_publish``: commit everything. In ``merge_main`` mode it rebases on
  the main branch and pushes to it. In ``propose_pr`` mode it pushes a new
  branch and opens a pull request with the GitHub CLI (``gh``).
- ``ergo_config_repo_discard``: throw away unpublished changes.
- ``ergo_config_repo_pull``: fast-forward the main branch from the remote.
- ``ergo_config_repo_run``: run one of the ``run.commands`` in the draft, so a
  bot can test code it wrote before it publishes it. Only named commands run,
  as an argv (no shell), with a bare environment (``PATH``, ``HOME``, ``LANG``;
  no secrets), a timeout and trimmed output. The commands come from the live
  bot.yaml, not the draft, so a draft can't add its own. A command that takes
  ``args`` runs draft code the bot wrote, with the permissions Ergonaut has:
  keep ``approve: true`` unless the bot is trusted with the machine.

In ``merge_main`` mode the bot edits the checkout it runs from. In
``propose_pr`` mode it edits a draft: a separate git worktree of the main
branch (inside ``.git``), so the running bots never see a change until its
pull request is merged and the checkout is pulled. A draft with no changes
follows the remote main branch, so work after a merge starts from it; a
draft with changes is rebased onto it when published, and ``status`` says
when main has moved on. Changes to bot.yaml,
agents.md or tool files take effect when the bot is loaded again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from django_ergo.bots.plugins import BotPlugin
from django_ergo.bots.tools import BotTool
from django_ergo.bots.tools import FunctionToolkit
from django_ergo.bots.tools import bot_tool
from django_ergo.plugins.bash import trim

if TYPE_CHECKING:
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.toolkit import Toolkit

MODES = {"merge_main", "propose_pr"}
COMMAND_TIMEOUT = 120
PR_FETCH_SECONDS = 30


def _pr_number(version: str) -> int:
    if not re.fullmatch(r"pr-[0-9]{1,9}", version):
        msg = f"Unknown version {version!r}: use draft or pr-<number>"
        raise ValueError(msg)
    return int(version[3:])


MAX_READ_CHARS = 50_000
RUN_TIMEOUT = 300
RUN_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


@dataclass
class RunCommand:
    argv: list[str]
    cwd: str = "."
    args: bool = False  # may the bot append arguments


def _run_commands(config: dict) -> dict[str, RunCommand]:
    commands = {}
    for name, spec in (config.get("commands") or {}).items():
        if isinstance(spec, list):
            spec = {"argv": spec}  # noqa: PLW2901
        argv = spec.get("argv") if isinstance(spec, dict) else None
        if not argv or not all(isinstance(a, str) for a in argv):
            msg = f"bot_management run.commands.{name} needs an argv list of strings"
            raise ValueError(msg)
        commands[str(name)] = RunCommand(
            list(argv), str(spec.get("cwd") or "."), bool(spec.get("args", False))
        )
    return commands


MAX_GREP_MATCHES = 200


class BotManagementPlugin(BotPlugin):
    name = "bot_management"
    description = (
        "Read and change this bot repository: config, instructions, skills, tools, tables, "
        "schedules, dashboards and pages, new bots"
    )

    @property
    def skill_name(self) -> str:
        return "config_repo"

    def on_load(self) -> None:
        self.mode = self.config.get("mode", "propose_pr")
        if self.mode not in MODES:
            msg = f"bot_management mode must be one of {sorted(MODES)}"
            raise ValueError(msg)
        self.main_branch = self.config.get("main_branch", "main")
        self.remote = self.config.get("remote", "origin")
        self.approve_publish = bool(self.config.get("approve_publish", True))
        self.root_only = bool(self.config.get("root_only", False))
        run = self.config.get("run") or {}
        self.run_commands = _run_commands(run)
        self.approve_run = bool(run.get("approve", True))
        self.run_timeout = int(run.get("timeout", RUN_TIMEOUT))
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
        if draft.is_dir():
            self._follow_main(draft)
        else:
            self._fetch_main(force=True)
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
    def remote_main(self) -> str:
        return f"{self.remote}/{self.main_branch}"

    def _fetch_main(self, *, force: bool = False) -> None:
        """Fetch the remote main branch, at most every PR_FETCH_SECONDS."""
        last = self.__dict__.get("_main_fetched", -1e9)
        if force or time.monotonic() - last > PR_FETCH_SECONDS:
            self.git("fetch", self.remote, self.main_branch)
            self._main_fetched = time.monotonic()

    def _follow_main(self, draft: Path) -> None:
        """Move a draft with no changes up to the remote main branch.

        Without this, a draft made before a PR merged keeps the old main,
        and the next change re-proposes (and conflicts with) what merged.
        """
        try:
            self._fetch_main()
        except ValueError:
            return  # offline: keep working on the draft as it is
        if self.git("status", "--porcelain", cwd=draft).strip():
            return
        if self._behind(draft):
            self.git("reset", "--hard", self.remote_main, cwd=draft)

    def _behind(self, work: Path) -> int:
        """How many commits the remote main has that ``work`` doesn't."""
        count = self.git("rev-list", "--count", f"HEAD..{self.remote_main}", cwd=work)
        return int(count.strip() or 0)

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

    def run_command(self, command: str, args: list[str] | None = None) -> str:
        """Run a configured command in the draft and report its exit code and output."""
        spec = self.run_commands.get(command)
        if spec is None:
            names = ", ".join(sorted(self.run_commands)) or "none"
            msg = f"Unknown command {command!r}; configured: {names}"
            raise ValueError(msg)
        args = [str(a) for a in args or []]
        if args and not spec.args:
            msg = f"{command} takes no arguments"
            raise ValueError(msg)
        cwd = self.path(spec.cwd)
        env = {k: os.environ[k] for k in RUN_ENV_KEYS if k in os.environ}
        env["CI"] = "1"
        try:
            proc = subprocess.run(  # noqa: S603 — configured argv, no shell
                [*spec.argv, *args],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.run_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"{command}: timed out after {self.run_timeout}s"
        except OSError as exc:
            return f"{command}: could not start: {exc}"
        output = trim((proc.stdout or "") + (proc.stderr or "")).strip()
        return f"{command}: exit {proc.returncode}\n{output}".strip()

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
        behind = ""
        if work != self.repo and (count := self._behind(work)):
            behind = (
                f"\n\n{self.remote_main} has {count} commit(s) this draft doesn't; "
                "publishing rebases the changes onto it."
            )
        return (
            f"Repository: {where}{self.repo}\nBranch: {branch}\nMode: {self.mode}\n\n"
            f"Changes:\n{changes}\n\nRecent commits:\n{log}{behind}"
        )

    def list_files(self, path: str = ".") -> str:
        base = self.path(path)
        work = self.workdir
        files = self.git(
            "ls-files", "--cached", "--others", "--exclude-standard", cwd=work
        )
        prefix = "" if base == work else f"{base.relative_to(work)}/"
        return "\n".join(f for f in files.splitlines() if f.startswith(prefix))

    def read(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        text = self.path(path).read_text()
        lines = text.splitlines()
        total = len(lines)
        if start_line is None and end_line is None:
            if len(text) <= MAX_READ_CHARS:
                return text
            next_line = text[:MAX_READ_CHARS].count("\n") + 1
            return (
                f"{text[:MAX_READ_CHARS]}\n[truncated: {total} total lines; "
                f"read on with start_line={next_line}]"
            )

        start = 1 if start_line is None else int(start_line)
        end = total if end_line is None else int(end_line)
        if start < 1:
            msg = "start_line must be at least 1"
            raise ValueError(msg)
        if end < start:
            msg = "end_line must not be before start_line"
            raise ValueError(msg)
        if start > total:
            return f"{path} has {total} lines; no lines at or after {start}."
        end = min(end, total)

        rendered: list[str] = []
        size = 0
        next_line = start
        for number in range(start, end + 1):
            line = f"{number}: {lines[number - 1]}"
            if rendered and size + len(line) + 1 > MAX_READ_CHARS:
                break
            rendered.append(line)
            size += len(line) + 1
            next_line = number + 1
        result = f"{path} lines {start}-{next_line - 1} of {total}\n" + "\n".join(
            rendered
        )
        if next_line <= end:
            result += f"\n[truncated: {total} total lines; read on with start_line={next_line}]"
        return result

    def grep(self, pattern: str, path: str = ".") -> str:
        """Find regex-matching lines in tracked or untracked repository files."""
        try:
            matcher = re.compile(pattern)
        except re.error as error:
            msg = f"Invalid pattern {pattern!r}: {error}"
            raise ValueError(msg) from None
        base = self.path(path)
        work = self.workdir
        if base.is_file():
            targets = [base]
        else:
            prefix = "" if base == work else f"{base.relative_to(work)}/"
            names = self.git(
                "ls-files", "--cached", "--others", "--exclude-standard", cwd=work
            ).splitlines()
            targets = [self.path(name) for name in names if name.startswith(prefix)]

        matches = []
        for target in targets:
            if not target.is_file():
                continue
            relative = target.relative_to(work)
            for number, line in enumerate(target.read_text().splitlines(), 1):
                if matcher.search(line):
                    matches.append(f"{relative}:{number}:{line}")
                    if len(matches) == MAX_GREP_MATCHES:
                        return (
                            "\n".join(matches)
                            + f"\n[truncated at {MAX_GREP_MATCHES} matches]"
                        )
        return "\n".join(matches) or "No matches."

    def edit(
        self, path: str, old_text: str, new_text: str, replace_all: bool = False
    ) -> str:
        target = self.path(path)
        if not target.is_file():
            msg = f"{path} doesn't exist or isn't a file"
            raise ValueError(msg)
        if not old_text:
            msg = "old_text must not be empty"
            raise ValueError(msg)
        text = target.read_text()
        count = text.count(old_text)
        if not count:
            msg = f"old_text wasn't found in {path}"
            raise ValueError(msg)
        if count > 1 and not replace_all:
            msg = (
                f"old_text matches {count} times in {path}; "
                "set replace_all=true to replace each"
            )
            raise ValueError(msg)
        replacements = count if replace_all else 1
        target.write_text(text.replace(old_text, new_text, -1 if replace_all else 1))
        plural = "" if replacements == 1 else "s"
        return (
            f"Edited {target.relative_to(self.workdir)} "
            f"({replacements} replacement{plural})"
        )

    def write(self, path: str, content: str) -> str:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {target.relative_to(self.workdir)} ({len(content)} chars)"

    def delete(self, path: str) -> str:
        """Remove a file (not a folder); empty folders left behind go too."""
        target = self.path(path)
        if not target.exists():
            msg = f"{path} doesn't exist"
            raise ValueError(msg)
        if target.is_dir():
            msg = f"{path} is a folder; delete its files one by one"
            raise ValueError(msg)
        target.unlink()
        work = self.workdir
        parent = target.parent
        while parent != work and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        return f"Deleted {target.relative_to(work)}"

    def make_migrations(self, work) -> str:
        """Write migrations for any bot tables changed in ``work`` (a separate
        process, so the draft's models load fresh). Returns what it printed."""
        import os
        import sys

        from django_ergo.bots.registry import find_bot_folders

        out = []
        for folder in find_bot_folders(work):
            yaml_text = (folder / "bot.yaml").read_text()
            if "tables:" not in yaml_text:
                continue
            proc = subprocess.run(  # noqa: S603 — our own management command
                [
                    sys.executable,
                    "-m",
                    "django",
                    "ergo_bot_makemigrations",
                    str(folder),
                ],
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
                # No __pycache__ in the bot repo's migrations/.
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if proc.returncode != 0:
                msg = f"makemigrations failed for {folder.name}: {(proc.stderr or proc.stdout).strip()[-800:]}"
                raise ValueError(msg)
            if "No changes" not in proc.stdout:
                out.append(proc.stdout.strip())
        return "\n".join(out)

    def preview(self, path: str) -> str:
        """Render a .jhtml page from the draft (or checkout) in a separate process."""
        import os
        import sys

        work = self.workdir
        target = self.path(path)
        if target.suffix != ".jhtml" or not target.is_file():
            msg = f"{path} isn't a .jhtml page in the repository"
            raise ValueError(msg)
        folder = next(
            (
                p
                for p in target.parents
                if (p / "bot.yaml").is_file() and p.is_relative_to(work)
            ),
            None,
        )
        if folder is None:
            msg = f"{path} isn't inside a bot folder"
            raise ValueError(msg)
        self.make_migrations(work)
        proc = subprocess.run(  # noqa: S603 — our own management command
            [
                sys.executable,
                "-m",
                "django",
                "ergo_bot_preview",
                str(folder),
                str(target.relative_to(folder)),
            ],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        lines = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not lines:
            msg = f"Preview failed: {(proc.stderr or proc.stdout).strip()[-800:]}"
            raise ValueError(msg)
        result = json.loads(lines[-1])
        notes = "\n".join(f"Note: {n}" for n in result.get("notes", []))
        if not result.get("ok"):
            return f"The page doesn't render: {result.get('error')}\n{notes}"
        return f"Rendered {path}:\n\n{result.get('preview')}\n\n{notes}"

    def diff(self) -> str:
        work = self.workdir
        self.make_migrations(work)
        self.git("add", "--intent-to-add", "--all", cwd=work)
        return self.git("diff", "HEAD", cwd=work).strip() or "(no changes)"

    def discard(self) -> str:
        if self.mode == "merge_main":
            # Stashed, not deleted: `git stash list` / `git stash pop` brings it back.
            if not self.git("status", "--porcelain").strip():
                return "Nothing to discard."
            self.git(
                "stash",
                "push",
                "--include-untracked",
                "-m",
                f"discarded by {self.bot.name}",
            )
            return "Set the unpublished changes aside (git stash)."
        self.drop_draft()
        return "Discarded the unpublished changes."

    def pull(self) -> str:
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch != self.main_branch:
            msg = f"The checkout is on {branch}, not {self.main_branch}; not switching it."
            raise ValueError(msg)
        self._fetch_main(force=True)
        try:
            return self.git("merge", "--ff-only", self.remote_main).strip()
        except ValueError:
            ahead = int(
                self.git("rev-list", "--count", f"{self.remote_main}..HEAD").strip()
                or 0
            )
            behind = self._behind(self.repo)
            if ahead and behind:
                msg = (
                    f"The checkout has diverged from {self.remote_main}; "
                    "it can't fast-forward. Commit or discard the local changes "
                    "before pulling."
                )
                raise ValueError(msg) from None
            raise

    def publish(self, message: str, title: str = "", body: str = "") -> str:
        work = self.workdir
        # A table change goes out with its migration, in the same commit.
        self.make_migrations(work)
        if not self.git("status", "--porcelain", cwd=work).strip():
            return "Nothing to publish: there are no changes."
        if self.mode == "merge_main":
            self.git("add", "--all")
            self.git("commit", "-m", message)
            self._fetch_main(force=True)
            self.git("rebase", self.remote_main)
            self.git("push", self.remote, f"HEAD:{self.main_branch}")
            sha = self.git("rev-parse", "--short", "HEAD").strip()
            return f"Pushed {sha} to {self.main_branch}."

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", (title or message).lower()).strip("-")
        branch = f"bot/{self.bot.name}/{stamp}-{slug[:40]}".rstrip("-")
        self.git("checkout", "-b", branch, cwd=work)
        self.git("add", "--all", cwd=work)
        self.git("commit", "-m", message, cwd=work)
        try:
            self._rebase_on_main(work)
            self.git("push", "-u", self.remote, branch, cwd=work)
            url = self._open_pr(branch, message, title, body, work)
        except ValueError:
            # Put the changes back in the draft, uncommitted, so they can be published again.
            self.git("reset", "--soft", "HEAD~1", cwd=work)
            self.git("branch", "-M", self.draft_branch, cwd=work)
            raise
        self.git("worktree", "remove", "--force", str(work))
        self.git("branch", "-D", self.draft_branch)
        return f"Opened {url} from {branch}; it goes live once merged."

    def _rebase_on_main(self, work: Path) -> None:
        """Put the commit on the latest remote main, so the PR holds only this change."""
        self._fetch_main(force=True)
        if not self._behind(work):
            return
        try:
            self.git("rebase", self.remote_main, cwd=work)
        except ValueError:
            conflicts = self.git(
                "diff", "--name-only", "--diff-filter=U", cwd=work
            ).split()
            self.git("rebase", "--abort", cwd=work)
            msg = (
                f"The changes conflict with {self.remote_main} in "
                f"{', '.join(conflicts) or 'some files'}: discard the draft, "
                "or read those files from main and write the changes again."
            )
            raise ValueError(msg) from None

    def _open_pr(self, branch: str, message: str, title: str, body: str, work) -> str:
        return self.run(
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
        return self.git("diff", "HEAD", cwd=work).strip()

    # -- reading proposed versions (Ergonaut's Files browser) ----------------------
    #
    # A version is "draft" (the unpublished changes) or "pr-<number>". Paths are
    # relative to the repository; "changed" compares with the live checkout.

    def _draft_work(self) -> Path | None:
        if self.mode == "merge_main":
            return self.repo
        return self.draft_dir if self.draft_dir.is_dir() else None

    def _pr_ref(self, number: int) -> str:
        """Fetch a pull request's head into a local ref (at most every 30 seconds)."""
        ref = f"refs/ergo/pr/{int(number)}"
        fetched = self.__dict__.setdefault("_pr_fetched", {})
        if time.monotonic() - fetched.get(ref, -1e9) > PR_FETCH_SECONDS:
            self.git(
                "fetch",
                "--quiet",
                "--force",
                self.remote,
                f"refs/pull/{int(number)}/head:{ref}",
            )
            fetched[ref] = time.monotonic()
        return ref

    def version_files(self, version: str) -> tuple[list[str], dict[str, str]]:
        """Every file at ``version``, and the changed ones (path -> A, M or D)."""
        changed: dict[str, str] = {}
        if version == "draft":
            work = self._draft_work()
            if work is None:
                return [], {}
            listed = self.git(
                "ls-files", "--cached", "--others", "--exclude-standard", cwd=work
            ).splitlines()
            files = [f for f in listed if (work / f).is_file()]
            for line in self.git(
                "status", "--porcelain", "--untracked-files=all", cwd=work
            ).splitlines():
                code, path = line[:2], line[3:].split(" -> ")[-1].strip('"')
                # " A" is a new file added with --intent-to-add (diff does that); "??" untracked.
                if "D" in code:
                    changed[path] = "D"
                elif "A" in code or code == "??":
                    changed[path] = "A"
                else:
                    changed[path] = "M"
            return files, changed
        ref = self._pr_ref(_pr_number(version))
        files = self.git("ls-tree", "-r", "--name-only", ref).splitlines()
        base = self.git("merge-base", "HEAD", ref).strip()
        for line in self.git("diff", "--name-status", base, ref).splitlines():
            status, *paths = line.split("\t")
            if status.startswith("R") and len(paths) == 2:  # noqa: PLR2004 — old and new path
                changed[paths[0]], changed[paths[1]] = "D", "A"
            elif paths:
                changed[paths[0]] = status[:1]
        return files, changed

    def version_read(self, version: str, path: str) -> bytes | None:
        """A file's bytes at ``version`` (None if it isn't there)."""
        if version == "draft":
            work = self._draft_work()
            target = (work / path).resolve() if work else None
            if (
                target is None
                or not target.is_relative_to(work)
                or not target.is_file()
            ):
                return None
            return target.read_bytes()
        return self._show(f"{self._pr_ref(_pr_number(version))}:{path}")

    def live_read(self, path: str) -> bytes | None:
        """A file's bytes in the live checkout's last commit."""
        return self._show(f"HEAD:{path}")

    def _show(self, spec: str) -> bytes | None:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "show", spec],  # noqa: S607
            cwd=self.repo,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else None

    def pull_requests(self) -> list[dict]:
        """Open pull requests on the bot repository, newest first."""
        fields = "number,title,url,headRefName,author,createdAt,body,additions,deletions,changedFiles,files"
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

    def _tools(self) -> list[BotTool]:  # noqa: C901 — one closure per tool
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

        @bot_tool(
            name="ergo_config_repo_read",
            parameters={
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            required=["path"],
        )
        def read(
            path: str, start_line: int | None = None, end_line: int | None = None
        ) -> str:
            """Read a file; pass line bounds for numbered output."""
            return plugin.read(path, start_line, end_line)

        @bot_tool(name="ergo_config_repo_grep")
        def grep(pattern: str, path: str = ".") -> str:
            """Find regex matches as path:line:text."""
            return plugin.grep(pattern, path)

        @bot_tool(
            name="ergo_config_repo_write",
            # In merge_main mode a write changes the bots that are running.
            requires_approval=self.mode == "merge_main" and self.approve_publish,
        )
        def write(path: str, content: str) -> str:
            """Create or replace a file in the bot repository (unpublished until ergo_config_repo_publish)."""
            return plugin.write(path, content)

        @bot_tool(
            name="ergo_config_repo_edit",
            requires_approval=self.mode == "merge_main" and self.approve_publish,
        )
        def edit(
            path: str, old_text: str, new_text: str, replace_all: bool = False
        ) -> str:
            """Replace exact text in a file (unpublished until ergo_config_repo_publish)."""
            return plugin.edit(path, old_text, new_text, replace_all)

        @bot_tool(
            name="ergo_config_repo_delete",
            requires_approval=self.mode == "merge_main" and self.approve_publish,
        )
        def delete(path: str) -> str:
            """Delete a file from the bot repository (unpublished until ergo_config_repo_publish).
            To rename or move a file, write it at the new path, then delete the old one."""
            return plugin.delete(path)

        @bot_tool(name="ergo_config_repo_diff")
        def diff() -> str:
            """Show uncommitted changes in the bot repository."""
            return plugin.diff()

        @bot_tool(name="ergo_config_repo_preview")
        def preview(path: str) -> str:
            """Render a .jhtml page from the unpublished changes and show its text (or the error).

            Nothing is saved: the draft's tables are created in a transaction that is rolled
            back, and empty tables get sample rows, some with optional fields empty.
            """
            return plugin.preview(path)

        @bot_tool(name="ergo_config_repo_pull")
        def pull() -> str:
            """Update the main branch from the remote (fast-forward only)."""
            return plugin.pull()

        @bot_tool(
            name="ergo_config_repo_discard", requires_approval=self.approve_publish
        )
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

        names = ", ".join(
            f"{n} (takes arguments)" if c.args else n
            for n, c in sorted(self.run_commands.items())
        )

        @bot_tool(
            name="ergo_config_repo_run",
            description=(
                "Run a configured command in the unpublished changes (tests, a script with "
                f"--dry-run) and get its exit code and output. Commands: {names}. args is "
                "an argument list (no shell), only for commands that take arguments."
            ),
            requires_approval=self.approve_run,
        )
        def run_command(command: str, args: list[str] | None = None) -> str:
            return plugin.run_command(command, args)

        functions = [
            status,
            list_files,
            read,
            grep,
            write,
            edit,
            delete,
            diff,
            preview,
            discard,
            pull,
            publish,
            prs,
        ]
        if self.run_commands:
            functions.append(run_command)
        return [fn.__bot_tool__ for fn in functions]
