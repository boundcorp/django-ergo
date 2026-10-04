"""The "Bots and threads" context block for chats that orchestrate.

Every turn of a chat with the ``orchestration`` skill loaded (main chats by
default) starts knowing, without a tool call:

- each bot it can message (and itself), with its description;
- under each bot, its main chat, named chats and open threads with this user:
  what each is doing (working, waiting for approval, idle), who started it,
  requests open in and out, running workers and when it last moved;
- the latest messages of each chat, as short snippets;
- open pull requests of the repos in bot.yaml's ``pull_requests``.

It is capped (``MAX_CHARS``, about 3k tokens): idle and older chats lose their
snippets first, then the oldest chats are left out, and the block says what it
left out. The history tools and ``ergo_thread_list`` still reach everything.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from django_ergo.bots.orchestrator import thread_status
from django_ergo.conversation.context import ContextSection
from django_ergo.conversation.context import ContextSource
from django_ergo.conversation.context import estimate_tokens
from django_ergo.conversation.history import Granularity
from django_ergo.conversation.history import SessionSource
from django_ergo.conversation.history import render_body
from django_ergo.settings import api_settings

if TYPE_CHECKING:
    from django_ergo.bots.runtime import Bot
    from django_ergo.bots.tools import ToolContext
    from django_ergo.conversation.models import ConversationSession

TITLE = "Bots and threads"
MAX_CHARS = 12_000  # about 3k tokens
SNIPPETS = 5  # latest messages shown per chat
SNIPPET_CHARS = 250
LATEST_CHARS = 600  # the newest message of a chat gets more room
ROWS_READ = 40  # rows read per chat to find its latest visible messages
PR_CACHE_SECONDS = 300
TOP_ROLES = ("root", "main", "chat")
HEADER = re.compile(r"^\[[^\]]*\]\s*")  # "[Message from design · Main (thread …)…]"
_pr_cache: dict[str, tuple[float, list[str]]] = {}


def ago(when) -> str:
    seconds = max(int((timezone.now() - when).total_seconds()), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


STATE_WORDS = {"waiting_for_approval": "waiting for approval", "working": "working"}


def facts(
    session: ConversationSession, current: ConversationSession, status: dict
) -> list[str]:
    """What a chat is doing, for its one-line summary (from ``thread_status``)."""
    out = [
        STATE_WORDS.get(status["state"], "idle"),
        f"last {ago(status['last_activity'])}",
    ]
    if status["started_by"]:
        out.append(f"started by {status['started_by']}")
    if status["working_for"]:
        out.append(f"working on {status['working_for']} request(s)")
    if status["waiting_on"]:
        out.append(f"waiting on {status['waiting_on']} reply(s)")
    if status["workers_running"]:
        out.append(f"{status['workers_running']} worker(s) running")
    if status["last_asks"]:
        out.append("asked the user something")
    if status["ready_to_resolve"]:
        out.append("looks finished: ready to resolve")
    if session.id == current.id:
        out.append("you are here")
    return out


def chat_name(session: ConversationSession) -> str:
    meta = session.metadata or {}
    role = meta.get("bot_role")
    if role in ("root", "main"):
        return "main"
    if role == "chat":
        return f"{meta.get('chat') or meta.get('title')} (named chat)"
    return f"{meta.get('title') or 'Thread'} (thread {session.id})"


def snippets(session: ConversationSession) -> list[str]:
    """The chat's latest visible messages, oldest first, clipped."""
    shown = []
    for message in reversed(SessionSource(session, last_rows=ROWS_READ).load()):
        if message.role not in ("user", "assistant"):
            continue
        text = HEADER.sub("", render_body(message, Granularity.CONVERSATION)).strip()
        if not text:
            continue
        who = "in" if message.role == "user" else "reply"
        limit = LATEST_CHARS if not shown else SNIPPET_CHARS
        shown.append(f"    {who}: {clip(text, limit)}")
        if len(shown) >= SNIPPETS:
            break
    return list(reversed(shown))


def open_prs(bot: Bot) -> list[str]:
    """Lines for the open pull requests this bot watches. ``DJANGO_ERGO["OPEN_PRS"]``
    (callable(bot) -> list of {repo, number, title, draft}) replaces the default,
    which asks the gh CLI about bot.yaml's ``pull_requests`` repos."""
    finder = api_settings.OPEN_PRS
    if finder is None:
        return gh_open_prs(bot.definition.pull_requests)
    rows = finder(bot) or []
    return [
        f"- {r['repo']} #{r['number']}{' (draft)' if r.get('draft') else ''}: "
        f"{clip(r.get('title', ''), 100)}"
        for r in rows
    ]


def gh_open_prs(repos: list[str]) -> list[str]:
    """Open pull requests of each repo (gh CLI), cached for a few minutes."""
    gh = shutil.which("gh")
    if not repos or gh is None:
        return []
    lines = []
    for repo in repos:
        cached = _pr_cache.get(repo)
        if cached and time.monotonic() - cached[0] < PR_CACHE_SECONDS:
            lines += cached[1]
            continue
        try:
            proc = subprocess.run(  # noqa: S603 — argv list, repo from bot.yaml
                [
                    gh,
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    "10",
                    "--json",
                    "number,title,isDraft,updatedAt",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            rows = json.loads(proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            rows = None
        if rows is None:
            found = [f"- {repo}: couldn't list pull requests"]
        elif not rows:
            found = [f"- {repo}: none open"]
        else:
            found = [
                f"- {repo} #{r['number']}{' (draft)' if r.get('isDraft') else ''}: "
                f"{clip(r.get('title', ''), 100)}"
                for r in rows
            ]
        _pr_cache[repo] = (time.monotonic(), found)
        lines += found
    return lines


def collect(bots: list[Bot], current: ConversationSession) -> list[tuple]:
    """(bot index, session, one-line summary, last activity) for each bot's chats
    with the user, most recently active first."""
    chats = []
    for index, each in enumerate(bots):
        qs = each.sessions().filter(user_id=current.user_id)
        qs = qs.filter(~Q(status="completed") | Q(metadata__bot_role__in=TOP_ROLES))
        for session in qs.order_by("-updated_at")[:30]:
            status = thread_status(session)
            line = f"- {chat_name(session)}: " + ", ".join(
                facts(session, current, status)
            )
            chats.append((index, session, line, status["last_activity"]))
    return sorted(chats, key=lambda c: c[3], reverse=True)


class Block:
    """The block's parts, and what of them still fits."""

    def __init__(self, bot: Bot, bots: list[Bot], chats: list[tuple], prs: list[str]):
        self.bot, self.bots, self.chats, self.prs = bot, bots, chats, prs
        self.keep = {c[1].id for c in chats}
        self.extra: dict = {}  # session id -> snippet lines
        self.note = ""

    def bot_line(self, each: Bot) -> str:
        registry = self.bot.registry
        note = " (this bot)" if each is self.bot else ""
        if registry and registry.is_parent(self.bot, each.name):
            note = " (your parent bot: message its main chat only)"
        about = each.definition.description or "no description"
        return f"### {each.name}{note}: {about}"

    def render(self) -> str:
        parts = []
        for index, each in enumerate(self.bots):
            parts.append(self.bot_line(each))
            for i, session, line, _ in self.chats:
                if i == index and session.id in self.keep:
                    parts += [line, *self.extra.get(session.id, [])]
        if self.prs:
            parts += ["### Open pull requests", *self.prs]
        if self.note:
            parts.append(self.note)
        return "\n".join(parts)


def overview(ctx: ToolContext, max_chars: int = MAX_CHARS) -> str:
    """The block's text: bots, their chats with this user, snippets, PRs."""
    bot, current = ctx.bot, ctx.session
    bots: list[Bot] = [bot, *(bot.registry.callable_bots(bot) if bot.registry else [])]
    chats = collect(bots, current)
    block = Block(bot, bots, chats, open_prs(bot))
    by_activity = list(chats)
    dropped = 0
    while len(block.render()) > max_chars and by_activity:
        block.keep.discard(by_activity.pop()[1].id)  # leave out the oldest chats
        dropped += 1
    if dropped:
        block.note = (
            f"({dropped} older chat(s) not shown; ergo_thread_list and the history "
            "tools reach them.)"
        )
    # Newest first get snippets; the current chat's are already in the window.
    for _, session, _, _ in by_activity:
        if session.id == current.id:
            continue
        block.extra[session.id] = snippets(session)
        if len(block.render()) > max_chars:
            block.extra.pop(session.id)  # this one and older stay one line
            break
    return block.render()


class OverviewSource(ContextSource):
    """The "Bots and threads" block (see the module docstring)."""

    weight = 2.0

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    def render(self, budget_tokens: int) -> ContextSection | None:
        if self.ctx.session is None:
            return None
        from django_ergo.bots.orchestrator import ORCHESTRATION_INSTRUCTIONS

        room = min(MAX_CHARS, max(budget_tokens, 0) * 4) - len(
            ORCHESTRATION_INSTRUCTIONS
        )
        text = overview(self.ctx, max(room, 0))
        if not text.strip():
            return None
        # The built-in rule for resolving threads rides with the block it refers to
        # (skill instructions only show when a skill is loaded by the tool).
        text = f"{text}\n\n{ORCHESTRATION_INSTRUCTIONS}"
        return ContextSection(TITLE, text, estimate_tokens(text))
